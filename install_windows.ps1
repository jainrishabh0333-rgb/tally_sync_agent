<#
    install_windows.ps1
    Registers the Tally sync agent as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        powershell -ExecutionPolicy Bypass -File .\install_windows.ps1

    The task runs every 15 minutes, whether or not a user is logged in, and
    starts automatically after a reboot. It only reads from Tally.

    Scope it to the current financial year -- see -SyncArgs below:

        powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 `
          -SyncArgs '--company "SN JAIN INDUSTRIES PVT LTD - (26-27)"'

    If PowerShell rejects that form with 'the value of argument "name" is not
    valid', it re-parsed the quotes. Call the script directly instead:

        cd C:\tally_bridge
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        .\install_windows.ps1 -SyncArgs '--company "SN JAIN INDUSTRIES PVT LTD - (26-27)"'

    Re-running is safe: it unregisters and re-registers, and regenerates the
    wrapper. The wrapper and its output go to -TaskDir (default: a sibling
    directory, C:\tally_bridge -> C:\tally_bridge_task) so that refreshing the
    agent folder from GitHub cannot delete the file the task launches.
#>

param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName     = "TallyBridgeSync",
    # Where the generated wrapper and its output live. This MUST be outside
    # the folder you refresh from GitHub.
    #
    # run_sync.cmd is generated here, not carried by the repo. While it lived
    # in the sync agent folder, refreshing that folder deleted it -- and the
    # task went on firing every 15 minutes at a path that no longer existed,
    # exiting 1 before Python started. No sync.log, no Frappe Sync Log row, no
    # failure anywhere: the mirror silently stopped while every dashboard
    # reported health. That happened on 2026-08-14 and again on 2026-08-20,
    # both times after a routine file update.
    #
    # Defaults to a sibling directory: C:\tally_bridge -> C:\tally_bridge_task.
    [string]$TaskDir = "",
    # Extra arguments appended to sync.py on every scheduled run.
    #
    # Scope this. Left empty, a run syncs every company listed in config.toml
    # (or every company open in Tally), and eight of the nine files hold no
    # vouchers -- so most of the work is repeated for nothing, every quarter
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

# Resolve the protected directory and make sure it exists. Deliberately a
# SIBLING of the agent folder rather than a subfolder of it -- a subfolder would
# be inside whatever gets refreshed, which is the whole problem.
if (-not $TaskDir) {
    $TaskDir = Join-Path (Split-Path -Parent $here) ((Split-Path -Leaf $here) + "_task")
}
if (-not (Test-Path $TaskDir)) {
    New-Item -ItemType Directory -Path $TaskDir -Force | Out-Null
    Write-Host "Created $TaskDir"
}
Write-Host "Wrapper directory: $TaskDir  (outside $here on purpose -- do not delete)"

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
# installs nothing machine-wide, and prints Success -- after which every
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

# config.toml is gitignored, so it sits in the refreshed folder with the same
# exposure run_sync.cmd used to have: a folder refresh deletes it, and sync.py
# then has no Frappe credentials to report its own death with. Keep a copy in
# the protected directory so the file can be restored without rebuilding the
# keys by hand. This copy is a BACKUP, not what the agent reads.
$configBackup = Join-Path $TaskDir "config.toml.backup"
Copy-Item $configPath $configBackup -Force
Write-Host "  backed up config.toml -> $configBackup"

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

# The task runs a generated .cmd rather than python.exe directly, for three
# reasons.
#
# One: output capture. sync.py configures logging only AFTER it imports its
# modules and parses arguments, so anything that kills it before that -- a
# missing package, a bad config -- leaves NO sync.log line and NO Frappe Sync
# Log row. The task simply reports a non-zero result and the mirror silently
# stops updating. Redirecting both streams to task_out.txt is the only way
# those failures are ever visible.
#
# Two: quoting. Passing a company name containing spaces and a hyphen
# through Task Scheduler into a nested interpreter is where this broke
# repeatedly -- the bare "-" in "... PVT LTD - (26-27)" gets read as the
# start of a new parameter once a layer of quotes is stripped. A .cmd file
# is parsed once, by cmd, so the quotes survive intact.
#
# Three: survival. Both files are written to $TaskDir, OUTSIDE the agent
# folder, so refreshing that folder from GitHub can no longer delete the one
# thing the scheduled task points at. The task still RUNS from $here -- sync.py
# and config.toml live there -- it just is not launched from there.
$cmdPath = Join-Path $TaskDir "run_sync.cmd"
$outPath = Join-Path $TaskDir "task_out.txt"
@"
@echo off
cd /d "$here"
"$python" $scriptArg > "$outPath" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $cmdPath -Encoding ASCII
Write-Host "Wrote wrapper: $cmdPath  (output -> $outPath)"

# Remove the wrapper from any previous install that put it inside the agent
# folder. Leaving it there is worse than deleting it: it looks authoritative,
# it is now unreferenced, and the next person debugging a dead sync will read
# a stale file and conclude everything is wired up.
$legacyCmd = Join-Path $here "run_sync.cmd"
if (Test-Path $legacyCmd) {
    Remove-Item $legacyCmd -Force
    Write-Host "  removed the old wrapper at $legacyCmd (it is no longer used)"
}

$action = New-ScheduledTaskAction -Execute $cmdPath -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# ExecutionTimeLimit must be SHORTER than the repetition interval. Tally
# stops accepting connections while it digests a big export, and sync.py
# retries with backoff -- so a bad run does not fail fast, it hangs. With the
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
Write-Host "  Get-Content '$here\sync.log' -Tail 40          # recent log"
Write-Host "  Get-Content '$outPath' -Tail 40                # last run's raw output"
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName   # remove"
Write-Host ""
Write-Host "The wrapper now lives in $TaskDir, outside the agent folder." -ForegroundColor Yellow
Write-Host "Refreshing $here from GitHub can no longer kill the task."
Write-Host "It still cannot replace config.toml though -- that file is gitignored"
Write-Host "and a refresh deletes it. If it goes missing, restore it from"
Write-Host "  $configBackup"
