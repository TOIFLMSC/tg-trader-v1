@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" bootstrap.py check
if errorlevel 1 goto end
".venv\Scripts\python.exe" bootstrap.py telegram
if errorlevel 1 goto end
".venv\Scripts\python.exe" bootstrap.py owner
:end
pause
