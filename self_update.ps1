<#
    self_update.ps1
    Refreshes the agent folder from GitHub, in place, without a human.

    This replaces the step that used to read: "download the ZIP from GitHub,
    extract it, and copy the folder onto C:\". That step was manual, it was
    skipped, and when it was done it silently deleted files the scheduled task
    depended on. See install_windows.ps1 for that history.

    Called by run_sync.cmd before every sync run. Its exit code is deliberately
    IGNORED by the wrapper: if an update cannot be applied, the sync must still
    run on the code already installed. A broken update must never become a
    stopped mirror.

    Safety properties, in the order they matter:

      1. It never deletes. Files are copied OVER the agent folder, so
         config.toml -- which is gitignored and therefore absent from the
         download -- survives. If it is missing anyway, the backup that
         install_windows.ps1 keeps in the task directory is restored.
      2. It compiles before it swaps. The downloaded Python is byte-compiled
         in the temp folder first. A push that does not parse is discarded
         there, and the live folder is never touched.
      3. It checks the commit before it downloads. Unchanged means one small
         API call and an exit, four times an hour.
      4. It corrects a reorder task scheduled before Tally opens, on every
         run, before that early exit -- so the box repairs a schedule nobody
         can log in to fix. See Repair-ReorderSchedule.

    Run by hand to force a refresh:
        powershell -ExecutionPolicy Bypass -File C:\tally_bridge\self_update.ps1
#>

param(
    [string]$AgentDir = "",
    [string]$TaskDir  = "",
    [string]$Repo     = "jainrishabh0333-rgb/tally_sync_agent",
    [string]$Branch   = "main",
    # Reorder-task schedule reconciliation. See Repair-ReorderSchedule below for
    # why a script that only copies files also corrects a scheduled task.
    [string]$ReorderTask   = "TallyBridgeReorder",
    [string]$ReorderAt     = "11:30",
    [string]$TallyOpensAt  = "10:00",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $AgentDir) { $AgentDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $TaskDir)  { $TaskDir  = Join-Path (Split-Path -Parent $AgentDir) ((Split-Path -Leaf $AgentDir) + "_task") }
if (-not (Test-Path $TaskDir)) { New-Item -ItemType Directory -Path $TaskDir -Force | Out-Null }

$logPath     = Join-Path $TaskDir "update.log"
$versionPath = Join-Path $AgentDir "VERSION.txt"

function Write-Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logPath -Value $line -Encoding UTF8
    # Keep the log from growing without bound; it is appended to four times an
    # hour forever otherwise.
    $lines = @(Get-Content -Path $logPath -ErrorAction SilentlyContinue)
    if ($lines.Count -gt 800) {
        Set-Content -Path $logPath -Value ($lines[-400..-1]) -Encoding UTF8
    }
}

# --- 0. correct a reorder task that fires before Tally is open --------------

