# setup_laptop.ps1 — one-shot installer for the Robinhood laptop bot (Windows).
#
#   irm https://raw.githubusercontent.com/ddava000/trading-bot/main/setup_laptop.ps1 -OutFile setup.ps1; .\setup.ps1
#
# Installs Git + Python if missing, clones the repo, wires the config, signs the
# Claude CLI in, proves a git push works, smoke-tests in DRY mode (places nothing),
# and registers an always-on task that restarts itself and survives reboots.
#
# You will see exactly TWO browser approvals: Claude sign-in, and GitHub push auth.

param(
    [string]$Account = "",
    [string]$RepoDir = "$env:USERPROFILE\trading-bot"
)

$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !!  $m" -ForegroundColor Yellow }

Step "1/7  Checking Git and Python"
function Have($cmd) { $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue) }
if (-not (Have git)) {
    Warn "Git missing - installing"
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
if (-not (Have python)) {
    Warn "Python missing - installing"
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
if (-not (Have git))    { throw "Git still not on PATH - close this window, open a new PowerShell, re-run." }
if (-not (Have python)) { throw "Python still not on PATH - close this window, open a new PowerShell, re-run." }
Ok "git $(git --version) / python $(python --version)"

Step "2/7  Getting the bot code"
if (Test-Path "$RepoDir\.git") {
    Push-Location $RepoDir; git pull --rebase --autostash 2>&1 | Out-Null; Pop-Location
    Ok "updated $RepoDir"
} else {
    git clone https://github.com/ddava000/trading-bot.git $RepoDir 2>&1 | Out-Null
    Ok "cloned to $RepoDir"
}
Set-Location $RepoDir
python -m pip install --quiet --upgrade requests | Out-Null
Ok "python deps ready"

Step "3/7  Locating the Claude CLI"
$claude = Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -ErrorAction SilentlyContinue |
          Sort-Object { [version]($_.Directory.Name) } -ErrorAction SilentlyContinue |
          Select-Object -Last 1 -ExpandProperty FullName
if (-not $claude) { $claude = (Get-Command claude -ErrorAction SilentlyContinue).Source }
if (-not $claude) { throw "Claude CLI not found. Open Claude Desktop once, then re-run this script." }
Ok $claude

Step "4/7  Signing the Claude CLI in   << BROWSER APPROVAL 1 of 2 >>"
# Desktop app login does NOT carry to the CLI - it keeps its own credentials.
$authed = $false
try {
    $probe = & $claude -p "Reply with exactly: READY" 2>&1 | Out-String
    if ($probe -match "READY") { $authed = $true }
} catch { }
if ($authed) {
    Ok "CLI already signed in"
} else {
    Write-Host "  A browser window will open. Click Approve, then come back here." -ForegroundColor White
    & $claude auth login
    $probe = & $claude -p "Reply with exactly: READY" 2>&1 | Out-String
    if ($probe -notmatch "READY") { throw "Claude CLI sign-in did not complete. Re-run this script." }
    Ok "CLI signed in"
}

Step "5/7  Proving git push works   << BROWSER APPROVAL 2 of 2 >>"
# The bot pushes its status so it can be monitored remotely. Trigger GitHub's
# auth now, during setup, instead of letting it fail silently at 3am.
git config user.name  "rh-laptop-bot" | Out-Null
git config user.email "rh-laptop-bot@users.noreply.github.com" | Out-Null
"laptop online $(Get-Date -Format s)" | Out-File -Encoding utf8 rh_laptop_online.txt
git add rh_laptop_online.txt | Out-Null
git commit -m "rh laptop: setup handshake" 2>&1 | Out-Null
Write-Host "  If a GitHub sign-in window opens, approve it." -ForegroundColor White
git push 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    git pull --rebase --autostash 2>&1 | Out-Null
    git push 2>&1 | Out-Null
}
if ($LASTEXITCODE -ne 0) { Warn "push failed - remote monitoring will be blind until this is fixed" }
else { Ok "push works - remote monitoring live" }

Step "6/7  Writing config and smoke-testing (DRY - places nothing)"
if (-not $Account) { $Account = Read-Host "  Robinhood AGENTIC account number" }
@{ account = $Account; claude = $claude } | ConvertTo-Json | Out-File -Encoding utf8 rh_config.json
Ok "rh_config.json written (gitignored - never leaves this laptop)"
python rh_bot.py --selftest
python rh_daemon.py --dry --once
Ok "smoke test complete - no orders were placed"

Step "7/7  Registering the always-on task"
$taskName = "rh-trading-bot"
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c python rh_daemon.py >> rh_daemon.log 2>&1" -WorkingDirectory $RepoDir
$triggers = @((New-ScheduledTaskTrigger -AtLogOn), (New-ScheduledTaskTrigger -AtStartup))
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "Robinhood laptop trading bot (always on)" | Out-Null
Ok "task '$taskName' registered - starts at logon and at boot, restarts if it dies"

Write-Host @"

================================================================
  DONE. The bot is installed but NOT trading yet (safe by design).
  It is currently in DRY mode until you say go.

  Repo:    $RepoDir
  Log:     $RepoDir\rh_daemon.log
  STOP IT: create a file named  rh_HALT  in the repo folder
================================================================

"@ -ForegroundColor Green
