Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

chcp 65001 | Out-Null
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Write-Host 'UTF-8 terminal mode enabled for this PowerShell session.' -ForegroundColor Green
Write-Host 'InputEncoding     :' [Console]::InputEncoding.WebName
Write-Host 'OutputEncoding    :' [Console]::OutputEncoding.WebName
Write-Host 'PYTHONUTF8        :' $env:PYTHONUTF8
Write-Host 'PYTHONIOENCODING  :' $env:PYTHONIOENCODING
Write-Host ''
Write-Host 'Tip: dot-source this script to affect your current shell:' -ForegroundColor Yellow
Write-Host '. .\scripts\use_utf8_terminal.ps1'
