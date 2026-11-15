# Trading Bot Google Drive pairing V2 (Windows PowerShell 5.1 and PowerShell 7)
# Runs official rclone authorization locally and never prints or uploads tokens
# anywhere except over TLS to the existing password-protected Trading Bot service.
param([switch]$SelfTest)
$ErrorActionPreference = 'Stop'

function Invoke-NativeAuthorization([string]$Program, [string]$Arguments) {
    # Use .NET redirected streams, NOT PowerShell's native 2>&1 pipeline.
    # Native stderr NOTICE output must never become a terminating PS 5.1 error.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Program
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) { throw 'Kunne ikke starte rclone.' }
        # Read both streams concurrently so neither can block the process.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Stdout = $stdoutTask.GetAwaiter().GetResult()
            Stderr = $stderrTask.GetAwaiter().GetResult()
        }
    } finally {
        $process.Dispose()
    }
}

if ($SelfTest) {
    $psExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path $psExe)) { throw 'Self-test krever Windows PowerShell 5.1.' }
    $cmd = '-NoProfile -Command "[Console]::Out.WriteLine(''JSON_OK''); [Console]::Error.WriteLine(''NOTICE: config file not found''); exit 0"'
    $result = Invoke-NativeAuthorization $psExe $cmd
    if ($result.ExitCode -ne 0 -or $result.Stdout -notmatch 'JSON_OK' -or $result.Stderr -notmatch 'NOTICE') {
        throw 'PowerShell standard output/error-test feilet.'
    }
    Write-Host 'PowerShell V2-test bestatt.' -ForegroundColor Green
    return
}

Write-Host 'Trading Bot Google Drive V2 - ny innloggingsmetode' -ForegroundColor Cyan
$endpoint = 'https://trading-remote.onrender.com/api/google-drive/pair'
$workspace = Join-Path $env:TEMP 'tradingbot-google-drive-setup'
New-Item -Path $workspace -ItemType Directory -Force | Out-Null
$localRclone = Get-Command rclone -ErrorAction SilentlyContinue
if ($localRclone) {
    $rcloneExe = $localRclone.Source
} else {
    $extract = Join-Path $workspace 'rclone'
    $found = Get-ChildItem -Path $extract -Filter rclone.exe -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) {
        $archive = Join-Path $workspace 'rclone-win64.zip'
        Write-Host 'Laster ned rclone fra offisiell kilde ...'
        Invoke-WebRequest -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile $archive -UseBasicParsing
        Expand-Archive -Path $archive -DestinationPath $extract -Force
        $found = Get-ChildItem -Path $extract -Filter rclone.exe -Recurse -File | Select-Object -First 1
    }
    if (-not $found) { throw 'rclone.exe ble ikke funnet.' }
    $rcloneExe = $found.FullName
}

# The shared Google OAuth application expires during 2026.
# Use a user's personal Desktop OAuth client for reliable cloud refresh.
Write-Host ''
Write-Host 'Google krever egen OAuth Desktop-app (engangsoppsett).' -ForegroundColor Yellow
Write-Host 'Veiledning: https://rclone.org/drive/#making-your-own-client-id'
$clientId = Read-Host 'Google Client ID (slutter paa .apps.googleusercontent.com)'
if ($clientId -notmatch '^[a-zA-Z0-9._-]+\.apps\.googleusercontent\.com$') {
    throw 'Ugyldig Google Client ID. Ingen data er sendt.'
}
$clientSecretInput = Read-Host 'Google Client Secret' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($clientSecretInput)
try { $clientSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
if ($clientSecret -notmatch '^[a-zA-Z0-9._-]{5,256}$') {
    throw 'Ugyldig Google Client Secret. Ingen data er sendt.'
}
$config = @{scope='drive.file';client_id=$clientId;client_secret=$clientSecret} | ConvertTo-Json -Compress
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($config))
Write-Host 'Google-innlogging aapnes i nettleseren. Velg din private Google-konto.' -ForegroundColor Cyan
Write-Host 'Fullfoer innloggingen hos Google, og kom tilbake hit.'
$result = Invoke-NativeAuthorization $rcloneExe ("authorize drive " + $encoded)
if ($result.ExitCode -ne 0) {
    # Explicitly classify errors, never display OAuth URLs or token values.
    $details = $result.Stderr + $result.Stdout
    $reason = 'Ukjent autorisasjonsfeil'
    if ($details -match 'invalid_client') { $reason = 'Google avviste Client ID/Secret' }
    elseif ($details -match 'access_denied|permission denied') { $reason = 'Google-kontoen avslo tilgang' }
    elseif ($details -match 'redirect_uri_mismatch') { $reason = 'OAuth Redirect URI er feil' }
    elseif ($details -match 'unauthorized_client|app is blocked|blocked') { $reason = 'Google blokkerte OAuth-appen' }
    elseif ($details -match 'connection refused|cannot open browser|failed to open browser') { $reason = 'Nettleser eller localhost-tilkobling mislyktes' }
    throw ('Google-innlogging mislyktes: ' + $reason + '. Se https://rclone.org/drive/')
}

# The OAuth JSON is generated by rclone. Never echo it to the console.
# Consider either standard stream since rclone versions differ.
$consoleText = $result.Stdout + "`n" + $result.Stderr
$matched = [regex]::Match($consoleText, '(?s)\{\s*"access_token"\s*:\s*".*?"\s*,.*?\}')
if (-not $matched.Success) {
    throw 'Google-innloggingen ga ikke et gjenkjennelig token. Ingen opplysninger ble sendt til Render.'
}
try { $token = $matched.Value | ConvertFrom-Json }
catch { throw 'Kunne ikke lese Google Drive-token. Ingen opplysninger ble sendt.' }
if (-not $token.access_token -or -not $token.refresh_token -or -not $token.expiry) {
    throw 'Google-token mangler paakrevde opplysninger. Ingen opplysninger ble sendt.'
}

$secret = Read-Host 'Passordet til Trading Bot-nettsiden (ikke Google-passord)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $dashboardPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
if ([string]::IsNullOrWhiteSpace($dashboardPassword)) {
    throw 'Trading Bot-passordet kan ikke vaere tomt.'
}

try {
    $body = @{ token = $token; client_id = $clientId; client_secret = $clientSecret } | ConvertTo-Json -Depth 8 -Compress
    $reply = Invoke-RestMethod -Uri $endpoint -Method POST -Headers @{
        'X-Setup-Password' = $dashboardPassword
    } -ContentType 'application/json' -Body $body -TimeoutSec 120
    if (-not $reply.ok -or -not $reply.status.connected) {
        throw 'Render bekreftet ikke Google Drive-tilkoblingen.'
    }
    Write-Host ''
    Write-Host 'KLART: Google Drive er koblet til Render.' -ForegroundColor Green
    Write-Host ('Google Drive-mappe: ' + $reply.status.destination)
    Write-Host 'Automatiske sikkerhetskopier fortsetter selv om PC-en er avslaatt.'
} finally {
    Remove-Variable dashboardPassword,body,token,consoleText,secret,result,clientSecret,clientSecretInput,config,encoded -ErrorAction SilentlyContinue
}
