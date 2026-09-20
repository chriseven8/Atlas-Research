param([switch]$Install)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$frontendPath = Join-Path $projectRoot 'frontend'
$runPath = Join-Path $projectRoot 'data'
New-Item -ItemType Directory -Force -Path $runPath | Out-Null

if (-not (Test-Path -LiteralPath $pythonPath)) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    $Install = $true
}
if ($Install) {
    & $pythonPath -m pip install -r backend/requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
    & $pythonPath -m pip install --no-deps -e backend
    if ($LASTEXITCODE -ne 0) { throw 'Backend installation failed.' }
    Push-Location -LiteralPath $frontendPath
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath 'node_modules\next\dist\bin\next'))) {
    throw 'Dependencies are missing. Run: powershell -ExecutionPolicy Bypass -File scripts/start.ps1 -Install'
}
foreach ($port in @(8765, 3000)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use. If Atlas is running, open http://localhost:3000."
    }
}
$env:NEXT_TELEMETRY_DISABLED = '1'
$apiProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'financial_research.api:app', '--host', '127.0.0.1', '--port', '8765') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runPath 'api.log') -RedirectStandardError (Join-Path $runPath 'api-error.log')
$uiProcess = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList @('node_modules/next/dist/bin/next', 'dev', '--hostname', '127.0.0.1', '--port', '3000') -WorkingDirectory $frontendPath -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runPath 'web.log') -RedirectStandardError (Join-Path $runPath 'web-error.log')
@{ api = @{ id = $apiProcess.Id; started = $apiProcess.StartTime.ToUniversalTime().ToString('o') }; web = @{ id = $uiProcess.Id; started = $uiProcess.StartTime.ToUniversalTime().ToString('o') } } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $runPath 'processes.json') -Encoding utf8
$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $apiHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
        $webHealth = Invoke-WebRequest -Uri 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 3
        if ($apiHealth.status -eq 'ok' -and $webHealth.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 1 }
    if ($apiProcess.HasExited -or $uiProcess.HasExited) { break }
}
if (-not $ready) {
    foreach ($startedProcess in @($apiProcess, $uiProcess)) {
        if (-not $startedProcess.HasExited) { & taskkill.exe /PID $startedProcess.Id /T /F 2>$null }
    }
    throw 'Startup did not complete. Check data/api-error.log and data/web-error.log.'
}
Write-Host ''
Write-Host 'Atlas Research is ready: http://localhost:3000' -ForegroundColor Green
Write-Host 'API documentation: http://localhost:8765/docs'
Write-Host 'Stop: powershell -ExecutionPolicy Bypass -File scripts/stop.ps1'
