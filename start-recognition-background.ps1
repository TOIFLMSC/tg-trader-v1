$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$pidPath = Join-Path $projectRoot 'data\recognition.pid'
if (Test-Path -LiteralPath $pidPath) {
    $savedPid = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$savedPid)) {
        $savedProcess = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
        if ($savedProcess -and $savedProcess.ProcessName -notin @('python', 'pythonw')) {
            throw "PID $savedPid принадлежит неожиданному процессу $($savedProcess.ProcessName)."
        }
        if ($savedProcess) {
            Write-Host "Распознавание уже работает, PID $savedPid."
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidPath -Force
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data') | Out-Null
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$stdout = Join-Path $projectRoot 'logs\recognition.log'
$stderr = Join-Path $projectRoot 'logs\recognition-error.log'
$recognitionProcess = Start-Process -FilePath $python -ArgumentList '-u', '-m', 'trader.service' `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr -PassThru
$deadline = (Get-Date).AddSeconds(30)
$servicePid = 0
$serviceProcess = $null
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $pidPath) {
        [void][int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$servicePid)
        $serviceProcess = Get-Process -Id $servicePid -ErrorAction SilentlyContinue
        $ready = (Test-Path -LiteralPath $stdout) -and
            ((Get-Content -LiteralPath $stdout -Raw -ErrorAction SilentlyContinue) -match 'READY:')
        if ($serviceProcess -and $ready) {
            Write-Host "Распознавание запущено в фоне, PID $servicePid."
            Write-Host 'Живой журнал: watch-recognition-log.cmd'
            exit 0
        }
    }
    if ($recognitionProcess.HasExited -and -not $serviceProcess) {
        break
    }
    Start-Sleep -Milliseconds 250
}
Write-Host 'Готовность reader не подтверждена. Откройте logs\recognition-error.log и logs\recognition.log.'
exit 1
