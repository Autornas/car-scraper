# Pulls new commits from GitHub and restarts the dashboard when code changed.
# Run by the "Car scraper auto-update" task (see install_autoupdate_task.ps1);
# output goes to data\logs\autoupdate.log.
#
# Safe by design: fast-forward only, skipped if tracked files have local edits,
# and rolled back if the new code doesn't start.

$ErrorActionPreference = "Stop"
$env:GIT_TERMINAL_PROMPT = "0"
$Project = Split-Path -Parent $PSScriptRoot
Set-Location $Project
$LogDir = Join-Path $Project "data\logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "autoupdate.log"
$Task = "Car scraper dashboard"
$Python = Join-Path $Project ".venv\Scripts\python.exe"

function Log($msg) {
    "$(Get-Date -Format 's')  $msg" | Add-Content -Path $Log -Encoding utf8
    if ((Get-Item $Log).Length -gt 1MB) {
        Get-Content $Log -Tail 200 | Set-Content "$Log.tmp" -Encoding utf8
        Move-Item "$Log.tmp" $Log -Force
    }
}

function Stop-Dashboard {
    Stop-ScheduledTask $Task -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*scraper_dashboard.py*" } |
        ForEach-Object { & taskkill /PID $_.ProcessId /T /F | Out-Null }
    Start-Sleep 2
}

function Start-Dashboard {
    Start-ScheduledTask $Task
    for ($i = 0; $i -lt 30; $i++) {          # up to ~60 s for the site to answer
        Start-Sleep 2
        try {
            Invoke-WebRequest "http://127.0.0.1:8765/api/me" -UseBasicParsing -TimeoutSec 3 | Out-Null
            return $true
        } catch {
            # any HTTP answer (even 403 in tailscale mode) means the site is up
            if ($_.Exception.Response) { return $true }
        }
    }
    return $false
}

try {
    git fetch --quiet
    $local = (git rev-parse HEAD).Trim()
    $remote = (git rev-parse "@{u}").Trim()
    if ($local -eq $remote) { return }

    if (git status --porcelain --untracked-files=no) {
        Log "update available ($($remote.Substring(0,7))) but tracked files have local edits; skipped"
        return
    }
    $changed = @(git diff --name-only $local $remote)
    git merge --ff-only $remote | Out-Null
    Log "updated $($local.Substring(0,7)) -> $($remote.Substring(0,7)): $($changed -join ', ')"

    if ($changed -contains "requirements.txt") {
        & $Python -m pip install -r requirements.txt --quiet
        Log "installed requirements"
    }
    if (-not ($changed | Where-Object { $_ -match '\.py$|^requirements\.txt$|^config\.toml$|^data/.*\.(json|txt)$' })) {
        Log "no code changes; no restart needed"
        return
    }

    Log "restarting dashboard"
    Stop-Dashboard
    if (Start-Dashboard) {
        Log "dashboard is up on the new version"
    } else {
        Log "NEW VERSION DID NOT START; rolling back to $($local.Substring(0,7))"
        Stop-Dashboard
        git reset --hard $local | Out-Null
        if (Start-Dashboard) { Log "rolled back; dashboard is up on the old version" }
        else { Log "ROLLBACK FAILED TO START; check data\logs\dashboard.log" }
    }
} catch {
    Log "ERROR: $($_.Exception.Message)"
}
