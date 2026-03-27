Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    git config core.hooksPath .githooks
    Write-Host 'Git hooks path configured to .githooks' -ForegroundColor Green
    Write-Host 'The UTF-8 pre-commit hook is now active for this repository.' -ForegroundColor Green
} finally {
    Pop-Location
}
