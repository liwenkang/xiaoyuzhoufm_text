param([ValidateRange(1,65535)][int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }
}
& $taskPython -m pip install --timeout 90 -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Run again to retry." }
$env:PYTHONUTF8 = "1"
$env:HF_HUB_DISABLE_XET = "1"
Write-Host "Podcast workspace: http://127.0.0.1:$Port"
Write-Host "Press Ctrl+C to stop. Tasks are saved in storage."
& $taskPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1
exit $LASTEXITCODE
