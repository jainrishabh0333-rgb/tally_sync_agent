<#
diagnose_task.ps1 -- why is TallyBridgeSync not syncing?

Read-only. Prints evidence; changes nothing. Run it on the Tally server
(AnyDesk to wsrv50172-ind) in PowerShell as Administrator, from C:\tally_bridge:

    powershell -ExecutionPolicy Bypass -File .\diagnose_task.ps1

This exists because this task has now died twice in a way that is INVISIBLE
from the Frappe side. When the task dies before Python starts -- a deleted
run_sync.cmd, a missing module, a missing config.toml -- there is no sync.log,
no Tally Sync Log row and no Error Log. `recent_failures` stays empty and
`sync_health` reported the mirror healthy while it sat a full day behind.
The evidence only exists here, so this is the first place to look, not the
last.

Two results are famously misread, so they are spelled out below:
  LastTaskResult 267009  = SCHED_S_TASK_RUNNING -- working, not failing.
  State: Ready           = "not running this instant". NOT "healthy".
#>

$ErrorActionPreference = "Continue"
$TaskName = "TallyBridgeSync"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# The wrapper and its output live OUTSIDE this folder, so a GitHub refresh
# cannot delete them. Older installs put them in $here; both are checked.
$TaskDir = Join-Path (Split-Path -Parent $here) ((Split-Path -Leaf $here) + "_task")

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Bad($t)     { Write-Host "  PROBLEM: $t" -ForegroundColor Red }
function Good($t)    { Write-Host "  ok: $t" -ForegroundColor Green }

Section "1. Does the task still exist?"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Bad "No task named '$TaskName'. It was deleted or never registered."
    Write-Host "  FIX: re-run install_windows.ps1 (command at the end of this output)."
} else {
    Good "task exists, State = $($task.State)"
    if ($task.State -eq "Disabled") { Bad "the task is DISABLED -- it will never fire." }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "  LastRunTime  : $($info.LastRunTime)"
    Write-Host "  NextRunTime  : $($info.NextRunTime)"
    Write-Host "  LastTaskResult: $($info.LastTaskResult)  $(
        switch ($info.LastTaskResult) {
            0       { '(success)' }
            267009  { '(SCHED_S_TASK_RUNNING -- running, NOT a failure)' }
            267011  { '(task has never run)' }
            1       { '(exit 1 -- the command died. Almost always a missing run_sync.cmd or a missing Python module: see section 3.)' }
            default { '' }
        })"
    if ($info.LastRunTime -lt (Get-Date).AddHours(-1)) {
        Bad "last run was over an hour ago on a 15-minute schedule."
    }
    Write-Host "`n  Action it actually runs:"
    $task.Actions | ForEach-Object {
        Write-Host "    Execute  : $($_.Execute)"
        Write-Host "    Arguments: $($_.Arguments)"
        Write-Host "    WorkingDir: $($_.WorkingDirectory)"
    }
}

Section "2. Do the files the task points at still exist?"
# A GitHub refresh of this folder deletes run_sync.cmd (the INSTALLER generates
# it; the repo does not carry it) and config.toml (gitignored). That is exactly
# how this died on 2026-08-14. Re-run install_windows.ps1 after EVERY update.
foreach ($f in @("sync.py", "tally_client.py", "config.toml")) {
    $p = Join-Path $here $f
    if (Test-Path $p) {
        Good "$f  ($((Get-Item $p).LastWriteTime))"
    } else {
        Bad "$f is MISSING"
        if ($f -eq "config.toml")  { Write-Host "    -> credentials are gone. It is gitignored, so a folder refresh removes it. Restore from $TaskDir\config.toml.backup, or rebuild from config.example.toml." }
    }
}

# The wrapper: current installs put it in $TaskDir, older ones in $here.
$cmdNew = Join-Path $TaskDir "run_sync.cmd"
$cmdOld = Join-Path $here "run_sync.cmd"
if (Test-Path $cmdNew) {
    Good "run_sync.cmd in the protected dir  ($((Get-Item $cmdNew).LastWriteTime))"
    if (Test-Path $cmdOld) { Write-Host "    NOTE: a stale copy also sits at $cmdOld and is NOT used. Delete it." }
} elseif (Test-Path $cmdOld) {
    Bad "run_sync.cmd is still inside the agent folder ($cmdOld)."
    Write-Host "    -> this is the OLD layout: the next GitHub refresh of this folder deletes it and the task dies silently. Re-run install_windows.ps1 to move it to $TaskDir."
} else {
    Bad "run_sync.cmd does not exist in EITHER location."
    Write-Host "    -> the task fires at a file that is not there and exits 1 before any Python runs. This is the classic silent death. Re-run install_windows.ps1."
}

