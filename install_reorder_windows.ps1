<#
    install_reorder_windows.ps1
    Registers the reorder refresh as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        cd C:\tally_bridge
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        .\install_reorder_windows.ps1 -Company "SN JAIN INDUSTRIES PVT LTD - (26-27)"

    This is a SECOND task alongside TallyBridgeSync, not a replacement.
    TallyBridgeSync mirrors Tally into Frappe every 15 minutes; this one
    refreshes the reorder report's live columns once a day.

    Why daily and not every 15 minutes: reorder levels drive cutting
    decisions, which are made daily at most. Each run re-reads every
    movement since the last export, so running it constantly would re-fetch
    the same vouchers from the engine the sales desk is typing into, for a
    number nobody looks at more than once a day.

    Re-running is safe: it unregisters and re-registers, and regenerates the
    wrapper.
#>

param(
    [string]$TaskName  = "TallyBridgeReorder",
    # 24-hour clock. Early enough that the numbers are ready before the
    # cutting decisions get made.
    [string]$AtTime    = "06:30",
    # Which company's movements to apply. Reorder levels are per item+size and
    # not company-scoped, so refreshing against more than one book would
    # double-count every movement.
    [string]$Company   = "",
    # Refuse to refresh a baseline older than this. See reorder_refresh.py:
    # accuracy is anchored to the last Reorder Report export, and past some
    # age a delta is no longer worth trusting.
    [int]$MaxAgeDays   = 45,
    # MUST be outside the folder you refresh from GitHub. run_reorder.cmd is
    # generated here, not carried by the repo. The sync task learned this the
    # hard way twice: while its wrapper lived in the agent folder, refreshing
    # that folder deleted it, and the task went on firing at a path that no
    # longer existed -- exiting before Python started, so no log, no Frappe
    # row, no failure anywhere while every dashboard reported health.
    [string]$TaskDir   = ""
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

# --- prove it runs before scheduling it -------------------------------------
# A dry run writes nothing and exits non-zero if Tally or Frappe is
# unreachable, or if no reorder levels have been exported yet. Better to fail
# here, in front of someone, than at 06:30 into a log nobody opens.
Write-Host "Verifying the refresh runs (dry run, writes nothing)..."
$checkArgs = @((Join-Path $here "reorder_refresh.py"), "--dry-run", "--show", "5")
if ($MaxAgeDays) { $checkArgs += @("--max-age-days", "$MaxAgeDays") }
& $python @checkArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error ("Dry run failed. Fix the issue above, then re-run this script. " +
        "If it reports no reorder rows, export the Reorder Report from Tally first.")
}

# --- generate the wrapper ----------------------------------------------------
$cmdPath = Join-Path $TaskDir "run_reorder.cmd"
$logPath = Join-Path $TaskDir "reorder_refresh.log"
$scriptPath = Join-Path $here "reorder_refresh.py"

$argLine = "--max-age-days $MaxAgeDays"
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
rem Generated by install_reorder_windows.ps1 -- do not edit by hand.
rem Lives outside the agent folder so refreshing that folder cannot delete it.
cd /d "$here"
$envLine
"$python" "$scriptPath" $argLine >> "$logPath" 2>&1
"@ | Set-Content -Path $cmdPath -Encoding ASCII

Write-Host "Wrote $cmdPath"
Write-Host "Log: $logPath"

# --- register ----------------------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action    = New-ScheduledTaskAction -Execute $cmdPath
$trigger   = New-ScheduledTaskTrigger -Daily -At $AtTime
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Refreshes the reorder report's live columns from Tally once a day." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName', daily at $AtTime, running as SYSTEM."
Write-Host ""
Write-Host "Run it once now to confirm:"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "    Get-Content '$logPath' -Tail 30"
Write-Host ""
Write-Host "Note: Get-ScheduledTask reporting LastTaskResult 267009 means the task"
Write-Host "is RUNNING, not broken. Wait for it to finish before reading that."
