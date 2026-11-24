# Jarvis setup for Windows (PowerShell). Run from the repo root:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Write-Host "== Python backend ==" -ForegroundColor Cyan
Push-Location backend
if (-not (Test-Path .venv)) {
  try { py -3.11 -m venv .venv } catch { }
  if (-not (Test-Path .venv)) { python -m venv .venv }
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Pop-Location
Write-Host "== Desktop app ==" -ForegroundColor Cyan
Push-Location app
npm install
Pop-Location
Write-Host "Done. Start Jarvis with:  cd app; npm run dev" -ForegroundColor Green
