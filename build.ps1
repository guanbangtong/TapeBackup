# Build dist\TapeBackup.exe (single file, no console window).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv .venv
    & $Python -m pip install --quiet -r requirements.txt
}

& $Python -m PyInstaller `
    --noconfirm --clean `
    --onefile --windowed `
    --name TapeBackup `
    run.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Built: $(Join-Path $PSScriptRoot 'dist\TapeBackup.exe')"
Write-Host "Copy that one file to the tape PC. Settings are saved as config.json next to it."
