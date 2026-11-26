# Trading Bot evidence backup -> existing Windows OneDrive sync folder.
# One-time on-demand downloader; no hourly network costs. No OAuth/cloud app setup.
# Password stored encrypted with Windows DPAPI for the current user only.
param([switch]$RunBackup, [switch]$SelfTest,
      [string]$TargetDirectory = (Join-Path $env:USERPROFILE 'OneDrive - Straye AS\AI\tradingbot'))
$ErrorActionPreference = 'Stop'
$baseUrl = 'https://trading-remote.onrender.com'
$appDir = Join-Path $env:LOCALAPPDATA 'TradingBotBackup'
$target = Join-Path $TargetDirectory 'Backups'
$oneDriveRoot = Split-Path (Split-Path $TargetDirectory -Parent) -Parent
$credFile = Join-Path $appDir 'dashboard-password.dpapi'
$scriptPath = Join-Path $appDir 'install-local-onedrive-backup.ps1'

function Assert-Archive([string]$File, [string]$ExpectedSha) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $actualSha = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash
    if ([string]::IsNullOrWhiteSpace($ExpectedSha) -or $actualSha -ne $ExpectedSha) {
        throw 'Nedlastingen besto ikke SHA256-kontrollen.'
    }
    $zip = [System.IO.Compression.ZipFile]::OpenRead($File)
    try {
        $allowed = @('learning_v3.sqlite','runtime_audit.sqlite','broker_history.sqlite','alpaca_edge_v49.sqlite')
        if ($zip.Entries.Count -ne 5) { throw 'Backupen inneholder uventet antall filer.' }
        $manifestEntry = $zip.GetEntry('manifest.json')
        if ($null -eq $manifestEntry) { throw 'Backupen mangler manifest.' }
        $manifestStream = $manifestEntry.Open()
        try {
            $reader = New-Object System.IO.StreamReader($manifestStream)
            try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { $reader.Dispose() }
        } finally { $manifestStream.Dispose() }
        if ($manifest.format -ne 'tradingbot-paper-evidence-v1' -or
            $manifest.mode -ne 'PAPER' -or $manifest.files.Count -ne 4) {
            throw 'Backupmanifestet er ugyldig eller ikke PAPER.'
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            foreach ($name in $allowed) {
                $e = $zip.GetEntry($name)
                $entry = @($manifest.files | Where-Object { $_.name -eq $name })
                if ($null -eq $e -or $entry.Count -ne 1) {
                    throw 'Databasen mangler i backupen.'
                }
                if ($e.Length -gt 805306368 -or $e.Length -ne [long]$entry[0].bytes) {
                    throw 'Databasefilen har ugyldig stoerrelse.'
                }
                $stream = $e.Open()
                try {
                    $digest = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant()
                } finally { $stream.Dispose() }
                if ($digest -ne [string]$entry[0].sha256) {
                    throw 'En database besto ikke SHA256-kontrollen.'
                }
            }
        } finally { $sha.Dispose() }
        foreach ($e in $zip.Entries) {
            if ($e.FullName -notin (@('manifest.json') + $allowed)) {
                throw 'Backupen inneholder uventede filer.'
            }
        }
    } finally { $zip.Dispose() }
}

