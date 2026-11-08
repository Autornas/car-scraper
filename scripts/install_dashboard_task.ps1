# Registers a Windows scheduled task that starts the website + watcher when you sign in.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_dashboard_task.ps1 -Remove
#
# Runs as you, hidden (no terminal window), and restarts up to 3 times if it crashes.
# Stop it from Task Scheduler ("Car scraper dashboard") or with: Stop-ScheduledTask "Car scraper dashboard"

param([switch]$Remove)

$TaskName = "Car scraper dashboard"

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

# Hidden PowerShell runs python with a real console (pythonw has none and failed),
# and keeps its output in data\logs\dashboard.log.
$Script = Join-Path $Project 'scripts\scraper_dashboard.py'
$Log = Join-Path $Project 'data\logs\dashboard.log'
$Command = "New-Item -ItemType Directory -Force (Split-Path '$Log') | Out-Null; " +
    "& '$Python' '$Script' --no-browser *> '$Log'"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Project `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"$Command`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Starts the car scraper website and watcher at sign-in." `
    -Force | Out-Null
Write-Host "Registered '$TaskName' (starts at sign-in). Start it now: Start-ScheduledTask '$TaskName'"
