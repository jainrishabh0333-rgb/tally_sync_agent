<#
    install_production_windows.ps1
    Registers the production-voucher fetch as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        cd C:\tally_bridge
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        .\install_production_windows.ps1 -Company "SN JAIN INDUSTRIES PVT LTD - (26-27)"

    This is a THIRD task alongside TallyBridgeSync and TallyBridgeReorder.
    It mirrors the factory's own vouchers — cutting issue, cutting, job work,
    pressing, packing — WITH their item lines, which the 15-minute sync
    cannot see (those vouchers carry no ledger value at all: measured over
    May–Aug 2026, all 929 cutting issues, 285 cutting journals, 433 job-work
    and 916 packing vouchers total exactly 0.00).

    FIRST TIME, run the probe by hand while Tally is open, and read it:

        python production_fetch.py --probe

    It writes nothing and reports which of the three possible XML shapes
    this Tally build uses for a stock journal's two sides. The fetcher
    handles all three, but knowing beats assuming, and the answer decides
    how much to trust the derived fabric norms.

    Why daily and not every 15 minutes: production vouchers drive fabric
    norms and WIP views, which are read a few times a day at most, and the
    fetch re-reads a rolling window. Daily at midday keeps it off the
    engine's back while the sales desk types.

    Re-running is safe: it unregisters and re-registers, and regenerates
    the wrapper.
#>

param(
    [string]$TaskName  = "TallyBridgeProduction",
    # Must fall inside Tally's working hours. The reorder task's history
    # taught the lesson: the first successful connection of the day lands
    # between 10:10 and 13:19, so midday clears every observed opening —
    # and StartWhenAvailable plus the retry window below covers a late one.
    [string]$AtTime    = "12:15",
    # How many days back each run re-reads. Production vouchers get
    # back-dated and edited like everything else in this book; a fortnight
    # of overlap catches that, and the upsert is GUID-keyed so replays are
    # free.
    [int]$Days         = 14,
    [string]$Company   = "",
    # MUST be outside the folder that self_update.ps1 refreshes. The sync
    # task learned this the hard way twice: a wrapper living inside the
    # refreshed folder gets deleted by the refresh, and the task goes on
    # firing at a path that no longer exists — no log, no Frappe row, no
    # failure anywhere while every dashboard reports health.
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
# The probe reads Tally and writes nothing. Better to fail here, in front of
# someone, than on a schedule into a log nobody opens. It also PRINTS which
# XML shape this build uses — read that output; it is the answer the fetcher
# has been waiting for since the day it was written.
Write-Host "Probing Tally (reads only, writes nothing)..."
if ($Company) { $env:TALLY_COMPANY = $Company }

& $python (Join-Path $here "production_fetch.py") --probe --days 30
if ($LASTEXITCODE -ne 0) {
    Write-Error ("Probe failed. If Tally is closed, open it and re-run. " +
        "If it reports zero vouchers, check the company name.")
}

# --- generate the wrapper ----------------------------------------------------
$cmdPath = Join-Path $TaskDir "run_production.cmd"
$logPath = Join-Path $TaskDir "production_fetch.log"
$scriptPath = Join-Path $here "production_fetch.py"

$envLine = ""
if ($Company) {
    # Environment variable rather than argument: sidesteps the quoting a
    # company name with spaces and brackets causes through cmd, PowerShell
    # and the Task Scheduler in turn.
    $envLine = "set `"TALLY_COMPANY=$Company`""
}

@"
@echo off
rem Generated by install_production_windows.ps1 -- do not edit by hand.
rem Lives outside the agent folder so refreshing that folder cannot delete it.
cd /d "$here"
$envLine
if exist "$logPath" for %%A in ("$logPath") do if %%~zA GTR 2000000 move /y "$logPath" "$logPath.1" >nul
echo ---- %DATE% %TIME% ---- >> "$logPath"
"$python" "$scriptPath" --days $Days >> "$logPath" 2>&1
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
# Same retry reasoning as the reorder task: a once-a-day job that meets a
# Frappe worker restart or a late-opening Tally must retry at the SCHEDULER
# level, or it loses a whole day of production data.
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable `
                -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
                -RestartCount 6 -RestartInterval (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Mirrors the factory's production vouchers (cutting, job work, packing) with item lines, daily." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName', daily at $AtTime, running as SYSTEM."
Write-Host ""
Write-Host "FIRST LOAD -- run once by hand with a wide window, while Tally is open:"
Write-Host "    python production_fetch.py --days 150"
Write-Host ""
Write-Host "Then run the task once to confirm the schedule works:"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "    Get-Content '$logPath' -Tail 30"
Write-Host ""
Write-Host "Note: Get-ScheduledTask reporting LastTaskResult 267009 means the task"
Write-Host "is RUNNING, not broken. Wait for it to finish before reading that."
