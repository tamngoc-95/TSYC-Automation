<#
.SYNOPSIS
  Prepare (default) or register the TSYC read-only health-check tasks in
  Windows Task Scheduler. See docs/HEALTH_MONITORING.md.

.DESCRIPTION
  Two tasks, both running the repository virtual environment:
    TSYC Health Check     daily at -CheckTime: scripts/health_check.py --scheduled [--notify]
    TSYC Health Watchdog  daily at -WatchdogTime: scripts/health_check.py --watchdog [--notify]
                          (credential-free; detects a missed or failing check;
                          --notify reads only the webhook URL from .env.health)

  Existing \TSYC\ tasks are listed before anything is (re)registered.
  Register-ScheduledTask refuses to overwrite an existing task of the same
  name; unregister it first after reviewing the listed definition.

  No credential is passed to or stored in the task definition. The check
  reads its least-privilege keys from .env.health at run time.

  Without -Register this script only prints what it would register and
  validates the prerequisites. Registering is an owner-authorized step.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\scheduler\register_health_tasks.ps1
  powershell -ExecutionPolicy Bypass -File tools\scheduler\register_health_tasks.ps1 -Register -Notify
#>
[CmdletBinding()]
param(
    [switch]$Register,
    [switch]$Notify,
    [string]$CheckTime = "07:00",
    [string]$WatchdogTime = "19:00",
    [ValidateSet("Interactive", "S4U")]
    [string]$LogonType = "Interactive"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\health_check.py"
$HealthEnv = Join-Path $Root ".env.health"

$problems = @()
if (-not (Test-Path $Python)) { $problems += "Missing virtual environment: $Python" }
if (-not (Test-Path $Script)) { $problems += "Missing script: $Script" }
if (-not (Test-Path $HealthEnv)) { $problems += "Missing .env.health (read-only credentials; see docs/HEALTH_MONITORING.md)" }

$checkArgs = "`"$Script`" --scheduled"
$watchdogArgs = "`"$Script`" --watchdog"
if ($Notify) { $checkArgs += " --notify"; $watchdogArgs += " --notify" }
$tasks = @(
    @{ Name = "TSYC Health Check"; Args = $checkArgs; Time = $CheckTime },
    @{ Name = "TSYC Health Watchdog"; Args = $watchdogArgs; Time = $WatchdogTime }
)

$existing = @(Get-ScheduledTask -TaskPath "\TSYC\" -ErrorAction SilentlyContinue)
foreach ($e in $existing) { Write-Output ("Existing   : \TSYC\{0} ({1}) -> {2}" -f $e.TaskName, $e.State, (($e.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join "; ")) }

Write-Output "Repository : $Root"
Write-Output "Logon type : $LogonType (current user $env:USERNAME)"
foreach ($t in $tasks) { Write-Output ("Task       : {0} daily {1} -> {2} {3}" -f $t.Name, $t.Time, $Python, $t.Args) }
if ($problems.Count) { $problems | ForEach-Object { Write-Output "PREREQUISITE: $_" } }

if (-not $Register) {
    Write-Output "Dry run: nothing registered. Re-run with -Register after the owner authorizes it."
    exit ([int]($problems.Count -gt 0))
}
if ($problems.Count) { throw "Refusing to register: prerequisites missing." }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType $LogonType -RunLevel Limited
foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $Python -Argument $t.Args -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    Register-ScheduledTask -TaskName $t.Name -TaskPath "\TSYC\" -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description "TSYC read-only health monitoring (no writes)." | Out-Null
    Write-Output "Registered \TSYC\$($t.Name)"
}
