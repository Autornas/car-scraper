# Registers a Windows scheduled task that runs scripts\nightly.py every night.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_nightly_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_nightly_task.ps1 -At 04:00
#   powershell -ExecutionPolicy Bypass -File scripts\install_nightly_task.ps1 -Remove
#
# The task runs as you, only while you are signed in (no password is stored),
# and catches up at the next sign-in if the PC was off at the set time.
# See it, run it now or delete it in Task Scheduler under "Autoplius nightly refresh".

param(
    [string]$At = "03:30",
    [switch]$Remove
)

$TaskName = "Autoplius nightly refresh"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
    return
}

$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "No virtual environment at $Python. Create it first (see README, Setup)."
}

$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$(Join-Path $Project 'scripts\nightly.py')`"" -WorkingDirectory $Project
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Re-scrapes stale Autoplius models, runs the damage sweep and rebuilds the report." `
    -Force | Out-Null
Write-Host "Registered '$TaskName' daily at $At. Logs: $Project\data\logs\"
