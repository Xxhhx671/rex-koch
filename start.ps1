# Запуск соцсети «Сфера»
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Kofi запускается на http://127.0.0.1:8000" -ForegroundColor Cyan
Write-Host "Остановка: Ctrl+C`n" -ForegroundColor DarkGray

py -m uvicorn app.main:app --host 127.0.0.1 --port 8000
