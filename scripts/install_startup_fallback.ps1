# Auto-start every instance at LOGON, without needing administrator rights.
#
# WHY A SECOND MECHANISM EXISTS. The machine rebooted on 2026-09-25 at 19:06
# local and only BTC came back: `BTC15Signal` and `BTC15Recorder` were the only
# boot-triggered tasks, so ETH, gold, silver and SOL stayed down with nothing
# to say so - and SOL had just been armed for live trading, so "armed" and
# "running" had quietly come apart.
#
# `scripts\install_instance_tasks.ps1` is the better fix: real scheduled tasks
# with BOOT triggers, which start the services before anyone logs in, and with
# restart-on-failure. But registering a scheduled task needs an ELEVATED shell
# on this box - a non-elevated Register-ScheduledTask is refused outright with
# "Access is denied", including for a plain logon-only task.
#
# So this is the fallback that works with the rights actually available: a
# shortcut per instance in the user's Startup folder. The differences, stated
# rather than glossed:
#
#   * it fires at LOGON, not at boot. An unattended reboot that stops at the
#     lock screen leaves these instances down until somebody logs in;
#   * Task Scheduler's restart-on-failure does not apply. Each entry launches
#     the instance with -Supervise, so watchdog.py still relaunches the service
#     if it exits - the supervision is inside the process tree instead.
#
# Re-running is safe: the .cmd files are overwritten, and run_service.py's
# single-instance lock makes a duplicate launch a no-op rather than a second
# trader.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup_fallback.ps1
#
# Remove with -Uninstall.

param(
    [string[]]$Instances = @('eth', 'gold', 'silver', 'sol'),
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$startup = [Environment]::GetFolderPath('Startup')

Write-Output "project : $root"
Write-Output "startup : $startup"

foreach ($name in $Instances) {
    $cmd = Join-Path $startup "BTC15Signal-$($name.ToUpper()).cmd"

    if ($Uninstall) {
        if (Test-Path $cmd) { Remove-Item $cmd -Force; Write-Output "$name : removed" }
        else { Write-Output "$name : nothing to remove" }
        continue
    }

    $launcher = Join-Path $root "scripts\run_$name.ps1"
    if (-not (Test-Path $launcher)) {
        Write-Output "$name : no launcher at $launcher - SKIPPED"
        continue
    }
    if (-not (Select-String -Path $launcher -Pattern '\$Supervise' -Quiet)) {
        Write-Output "$name : launcher has no -Supervise switch - SKIPPED"
        continue
    }

    # `start "" /min` returns immediately so one slow instance cannot hold up
    # the others, and -WindowStyle Hidden keeps the console out of the way.
    # The working directory is set explicitly: every path inside the launchers
    # is relative to the project root.
    $body = @"
@echo off
rem Written by scripts\install_startup_fallback.ps1 - starts the $($name.ToUpper())
rem instance at logon under watchdog.py supervision. Safe to delete; the
rem instance can always be started by hand with scripts\run_$name.ps1.
cd /d "$root"
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$launcher" -Supervise
"@
    Set-Content -Path $cmd -Value $body -Encoding ASCII
    Write-Output "$name : installed -> $cmd"
}

Write-Output ''
Write-Output '--- startup entries now ---'
Get-ChildItem $startup -Filter 'BTC15Signal-*.cmd' -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Output "  $($_.Name)" }
if (-not (Get-ChildItem $startup -Filter 'BTC15Signal-*.cmd' -ErrorAction SilentlyContinue)) {
    Write-Output '  (none)'
}
