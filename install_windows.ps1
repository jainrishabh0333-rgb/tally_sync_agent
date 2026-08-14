<#
    install_windows.ps1
    Registers the Tally sync agent as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        powershell -ExecutionPolicy Bypass -File .\install_windows.ps1

    The task runs every 15 minutes, whether or not a user is logged in, and
    starts automatically after a reboot. It only reads from Tally.

    Scope it to the current financial year — see -SyncArgs below:

        powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 `
          -SyncArgs '--company "SN JAIN INDUSTRIES PVT LTD - (26-27)"'
#>

param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName     = "TallyBridgeSync",
    # Extra arguments appended to sync.py on every scheduled run.
    #
    # Scope this. Left empty, a run syncs every company listed in config.toml
    # (or every company open in Tally), and eight of the nine files hold no
    # vouchers — so most of the work is repeated for nothing, every quarter
    # hour, on the same engine the sales desk is typing into. Measured against
    # the live server, one CURRENT-YEAR run costs about 35 seconds:
    # ledgers 7.5s, stock items 8.9s, a week of vouchers 18s. Nine files does
    # not.
    #
    #   -SyncArgs '--company "SN JAIN INDUSTRIES PVT LTD - (26-27)"'
    #
    # The older financial years and the unit files change rarely; run those by
    # hand, or add a second task on a nightly trigger.
    [string]$SyncArgs = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# --- locate Python -----------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Write-Error "Python not found on PATH. Install Python 3.10+ from python.org, ticking 'Add python.exe to PATH'."
}
Write-Host "Using Python: $python"

# --- dependencies ------------------------------------------------------------
# --ignore-installed is load-bearing, not belt-and-braces. The task runs as
# SYSTEM, which cannot see packages in an interactive user's site-packages.
# If the admin running this installer already has `requests` in their own
# user site-packages, a plain `pip install` finds it "already satisfied",
# installs nothing machine-wide, and prints Success — after which every
# scheduled run dies on `ModuleNotFoundError: No module named 'requests'`
# before logging exists, so it fails with no sync.log and no Frappe entry.
# That cost a full debugging session on 2026-08-14.
Write-Host "Installing dependencies (machine-wide, for the SYSTEM account)..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet --ignore-installed -r (Join-Path $here "requirements.txt")

# Verify the way SYSTEM will see it. -s disables the per-user site-packages
# directory, so this import succeeds only if the package really did land
# machine-wide. Without this check the installer cheerfully reports success
# for a task that cannot start.
& $python -s -c "import requests" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error ("Dependencies are not visible to the SYSTEM account. Re-run " +
        "this script from an Administrator PowerShell, or install manually:`n" +
        "  & '$python' -m pip install --ignore-installed requests")
}
Write-Host "  dependencies verified visible without user site-packages."

# --- config check ------------------------------------------------------------
$configPath = Join-Path $here "config.toml"
if (-not (Test-Path $configPath)) {
    Write-Error "config.toml not found. Copy config.example.toml to config.toml and fill in your Tally company name and Frappe API keys first."
}

Write-Host "Verifying connectivity to Tally and Frappe..."
& $python (Join-Path $here "sync.py") --check
if ($LASTEXITCODE -ne 0) {
    Write-Error "Connectivity check failed. Fix the issues above, then re-run this script."
}

# --- register the scheduled task --------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$scriptArg = "`"$(Join-Path $here 'sync.py')`""
if ($SyncArgs) { $scriptArg = "$scriptArg $SyncArgs" }
Write-Host "Scheduled command: $python $scriptArg"

# The task runs a generated .cmd rather than python.exe directly, for two
# reasons.
#
# One: output capture. sync.py configures logging only AFTER it imports its
# modules and parses arguments, so anything that kills it before that — a
# missing package, a bad config — leaves NO sync.log line and NO Frappe Sync
# Log row. The task simply reports a non-zero result and the mirror silently
# stops updating. Redirecting both streams to task_out.txt is the only way
# those failures are ever visible.
#
# Two: quoting. Passing a company name containing spaces and a hyphen
# through Task Scheduler into a nested interpreter is where this broke
# repeatedly — the bare "-" in "... PVT LTD - (26-27)" gets read as the
# start of a new parameter once a layer of quotes is stripped. A .cmd file
# is parsed once, by cmd, so the quotes survive intact.
$cmdPath = Join-Path $here "run_sync.cmd"
$outPath = Join-Path $here "task_out.txt"
@"
@echo off
cd /d "$here"
"$python" $scriptArg > "$outPath" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $cmdPath -Encoding ASCII
Write-Host "Wrote wrapper: $cmdPath  (output -> $outPath)"

$action = New-ScheduledTaskAction -Execute $cmdPath -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# ExecutionTimeLimit must be SHORTER than the repetition interval. Tally
# stops accepting connections while it digests a big export, and sync.py
# retries with backoff — so a bad run does not fail fast, it hangs. With the
# old two-hour limit a single hung run would sit there through eight
# scheduled starts, all of them skipped by IgnoreNew, and the mirror would
# quietly stop updating while the task still reported "running". Killing it
# at ten minutes means the next quarter-hour gets a clean attempt.
#
# IgnoreNew is the Task Scheduler default, but it is stated here because the
# whole design depends on it: two syncs writing the same vouchers at once is
# exactly the race the docname scheme exists to prevent.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -MultipleInstances IgnoreNew `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# SYSTEM so it runs with no one logged in. Tally must be running for the sync
# to succeed; failures are logged to sync.log and to the Frappe Sync Log.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Syncs TallyPrime data to Frappe every $IntervalMinutes minutes (read-only)." | Out-Null

Write-Host ""
Write-Host "Installed. Task '$TaskName' runs every $IntervalMinutes minutes." -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName        # run now"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName      # last result"
Write-Host "  Get-Content .\sync.log -Tail 40                # recent log"
Write-Host "  Get-Content .\task_out.txt -Tail 40            # last run's raw output"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName   # remove"
