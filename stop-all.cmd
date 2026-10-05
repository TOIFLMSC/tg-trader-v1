@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m trader.service --stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-ui.ps1"
pause
