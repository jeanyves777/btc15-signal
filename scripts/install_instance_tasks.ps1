# Auto-start every instance at boot, not just BTC.
#
# WHAT WENT WRONG WITHOUT THIS. The machine rebooted on 2026-09-25 at 19:06
# local and only the BTC instance came back, because `BTC15Signal` and
# `BTC15Recorder` were the only boot-triggered tasks on the box. ETH, gold,
# silver and SOL stayed down with nothing to say so - and SOL had just been
# armed for live trading, so "armed" and "running" had quietly come apart.
# A trading service that does not survive a reboot is a silent stop, which is
# the failure mode this system is most exposed to.
#
# ONE TASK PER INSTANCE, mirroring BTC15Signal exactly: boot AND logon
# triggers, restart 999 times at one-minute intervals, no execution time
# limit, highest run level, S4U so it runs whether or not anyone is logged in.
#
# Each task runs that instance's OWN launcher with -Supervise, so the
# environment comes from the one file that already defines it and watchdog.py
# relaunches the service if it exits. There is deliberately no second copy of
# any instance's environment here: a duplicated env block is how one instance
# ends up writing another's database (FINDINGS 43, 63).
#
# Re-running this is safe - `-Force` replaces an existing task of the same
# name, and run_service.py's single-instance lock means a duplicate launch is
# a no-op rather than a second trader.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_instance_tasks.ps1
#
# BTC is NOT touched. It already has BTC15Signal and that task is the one with
# the longest running history; re-registering it here would risk breaking the
# instance that trades with approval for no gain.

param(
    [string[]]$Instances = @('eth', 'gold', 'silver', 'sol', 'xrp'),
    [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

Write-Output "project : $root"
Write-Output "shell   : $ps"

foreach ($name in $Instances) {
    $launcher = Join-Path $root "scripts\run_$name.ps1"
    if (-not (Test-Path $launcher)) {
        Write-Output "$name : no launcher at $launcher - SKIPPED"
        continue
    }
    # The launcher must actually understand -Supervise, or the task would run
    # the service unsupervised and the restart-on-exit would be lost silently.
    if (-not (Select-String -Path $launcher -Pattern '\$Supervise' -Quiet)) {
        Write-Output "$name : launcher has no -Supervise switch - SKIPPED"
        continue
    }

    $task = "BTC15Signal-$($name.ToUpper())"
    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Supervise"

    if ($WhatIfOnly) {
        Write-Output "$task : would register -> $ps $arg"
        continue
    }

    $action = New-ScheduledTaskAction -Execute $ps -Argument $arg -WorkingDirectory $root
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -AtLogOn)
    )
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest

    Register-ScheduledTask -TaskName $task -Action $action -Trigger $triggers `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output "$task : registered"
}

Write-Output ''
Write-Output '--- every BTC15 task on this box ---'
Get-ScheduledTask | Where-Object { $_.TaskName -like 'BTC15*' } | Sort-Object TaskName | ForEach-Object {
    $trig = ($_.Triggers | ForEach-Object {
        $_.CimClass.CimClassName -replace 'MSFT_Task', '' -replace 'Trigger', ''
    }) -join '+'
    Write-Output ("  {0,-24} state={1,-8} triggers={2}" -f $_.TaskName, $_.State, $trig)
}
