<#
    install_orders_windows.ps1
    Registers the order importer as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        cd C:\tally_bridge
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        .\install_orders_windows.ps1

    This is a THIRD task alongside TallyBridgeSync and TallyBridgeReorder,
    not a replacement.

    Why it has to exist at all: sync.py mirrors Tally INTO Frappe and has no
    order-import step. order_importer.py is a separate script whose config
    default is one pass and exit. Nothing else drains the queue. Until this
    task exists, an order queued from the Order Pad page sits at Pending
    forever, and the failure is silent at every level -- the page says
    "Queued", the queue row looks healthy, and no voucher ever appears.

    Every 15 minutes inside working hours, not daily and not around the
    clock. Orders are typed against a pad in front of a customer and want to
    be in Tally the same session, so once a day is far too slow. Outside
    working hours Tally is shut, and a run then just logs a connection error
    -- harmless, but a night of them buries the one line that matters.

    Re-running is safe: it unregisters and re-registers, and regenerates the
    wrapper.
#>

param(
    [string]$TaskName = "TallyBridgeOrders",
    # Start of the repeat window, 24-hour clock. Measured across a week of
    # sync logs (see install_reorder_windows.ps1), the first connection that
    # succeeds lands between 10:10 and 10:55, once as late as 13:19. Starting
    # at 10:00 costs a few harmless "company closed" lines and means an order
    # queued first thing is in Tally the moment Tally opens.
    [string]$StartTime      = "10:00",
    [int]$WindowHours       = 10,
    # 15 minutes matches the sync task's cadence. The importer asks Frappe for
    # pending orders and almost always gets none, so an idle pass is one cheap
    # HTTP call -- nothing like the heavy pulls that make Tally throttle.
    [int]$EveryMinutes      = 15,
    # Only process orders for this company file, exactly as Tally names it.
    # Left empty, config.toml decides, which is what you want with one book.
    [string]$Company        = "",
    # MUST be outside the folder you refresh from GitHub. run_orders.cmd is
    # generated here, not carried by the repo. The sync task learned this the
    # hard way twice: while its wrapper lived in the agent folder, refreshing
    # that folder deleted it, and the task went on firing at a path that no
    # longer existed -- exiting before Python started, so no log, no Frappe
    # row, no failure anywhere while every dashboard reported health.
    [string]$TaskDir        = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $TaskDir) {
    $TaskDir = Join-Path (Split-Path -Parent $here) ((Split-Path -Leaf $here) + "_task")
}
if (-not (Test-Path $TaskDir)) {
    New-Item -ItemType Directory -Path $TaskDir -Force | Out-Null
}
Write-Host "Wrapper directory: $TaskDir  (outside $here on purpose -- do not delete)"

# --- locate Python -----------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org, ticking 'Add python.exe to PATH'."
}
Write-Host "Using Python: $python"

# --- dependencies visible to SYSTEM -----------------------------------------
# -s disables per-user site-packages, which is how the SYSTEM account sees the
# world. An import that only works without -s means the package landed in an
# interactive user's profile and every scheduled run will die on
# ModuleNotFoundError before logging exists.
& $python -s -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dependencies machine-wide for the SYSTEM account..."
    & $python -m pip install --quiet --ignore-installed -r (Join-Path $here "requirements.txt")
    & $python -s -c "import requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Error ("Dependencies are not visible to the SYSTEM account. Re-run " +
            "from an Administrator PowerShell, or install manually:`n" +
            "  & '$python' -m pip install --ignore-installed requests")
    }
}
Write-Host "  dependencies verified visible without user site-packages."

# --- config ------------------------------------------------------------------
$configPath = Join-Path $here "config.toml"
if (-not (Test-Path $configPath)) {
    Write-Error "config.toml not found. This task reuses the same config as TallyBridgeSync."
}

