@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Живой журнал распознавания. Для выхода нажмите Ctrl+C.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '.\logs\recognition.log' -Tail 50 -Wait -Encoding UTF8"