Section "3. Can SYSTEM (not you) import requests?"
# The task runs as SYSTEM. A per-user pip install is invisible to SYSTEM, and
# pip will still say "already satisfied" to an admin. -s disables user
# site-packages so this sees what SYSTEM sees.
& python -s -c "import requests, sys; print('  ok: requests', requests.__version__, 'on', sys.version.split()[0])" 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Bad "SYSTEM cannot import requests -- every run dies before logging exists."
    Write-Host "    FIX: python -m pip install --ignore-installed requests"
}

Section "4. The only place a pre-logging crash is visible"
$out = Join-Path $TaskDir "task_out.txt"
if (-not (Test-Path $out)) { $out = Join-Path $here "task_out.txt" }
if (Test-Path $out) {
    Write-Host "  task_out.txt last written $((Get-Item $out).LastWriteTime)"
    Get-Content $out -Tail 30 | ForEach-Object { Write-Host "    $_" }
} else {
    Bad "no task_out.txt -- the task has not produced output since install."
}

Section "5. sync.log (only written once Python starts)"
$log = Join-Path $here "sync.log"
if (Test-Path $log) {
    Write-Host "  sync.log last written $((Get-Item $log).LastWriteTime)"
    Get-Content $log -Tail 15 | ForEach-Object { Write-Host "    $_" }
} else {
    Write-Host "  no sync.log -- consistent with dying before Python starts."
}

Section "6. Is Tally answering right now?"
try {
    $r = Invoke-WebRequest -Uri "http://localhost:9000" -Method Post -TimeoutSec 20 `
         -ContentType "text/xml" -Body "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>List of Companies</ID></HEADER><BODY><DESC></DESC></BODY></ENVELOPE>"
    Good "Tally answered on port 9000 ($($r.RawContentLength) bytes)"
} catch {
    Bad "Tally did not answer on localhost:9000 -- $($_.Exception.Message)"
    Write-Host "    Port open but no answer usually means a MODAL DIALOG on the Tally console. Look at the Tally window."
}

Section "7. Is the firewall rule still scoped to Tailscale?"
# A hosted image can have its firewall reset by provider maintenance. This rule
# is the ENTIRE access control on an unauthenticated gateway.
$fw = Get-NetFirewallRule -DisplayName "Tally 9000 Mac" -ErrorAction SilentlyContinue
if (-not $fw) {
    Bad "the 'Tally 9000 Mac' rule is gone. Port 9000 may now be unreachable, or worse, open to the provider's tenant network."
} else {
    $addr = $fw | Get-NetFirewallAddressFilter
    Write-Host "  RemoteAddress: $($addr.RemoteAddress)"
    if ($addr.RemoteAddress -eq "Any") { Bad "scoped to Any -- the gateway is exposed to the whole 10.10.0.0/16 tenant network. Re-scope to 100.64.0.0/10 on the Tailscale interface." }
    else { Good "scoped" }
}

Write-Host "`n=== To repair ===" -ForegroundColor Yellow
Write-Host "Re-running the installer is safe and idempotent -- it unregisters and"
Write-Host "re-registers, and regenerates run_sync.cmd. In an ADMIN PowerShell:"
Write-Host ""
Write-Host "  cd C:\tally_bridge" -ForegroundColor White
Write-Host "  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force" -ForegroundColor White
Write-Host "  .\install_windows.ps1 -SyncArgs '--company \"SN JAIN INDUSTRIES PVT LTD - (26-27)\"'" -ForegroundColor White
Write-Host ""
Write-Host "Call the .ps1 DIRECTLY, as above. Running it as"
Write-Host "  powershell -File .\install_windows.ps1 -SyncArgs '...'"
Write-Host "from inside PowerShell re-parses the quotes and fails with"
Write-Host "'the value of argument \"name\" is not valid'. (docs/STATUS.md still"
Write-Host "shows that broken form.)"
Write-Host ""
Write-Host "The installer REFUSES to register the task if its Tally/Frappe"
Write-Host "connectivity check fails. That refusal is correct -- fix the cause."
Write-Host ""
Write-Host "Then force one run and watch it:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Get-Content $TaskDir\task_out.txt -Tail 40 -Wait"
Write-Host ""
Write-Host "A healthy run prints:  Voucher requests: using 'filter_dotted' ..."
Write-Host "If it says 'No voucher request on this Tally build honours date ranges',"
Write-Host "STOP -- do not let it fall through to the whole-company fetch."
