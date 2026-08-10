<#
    install_windows.ps1
    Registers the Tally sync agent as a Windows Scheduled Task.

    Run in PowerShell **as Administrator** from the sync_agent folder:

        powershell -ExecutionPolicy Bypass -File .\install_windows.ps1

    The task runs every 15 minutes, whether or not a user is logged in, and
    starts automatically after a reboot. It only reads from Tally.
#>

param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName     = "TallyBridgeSync"
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
Write-Host "Installing dependencies..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $here "requirements.txt")

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

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$(Join-Path $here 'sync.py')`"" -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

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
Write-Host "  Unregister-ScheduledTask -TaskName $TaskName   # remove"