function Invoke-Backup {
    if (-not (Test-Path -LiteralPath $oneDriveRoot -PathType Container)) {
        throw 'OneDrive - Straye AS-mappen finnes ikke paa denne PC-en.'
    }
    if (-not (Test-Path -LiteralPath $credFile -PathType Leaf)) {
        throw 'Ingen lokalt lagret Trading Bot-innlogging. Kjoer installasjonskommandoen.'
    }
    New-Item -Path $target -ItemType Directory -Force | Out-Null
    $secure = (Get-Content -LiteralPath $credFile -Raw).Trim() | ConvertTo-SecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $http = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    try {
        $null = Invoke-WebRequest -Uri "$baseUrl/login" -Method POST -Body @{password=$password} -WebSession $http -UseBasicParsing -TimeoutSec 30
    } finally {
        Remove-Variable password -ErrorAction SilentlyContinue
    }
    # Keep unfinished downloads OUTSIDE the OneDrive sync directory.
    $temp = Join-Path $env:TEMP ('.trbot-' + [Guid]::NewGuid().ToString('N') + '.partial')
    try {
        # Build ZIP in the cloud first, then transfer it only once it is ready.
        $started = Invoke-RestMethod -Uri "$baseUrl/api/evidence-backup/start" -Method POST -WebSession $http -ContentType 'application/json' -Body '{}' -TimeoutSec 40
        $jobId = [string]$started.id
        if (-not $jobId -or $started.phase -notin @('ready','building')) {
            throw ('Backup ble ikke startet: ' + [string]$started.error)
        }
        $deadline = (Get-Date).AddMinutes(18)
        $status = $started
        while ($status.phase -eq 'building' -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 6
            $status = Invoke-RestMethod -Uri "$baseUrl/api/evidence-backup/status" -WebSession $http -TimeoutSec 30
            if ([string]$status.id -ne $jobId) { throw 'Backupjobb-ID endret. Ingen data ble lastet ned.' }
            Write-Host ('Backup forberedes i Render ... ' + $status.phase)
        }
        if ($status.phase -ne 'ready') {
            throw ('Backup ikke klar: ' + [string]$status.error)
        }
        Write-Host ('ZIP er klar: ' + [math]::Round(([double]$status.size_bytes / 1MB), 1) + ' MiB. Laster ned én gang.')
        $resp = Invoke-WebRequest -Uri "$baseUrl/api/evidence-backup" -WebSession $http -OutFile $temp -PassThru -UseBasicParsing -TimeoutSec 900
        if ($resp.StatusCode -ne 200) { throw 'Render svarte ikke med en gyldig sikkerhetskopi.' }
        $hashHeader = [string]$resp.Headers['X-TradingBot-SHA256']
        Assert-Archive -File $temp -ExpectedSha $hashHeader
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmss')
        $dest = Join-Path $target ('TradingBot_EVIDENCE_' + $stamp + 'UTC.zip')
        if (Test-Path -LiteralPath $dest) { throw 'Backup med samme filnavn finnes allerede.' }
        Move-Item -LiteralPath $temp -Destination $dest
        Write-Host ('BACKUP OK: ' + $dest) -ForegroundColor Green
        return $dest
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

if ($SelfTest) {
    # Test installation script without changing PC, logging in or scheduling jobs.
    if ($baseUrl -ne 'https://trading-remote.onrender.com') { throw 'Ugyldig endpoint.' }
    if ($target -notlike '*OneDrive - Straye AS\AI\tradingbot\Backups') { throw 'Feil maalmappe.' }
    Write-Host 'Backup script self-test OK' -ForegroundColor Green
    return
}

New-Item -Path $appDir -ItemType Directory -Force | Out-Null
if ($RunBackup) {
    try { $null = Invoke-Backup }
    catch { Write-Error ('Backup mislyktes: ' + $_.Exception.Message); exit 1 }
    return
}

if (-not (Test-Path -LiteralPath $oneDriveRoot -PathType Container)) {
    throw 'OneDrive - Straye AS er ikke synkronisert paa PC-en. Ingen endringer gjort.'
}
$setupDir = Join-Path $TargetDirectory 'Oppsett'
New-Item -Path $setupDir -ItemType Directory -Force | Out-Null
$savedScript = Join-Path $setupDir 'install-local-onedrive-backup.ps1'
foreach ($destination in @($scriptPath, $savedScript)) {
    if ([IO.Path]::GetFullPath($PSCommandPath) -ne [IO.Path]::GetFullPath($destination)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $destination -Force
    }
}
$sourceBat = Join-Path $PSScriptRoot 'Hent_TradingBot_til_OneDrive.bat'
$savedBat = Join-Path $setupDir 'Hent_TradingBot_til_OneDrive.bat'
if ((Test-Path -LiteralPath $sourceBat) -and
    [IO.Path]::GetFullPath($sourceBat) -ne [IO.Path]::GetFullPath($savedBat)) {
    Copy-Item -LiteralPath $sourceBat -Destination $savedBat -Force
}
Write-Host 'Trading Bot - sikkerhetskopi til eksisterende OneDrive' -ForegroundColor Cyan
Write-Host ('Lagringsmappe: ' + $target)
Write-Host 'Bruker OneDrive-programmet som allerede er innlogget. Ingen Microsoft-app kobles til Render.'
if (-not (Test-Path -LiteralPath $credFile -PathType Leaf)) {
    $secure = Read-Host 'Skriv Trading Bot-passordet (ikke OneDrive-passord)' -AsSecureString
    $secure | ConvertFrom-SecureString | Set-Content -LiteralPath $credFile -Encoding UTF8
} else {
    Write-Host 'Bruker eksisterende sikkert lagret Trading Bot-innlogging.' -ForegroundColor Green
}
Write-Host 'Tester full sikkerhetskopiering mot Render ...'
$null = Invoke-Backup
Write-Host 'FERDIG: Verifisert sikkerhetskopi lagret i OneDrive-mappen paa PC-en.' -ForegroundColor Green
Write-Host 'OneDrive-programmet synkroniserer filen til skyen naar det er aktivt.'
Write-Host ('Kjoer Hent_TradingBot_til_OneDrive.bat i ' + $setupDir + ' naar du vil hente en ny kopi.')
