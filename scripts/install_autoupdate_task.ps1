# Registers a Windows scheduled task that runs scripts\autoupdate.ps1 every few minutes.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_autoupdate_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_autoupdate_task.ps1 -Minutes 10
#   powershell -ExecutionPolicy Bypass -File scripts\install_autoupdate_task.ps1 -Remove
#
# Runs as you, hidden, from sign-in on. Log: data\logs\autoupdate.log

param(
    [int]$Minutes = 5,
    [switch]$Remove
)

$TaskName = "Car scraper auto-update"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
    return
}

$Project = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Project "scripts\autoupdate.ps1"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Project `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$Trigger.Repetition = $Repeat.Repetition
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Pulls new commits from GitHub and restarts the dashboard when code changed." `
    -Force | Out-Null
Write-Host "Registered '$TaskName' (every $Minutes min from sign-in). Run now: Start-ScheduledTask '$TaskName'"
