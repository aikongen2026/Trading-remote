# One-time local Microsoft login -> secured Trading Bot connection.
# No passwords or OAuth tokens are sent to ChatGPT or saved in this script.
$ErrorActionPreference = 'Stop'
$endpoint = 'https://trading-remote.onrender.com/api/onedrive/pair'
$workspace = Join-Path $env:TEMP 'tradingbot-onedrive-setup'
New-Item -Path $workspace -ItemType Directory -Force | Out-Null

$localRclone = Get-Command rclone -ErrorAction SilentlyContinue
if ($localRclone) {
    $rcloneExe = $localRclone.Source
} else {
    $archive = Join-Path $workspace 'rclone-win64.zip'
    Write-Host 'Laster ned rclone fra offisiell kilde ...'
    Invoke-WebRequest -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile $archive
    $extract = Join-Path $workspace 'rclone'
    Expand-Archive -Path $archive -DestinationPath $extract -Force
    $found = Get-ChildItem -Path $extract -Filter rclone.exe -Recurse -File | Select-Object -First 1
    if (-not $found) { throw 'Kunne ikke finne rclone.exe etter nedlasting.' }
    $rcloneExe = $found.FullName
}

Write-Host ''
Write-Host 'Microsoft OneDrive-innlogging aapnes i nettleseren.'
Write-Host 'Velg din jobb-OneDrive og godkjenn bare hvis du stoler paa rclone.'
Write-Host ''
$consoleText = (& $rcloneExe authorize onedrive 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw 'Microsoft-innloggingen ble ikke fullfoert. Kontroller om IT har sperret rclone.'
}
$matched = [regex]::Match($consoleText, '(?s)(\{"access_token".*?\})')
if (-not $matched.Success) {
    throw 'Fant ikke et gyldig OneDrive-token fra rclone. Ingen opplysninger er sendt.'
}
try { $token = $matched.Value | ConvertFrom-Json -AsHashtable }
catch { throw 'Kunne ikke lese Microsoft-token. Ingen opplysninger er sendt.' }
if (-not $token.access_token -or -not $token.refresh_token -or -not $token.expiry) {
    throw 'Token mangler paakrevde felter. Ingen opplysninger er sendt.'
}
$secret = Read-Host 'Skriv PASSORDET til Trading Bot-nettsiden (ikke Microsoft-passordet)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $dashboardPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
if ([string]::IsNullOrWhiteSpace($dashboardPassword)) {
    throw 'Trading Bot-passord kan ikke vaere tomt.'
}
try {
    $body = @{ token = $token } | ConvertTo-Json -Depth 8 -Compress
    $reply = Invoke-RestMethod -Uri $endpoint -Method POST -Headers @{
        'X-Setup-Password' = $dashboardPassword
    } -ContentType 'application/json' -Body $body -TimeoutSec 120
    if (-not $reply.ok -or -not $reply.status.connected) {
        throw 'Render bekreftet ikke OneDrive-tilkoblingen.'
    }
    Write-Host ''
    Write-Host 'KLART: OneDrive er koblet til Render.' -ForegroundColor Green
    Write-Host ('Mappe: ' + $reply.status.destination)
    Write-Host 'Sikkerhetskopiering starter automatisk og kjoerer videre i skyen.'
} finally {
    Remove-Variable dashboardPassword,body,token,consoleText,secret -ErrorAction SilentlyContinue
}
