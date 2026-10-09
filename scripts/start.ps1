$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install requirements-lock.txt first; see README.md.'
}
Set-Location -LiteralPath $projectRoot
& $pythonExecutable -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