# --- show what a pass would do, before scheduling it -------------------------
# A dry run builds the XML for every pending order and prints it, sending
# nothing and changing no status.
#
# Deliberately NOT a gate on the exit code, unlike the reorder installer. The
# importer exits 1 when an ORDER would fail, which says nothing about whether
# the task is configured correctly -- one stale bad row in the queue would
# otherwise block the install that fixes the drain. What a person needs to see
# here is the output, so it is printed and explained rather than enforced.
Write-Host "Dry run -- builds envelopes, sends nothing, changes no status..."
if ($Company) { $env:TALLY_COMPANY = $Company }

$checkArgs = @((Join-Path $here "order_importer.py"), "--dry-run")
if ($Company) { $checkArgs += @("--company", $Company) }
& $python @checkArgs
$dryExit = $LASTEXITCODE
if ($dryExit -ne 0) {
    Write-Host ""
    Write-Warning ("The dry run reported at least one order that would FAIL. " +
        "That is about the orders in the queue, not about this task, so the " +
        "install continues. Read the output above before trusting the next " +
        "real pass.")
}

# --- generate the wrapper ----------------------------------------------------
$cmdPath    = Join-Path $TaskDir "run_orders.cmd"
$logPath    = Join-Path $TaskDir "order_import.log"
$scriptPath = Join-Path $here "order_importer.py"

# --once regardless of what [orders].poll says in config.toml. poll = true
# makes the script sleep and drain every 60 seconds forever, which is a
# reasonable way to run it by hand and a terrible way to run it from a
# scheduler -- the task would never exit, the next trigger would be skipped as
# "already running", and one crash would end importing until someone noticed.
$argLine = "--once"
$envLine = ""
if ($Company) {
    # Passed as an environment variable rather than an argument: load_settings
    # reads TALLY_COMPANY, and this sidesteps the quoting problems a company
    # name with spaces and brackets causes through cmd, PowerShell and the
    # Task Scheduler in turn.
    $envLine = "set `"TALLY_COMPANY=$Company`""
}

@"
@echo off
rem Generated by install_orders_windows.ps1 -- do not edit by hand.
rem Lives outside the agent folder so refreshing that folder cannot delete it.
cd /d "$here"
$envLine
rem Keep the log from growing without bound: once past ~2 MB it is rotated to
rem .1 and started fresh. A job firing every 15 minutes would otherwise leave a
rem file nobody can open on the day they finally need to read it.
if exist "$logPath" for %%A in ("$logPath") do if %%~zA GTR 2000000 move /y "$logPath" "$logPath.1" >nul
echo ---- %DATE% %TIME% ---- >> "$logPath"
"$python" "$scriptPath" $argLine >> "$logPath" 2>&1
"@ | Set-Content -Path $cmdPath -Encoding ASCII

Write-Host "Wrote $cmdPath"
Write-Host "Log: $logPath"

# --- register ----------------------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $cmdPath

# A daily trigger carrying a repetition, which is how Task Scheduler expresses
# "every N minutes, but only between these hours". Building the repetition from
# a throwaway -Once trigger is the documented idiom; -Daily does not take
# -RepetitionInterval directly.
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Hours $WindowHours)).Repetition

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# No RestartCount here, unlike the reorder task. A missed pass costs at most
# fifteen minutes because the next one is already scheduled; scheduler-level
# retries would only stack passes on top of each other. The time limit is well
# under the repeat interval so a wedged run can never overlap the next.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description ("Imports approved Sales Orders from the Frappe queue into " +
                  "TallyPrime, every $EveryMinutes minutes during working hours.") | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName': every $EveryMinutes min for $WindowHours h from $StartTime, as SYSTEM."
Write-Host ""
Write-Host "Run it once now to confirm:"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "    Get-Content '$logPath' -Tail 30"
Write-Host ""
Write-Host "Note: Get-ScheduledTask reporting LastTaskResult 267009 means the task"
Write-Host "is RUNNING, not broken. Wait for it to finish before reading that."
