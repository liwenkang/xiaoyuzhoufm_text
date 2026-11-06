param([ValidateRange(1,65535)][int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }
}
& $taskPython -m pip install --timeout 90 -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Run again to retry." }
$env:PYTHONUTF8 = "1"
Write-Host "Podcast workspace: http://127.0.0.1:$Port"
Write-Host "Paste an episode URL on the page. Configure credentials first with: .venv\Scripts\python.exe -m app.credentials"
Write-Host "Press Ctrl+C to stop."
& $taskPython -m uvicorn app.serverless:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
