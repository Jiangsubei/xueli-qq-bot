$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

function Fail-AndExit {
    param(
        [string]$Message,
        [int]$Code = 1
    )

    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-Host ""
    Read-Host "Press Enter to continue"
    exit $Code
}

Write-Host ""
Write-Host "========================================"
Write-Host "       QQ AI Launcher (PowerShell)"
Write-Host "========================================"
Write-Host ""

try {
    python --version *> $null
} catch {
    Fail-AndExit "Python was not found. Please install Python 3.8+ first."
}

if (-not (Test-Path -LiteralPath ".venv")) {
    Write-Host "[INFO] Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Fail-AndExit "Failed to create virtual environment."
    }
}

Write-Host "[INFO] Activating virtual environment..." -ForegroundColor Cyan
. .\.venv\Scripts\Activate.ps1

Write-Host "[INFO] Installing dependencies..." -ForegroundColor Cyan
python -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Fail-AndExit "Failed to install dependencies."
}

if (-not (Test-Path -LiteralPath "xueli\config\.env")) {
    Write-Host ""
    Write-Host "[WARN] .env was not found." -ForegroundColor Yellow
    Write-Host "[INFO] Creating .env from .env.example..." -ForegroundColor Cyan
    Copy-Item -LiteralPath "xueli\config\.env.example" -Destination "xueli\config\.env" -Force
    Write-Host ""
    Write-Host "========================================"
    Write-Host "Edit xueli\config\.env and run start.ps1 again."
    Write-Host "========================================"
    Write-Host ""
    notepad xueli\config\.env
    Read-Host "Press Enter to continue"
    exit 1
}

Write-Host ""
Write-Host "========================================"
Write-Host "Starting bot and WebUI..."
Write-Host "Press Ctrl+C to stop."
Write-Host "========================================"
Write-Host ""

python main.py
$appExit = $LASTEXITCODE

if (Test-Path -LiteralPath ".\.venv\Scripts\Deactivate.ps1") {
    . .\.venv\Scripts\Deactivate.ps1
}

Write-Host ""
Write-Host "Service stopped."
Read-Host "Press Enter to continue"
exit $appExit
