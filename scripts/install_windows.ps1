$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$TaskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited

if (-not (Test-Path (Join-Path $Project ".env"))) {
    throw "Create .env from .env.example and add Telegram settings first."
}

if (-not (Test-Path $Python)) {
    py -3 -m venv (Join-Path $Project ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Python environment creation failed." }
}

& $Python -m pip install -e $Project
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
$Action = New-ScheduledTaskAction `
    -Execute (Join-Path $Project ".venv\Scripts\pythonw.exe") `
    -Argument ('"' + (Join-Path $Project "scripts\run_service.py") + '"') `
    -WorkingDirectory $Project
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask `
    -TaskName "BTC15Signal" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "BTC 15-minute Kalshi signal and Telegram alerts" `
    -Force
Start-ScheduledTask -TaskName "BTC15Signal"
$OptimizeAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m btc15_signal.kalshi_backtest --days 30 --output reports/flexible-backtest.json --strategy-output strategy.json" `
    -WorkingDirectory $Project
$OptimizeTrigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask `
    -TaskName "BTC15Optimizer" `
    -Action $OptimizeAction `
    -Trigger $OptimizeTrigger `
    -Principal $Principal `
    -Description "Daily fee-aware BTC15 walk-forward optimization" `
    -Force
$ReversionAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "-m btc15_signal.reversion_backtest --days 30 --output reports/reversion-backtest.json --strategy-output reversion_strategy.json" `
    -WorkingDirectory $Project
Register-ScheduledTask `
    -TaskName "BTC15ReversionOptimizer" `
    -Action $ReversionAction `
    -Trigger $OptimizeTrigger `
    -Principal $Principal `
    -Description "Daily BTC15 spike-reversion take-profit optimization" `
    -Force
Write-Host "BTC15Signal installed and started."
