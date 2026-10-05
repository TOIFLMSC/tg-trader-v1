@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo TG Trader UI: http://127.0.0.1:8787
".venv\Scripts\python.exe" -m trader.webapp
pause
