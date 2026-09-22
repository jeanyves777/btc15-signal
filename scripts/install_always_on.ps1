# Make the BTC15 system run 24/7: at boot, without a logged-in user, supervised.
#
# The scheduled task that existed fired on LOGON, ran run_service.py directly
# with no watchdog, and had no recorder at all - so a reboot or power cut left
# the system down until somebody signed in, and then unsupervised. This fixes
# all three. Changing a task's trigger or principal requires elevation, which
# is why this is a separate script rather than something the agent could apply.
#
# Run as Administrator. Logs to runtime\install_always_on.log.

$ErrorActionPreference = 'Stop'
$root = 'D:\Kalshi\btc15-signal'
$pyw  = Join-Path $root '.venv\Scripts\pythonw.exe'
$log  = Join-Path $root 'runtime\install_always_on.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Say($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# Registering a task with a boot trigger and an S4U principal REQUIRES
# elevation. Run unelevated, every call fails with Access is denied - which is
# exactly what happened on the first attempt, from a normal PowerShell.
$admin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host ""
    Write-Host "  NOT RUNNING AS ADMINISTRATOR - nothing was changed." -ForegroundColor Red
    Write-Host ""
    Write-Host "  Close this window. Press Start, type PowerShell, right-click"
    Write-Host "  'Windows PowerShell' and choose 'Run as administrator'."
    Write-Host "  An elevated window says 'Administrator:' in its title bar and"
    Write-Host "  opens in C:\WINDOWS\system32. Then run:"
    Write-Host ""
    Write-Host "      D:\Kalshi\btc15-signal\scripts\install_always_on.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Say "=== install_always_on starting ==="
if (-not (Test-Path $pyw)) { Say "FATAL: interpreter missing at $pyw"; exit 1 }

# AtStartup fires before any logon; the logon trigger is a belt-and-braces
# second path for the case where the machine is already up.
$boot  = New-ScheduledTaskTrigger -AtStartup
$logon = New-ScheduledTaskTrigger -AtLogOn -User 'admin'

# S4U runs the task whether or not a user is signed in, WITHOUT storing a
# password. Outbound HTTPS (Kalshi, Binance, Telegram) works under S4U; only
# network resources authenticated as the user would not, and none are used.
$principal = New-ScheduledTaskPrincipal -UserId 'admin' -LogonType S4U -RunLevel Highest

# StartWhenAvailable covers a missed trigger after an unclean shutdown.
# ExecutionTimeLimit 0 means never kill it for running too long - this is a
# permanent service, not a job.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# The task starts the WATCHDOG, not the service: the watchdog owns restarting
# run_service.py, and run_service.py's own single-instance lock stops the two
# ever racing. Pointing the task at the service left nothing supervising it.
$jobs = @(
    @{ Name = 'BTC15Signal';   Arg = 'scripts\watchdog.py';                Desc = 'BTC15 trading service, supervised by its watchdog' },
    @{ Name = 'BTC15Recorder'; Arg = '-m btc15_signal.recorder --every 10'; Desc = 'BTC15 order-book recorder' }
)

foreach ($job in $jobs) {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument $job.Arg -WorkingDirectory $root
    try {
        Register-ScheduledTask -TaskName $job.Name -Action $action `
            -Trigger @($boot, $logon) -Principal $principal -Settings $settings `
            -Description $job.Desc -Force -ErrorAction Stop | Out-Null
        # VERIFY, do not assume. The first version logged "registered" after a
        # non-terminating Access-is-denied that the catch never saw.
        $check = Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue
        if ($check -and ($check.Triggers.CimClass.CimClassName -contains 'MSFT_TaskBootTrigger')) {
            Say "$($job.Name): registered (boot + logon, S4U, watchdog-supervised)"
        } else {
            Say "$($job.Name): registration did NOT take - still $($check.Triggers[0].CimClass.CimClassName)"
        }
    } catch {
        Say "$($job.Name): FAILED - $($_.Exception.Message)"
    }
}

# The nightly optimiser must never rewrite a LIVE rule again. It overwrote
# strategy.json at 02:00 daily and can emit `enabled: false`, which is what
# silently stopped trading for four hours on 2026-09-21.
foreach ($pair in @(
    @{ Task = 'BTC15Optimizer';          From = 'strategy.json';           To = 'reports/proposed-strategy.json' },
    @{ Task = 'BTC15ReversionOptimizer'; From = 'reversion_strategy.json'; To = 'reports/proposed-reversion-strategy.json' }
)) {
    $t = Get-ScheduledTask -TaskName $pair.Task -ErrorAction SilentlyContinue
    if (-not $t) { Say "$($pair.Task): not present, skipped"; continue }
    $a = $t.Actions[0]
    if ($a.Arguments -like "*--strategy-output $($pair.From)*") {
        $newArgs = $a.Arguments -replace [regex]::Escape("--strategy-output $($pair.From)"), "--strategy-output $($pair.To)"
        Set-ScheduledTask -TaskName $pair.Task `
            -Action (New-ScheduledTaskAction -Execute $a.Execute -Argument $newArgs -WorkingDirectory $a.WorkingDirectory) | Out-Null
        Say "$($pair.Task): output repointed away from the live rule"
    } else {
        Say "$($pair.Task): already safe"
    }
}

Say "--- verification ---"
foreach ($name in 'BTC15Signal', 'BTC15Recorder', 'BTC15Optimizer', 'BTC15ReversionOptimizer') {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { Say "$name : MISSING"; continue }
    $triggers = ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task','' -replace 'Trigger','' }) -join '+'
    Say ("{0,-24} state={1,-8} trigger={2,-16} runas={3}/{4}" -f `
        $name, $t.State, $triggers, $t.Principal.UserId, $t.Principal.LogonType)
}

# Start them now if they are not already up, so 24/7 begins immediately rather
# than at the next reboot.
foreach ($name in 'BTC15Signal', 'BTC15Recorder') {
    $running = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
        Where-Object { $_.CommandLine -match 'watchdog|recorder' }
    if (-not $running) {
        Start-ScheduledTask -TaskName $name
        Say "$name : started now"
    }
}
Say "=== done ==="
