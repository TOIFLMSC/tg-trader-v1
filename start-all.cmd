@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
echo Запускаю Telegram reader в фоне...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-recognition-background.ps1"
if errorlevel 1 (
    echo Reader не запущен; Web UI не будет стартовать. Проверьте logs\recognition-error.log.
    pause
    exit /b 1
)
echo Открываю локальную панель http://127.0.0.1:8787
start "TG Trader browser" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8787'"
".venv\Scripts\python.exe" -m trader.webapp
pause
