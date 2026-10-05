@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m trader.service --stop
pause