function Repair-ReorderSchedule {
    <#
        Why this lives in the updater rather than in the installer that created
        the task: the installer only runs when a person runs it, and the whole
        point of this box is that nobody has to. TallyBridgeReorder was
        registered at 06:30 on 2026-08-23 and never completed a single run,
        because Tally is not open then -- measured over a week of sync logs,
        the first connection that succeeds on any day lands between 10:10 and
        10:55. The task failed, retried three times ten minutes apart, and gave
        up three hours before the engine came up. Nothing reported it: the
        reorder rows simply kept the date of the last manual export while every
        dashboard read healthy.

        Deliberately narrow. It corrects ONLY the condition that can never
        work -- a daily trigger before Tally opens -- and leaves any later time
        alone, so re-timing the task by hand is not undone on the next push.
        Everything it touches is wrapped: a failure here must never stop an
        update, still less a sync.
    #>
    param([string]$TaskName, [string]$At, [string]$OpensAt)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    if (-not $task.Triggers -or $task.Triggers.Count -lt 1) { return }

    $trigger = $task.Triggers[0]
    if (-not $trigger.StartBoundary) { return }

    $current = ([datetime]$trigger.StartBoundary).TimeOfDay
    $opens   = [timespan]$OpensAt
    if ($current -ge $opens) { return }

    $want    = [timespan]$At
    $stamp   = (Get-Date).Date.Add($want).ToString("yyyy-MM-ddTHH:mm:ss")
    $trigger.StartBoundary = $stamp

    # Widen the retry window while we are here. Three retries ten minutes apart
    # covers half an hour; Tally came up as late as 13:19 on one observed day,
    # so six retries twenty minutes apart -- two hours -- is what makes a fixed
    # daily time safe. Losing a run costs a day of stale numbers.
    $task.Settings.RestartCount    = 6
    $task.Settings.RestartInterval = "PT20M"

    $task | Set-ScheduledTask -ErrorAction Stop | Out-Null
    Write-Log ("FIXED  {0} fired at {1}, before Tally opens; moved to {2}" -f `
               $TaskName, $current.ToString("hh\:mm"), $At)
}

try {
    Repair-ReorderSchedule -TaskName $ReorderTask -At $ReorderAt -OpensAt $TallyOpensAt
} catch {
    Write-Log "WARN  could not check the $ReorderTask schedule: $($_.Exception.Message)"
}

# --- 1. what is live, and what is on GitHub ---------------------------------

$localSha = ""
if (Test-Path $versionPath) {
    $localSha = (Select-String -Path $versionPath -Pattern '^commit\s+(\S+)' |
                 ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
}

try {
    $api = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/commits/$Branch" `
                             -Headers @{ "User-Agent" = "tally-sync-self-update" } `
                             -TimeoutSec 30
    $remoteSha = $api.sha
} catch {
    Write-Log "SKIP  could not reach GitHub: $($_.Exception.Message)"
    exit 0
}

if ($remoteSha -eq $localSha -and -not $Force) {
    exit 0
}

$short   = $remoteSha.Substring(0, 7)
$wasShort = if ($localSha) { $localSha.Substring(0, 7) } else { "none" }
# Computed on its own line rather than inlined into the message: a subexpression
# containing its own double quotes inside a double-quoted string is legal but
# is exactly the kind of thing that fails on one PowerShell version and not
# another, and this file cannot be tested from the Mac.
$subject = ($api.commit.message -split "`n")[0]
Write-Log "UPDATE $wasShort -> $short  ($subject)"

# --- 2. download and stage --------------------------------------------------

$stage = Join-Path $env:TEMP ("tally_agent_" + $short)
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$zip = Join-Path $stage "agent.zip"
try {
    Invoke-WebRequest -Uri "https://github.com/$Repo/archive/$remoteSha.zip" `
                      -OutFile $zip -TimeoutSec 180 -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $stage -Force
} catch {
    Write-Log "ABORT download or extract failed: $($_.Exception.Message)"
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

$src = Get-ChildItem -Path $stage -Directory | Where-Object { $_.Name -like "tally_sync_agent-*" } | Select-Object -First 1
if (-not $src) {
    Write-Log "ABORT extracted archive has no agent folder"
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}

# --- 3. compile the new code BEFORE it goes anywhere near the live folder ----

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if ($python) {
    $pyFiles = Get-ChildItem -Path $src.FullName -Filter *.py | ForEach-Object { $_.FullName }
    & $python -m py_compile @pyFiles 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ABORT $short does not compile -- keeping the installed version"
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
        exit 1
    }
} else {
    Write-Log "WARN  python not on PATH; skipping the compile check"
}

# --- 4. copy over, never delete ---------------------------------------------

$copied = 0
foreach ($pattern in @("*.py", "*.ps1", "*.html", "*.toml", "requirements.txt")) {
    foreach ($f in Get-ChildItem -Path $src.FullName -Filter $pattern -File) {
        # config.toml holds this machine's Tally company and Frappe keys and is
        # never in the repo. config.example.toml is, and is safe to refresh.
        if ($f.Name -eq "config.toml") { continue }
        Copy-Item $f.FullName (Join-Path $AgentDir $f.Name) -Force
        $copied++
    }
}

$configPath = Join-Path $AgentDir "config.toml"
$configBackup = Join-Path $TaskDir "config.toml.backup"
if (-not (Test-Path $configPath) -and (Test-Path $configBackup)) {
    Copy-Item $configBackup $configPath -Force
    Write-Log "RESTORED config.toml from the backup in $TaskDir"
}

@"
commit $remoteSha
branch $Branch
applied $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
files $copied
"@ | Set-Content -Path $versionPath -Encoding UTF8

Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Write-Log "OK    $short applied, $copied files"
exit 0
