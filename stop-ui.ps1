$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidPath = Join-Path $projectRoot 'data\ui.pid'

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host 'UI не запущен: PID-файл отсутствует.'
    exit 0
}

$uiPid = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$uiPid)) {
    Write-Host 'UI не запущен: PID-файл некорректен.'
    exit 0
}

$process = Get-Process -Id $uiPid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Host "UI уже остановлен; удалён устаревший PID $uiPid."
    exit 0
}
if ($process.ProcessName -ne 'python') {
    throw "PID $uiPid принадлежит неожиданному процессу $($process.ProcessName); остановка отменена."
}

Stop-Process -Id $uiPid
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Process -Id $uiPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $uiPid -ErrorAction SilentlyContinue) {
    throw "UI PID $uiPid не остановился."
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
Write-Host "UI остановлен, PID $uiPid."
