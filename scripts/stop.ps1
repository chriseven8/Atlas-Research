$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $projectRoot 'data\processes.json'
if (-not (Test-Path -LiteralPath $pidPath)) { Write-Host 'No Atlas process record found.'; exit 0 }
$record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
foreach ($entry in @($record.api, $record.web)) {
    $process = Get-Process -Id $entry.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().ToString('o') -eq $entry.started) {
        # Kill only the recorded process and its children, never unrelated listeners.
        & taskkill.exe /PID $entry.id /T /F 2>$null
    }
}
Remove-Item -LiteralPath $pidPath
Write-Host 'Atlas processes stopped. Research data has been preserved.'
