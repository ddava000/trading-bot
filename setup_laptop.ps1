# setup_laptop.ps1 — one-shot installer for the Robinhood laptop bot (Windows).
#
#   Set-ExecutionPolicy -Scope Process Bypass -Force; irm https://raw.githubusercontent.com/ddava000/trading-bot/main/setup_laptop.ps1 -OutFile setup.ps1; .\setup.ps1
#
# Safe to re-run at any point; every step is idempotent.
# You will see exactly TWO browser approvals: Claude sign-in, and GitHub push auth.
#
# Windows PowerShell 5.1 notes that shaped this script:
#   * git/winget write normal progress to STDERR, and with ErrorActionPreference=Stop
#     PowerShell turns that into a fatal NativeCommandError. So: Continue + explicit
#     $LASTEXITCODE checks, and --quiet flags instead of stderr redirection.
#   * `python` may be the Microsoft Store ALIAS STUB, which exists on PATH but is not
#     an interpreter. Presence is not enough - the version string must be verified.

param(
    [string]$Account = "",
    [string]$RepoDir = "$env:USERPROFILE\trading-bot",
    # -Live is the ONE explicit act that arms real trading. Without it the task
    # runs --dry, so a plain re-run can never silently put the bot into the market.
    [switch]$Live
)

$ErrorActionPreference = "Continue"
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !!  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "`nSTOPPED: $m" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    # Must PROVE it is a real interpreter - the Store stub answers but isn't one.
    foreach ($c in @("python", "python3", "py")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $v = (& $c --version) 2>&1 | Out-String
            if ($v -match "Python 3\.\d") { return $cmd.Source }
        }
    }
    foreach ($pat in @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                       "$env:ProgramFiles\Python3*\python.exe",
                       "${env:ProgramFiles(x86)}\Python3*\python.exe",
                       "C:\Python3*\python.exe")) {
        $f = Get-ChildItem $pat -ErrorAction SilentlyContinue |
             Sort-Object FullName | Select-Object -Last 1
        if ($f) {
            $v = (& $f.FullName --version) 2>&1 | Out-String
            if ($v -match "Python 3\.\d") { return $f.FullName }
        }
    }
    return $null
}

Step "1/7  Checking Git and Python"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Warn "Git missing - installing (this takes a minute)"
    winget install --id Git.Git -e --source winget --silent `
        --accept-package-agreements --accept-source-agreements | Out-Null
    Refresh-Path
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die "Git is installed but not on PATH yet. Close this window, open a NEW PowerShell, re-run."
}
Ok (git --version)

$py = Find-Python
if (-not $py) {
    Warn "Python missing (the 'python' command on PATH is the Microsoft Store stub, not a real interpreter)"
    Warn "Installing Python 3.12 - this takes a couple of minutes"
    winget install --id Python.Python.3.12 -e --source winget --silent `
        --accept-package-agreements --accept-source-agreements | Out-Null
    Refresh-Path
    $py = Find-Python
}
if (-not $py) {
    Die "Python installed but not detectable yet. Close this window, open a NEW PowerShell, re-run this script."
}
Ok "python at $py  ($((& $py --version) 2>&1))"

Step "2/7  Getting the bot code"
if (Test-Path (Join-Path $RepoDir ".git")) {
    Push-Location $RepoDir
    git pull --rebase --autostash --quiet
    Pop-Location
    Ok "updated $RepoDir"
} else {
    if ((Test-Path $RepoDir) -and (Get-ChildItem $RepoDir -Force | Measure-Object).Count -gt 0) {
        Warn "$RepoDir exists but is not a git repo - moving it aside"
        Move-Item $RepoDir "$RepoDir.old-$(Get-Date -Format yyyyMMddHHmmss)"
    }
    git clone --quiet https://github.com/ddava000/trading-bot.git $RepoDir
    if ($LASTEXITCODE -ne 0) { Die "git clone failed (exit $LASTEXITCODE). Check the network and re-run." }
    Ok "cloned to $RepoDir"
}
Set-Location $RepoDir
# tzdata is required on Windows: unlike Linux/CI, Windows ships no system IANA
# tz database, so ZoneInfo("America/New_York") in alpaca_bot dies without it.
& $py -m pip install --quiet --upgrade requests tzdata
if ($LASTEXITCODE -ne 0) { Warn "pip install had trouble - continuing, the smoke test will catch it" }
else { Ok "python deps ready" }

Step "3/7  Locating the Claude CLI"
# Having Claude Desktop installed and open is NOT enough: Desktop only provisions
# its bundled claude.exe after Claude Code has actually run inside it, so a fresh
# Desktop install has no CLI at all. Check every real location, then install.
function Find-Claude {
    foreach ($p in @("$env:USERPROFILE\.local\bin\claude.exe",
                     "$env:LOCALAPPDATA\Programs\claude\claude.exe")) {
        if (Test-Path $p) { return $p }
    }
    $d = Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime | Select-Object -Last 1
    if ($d) { return $d.FullName }
    $c = Get-Command claude -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}
$claude = Find-Claude
if ($claude) {
    $ver = (& $claude --version) 2>&1 | Out-String
    if ($ver -notmatch "\d+\.\d+") {
        Warn "found $claude but it reported no version - reinstalling"
        $claude = $null
    }
}
if (-not $claude) {
    Warn "Claude CLI not installed - installing the official one (about a minute)"
    irm https://claude.ai/install.ps1 | iex
    Refresh-Path
    $claude = Find-Claude
}
if (-not $claude) {
    Die "Claude CLI install did not land on PATH. Close this window, open a NEW PowerShell, re-run."
}
Ok "$claude  ($((((& $claude --version) 2>&1) | Out-String).Trim()))"

Step "4/7  Signing the Claude CLI in   << BROWSER APPROVAL 1 of 2 >>"
# Claude Desktop's login does NOT carry to the CLI - it keeps separate credentials.
$probe = (& $claude -p "Reply with exactly: READY") 2>&1 | Out-String
if ($probe -match "READY") {
    Ok "CLI already signed in"
} else {
    Write-Host "  A browser window will open. Click Approve, then return here." -ForegroundColor White
    & $claude auth login
    $probe = (& $claude -p "Reply with exactly: READY") 2>&1 | Out-String
    if ($probe -notmatch "READY") { Die "Claude CLI sign-in did not complete. Re-run this script to retry." }
    Ok "CLI signed in"
}

Step "5/7  Proving git push works   << BROWSER APPROVAL 2 of 2 >>"
# The bot pushes status so it can be monitored remotely. Trigger GitHub auth NOW,
# during setup, rather than letting it fail silently at 3am.
git config user.name  "rh-laptop-bot"
git config user.email "rh-laptop-bot@users.noreply.github.com"
"laptop online $(Get-Date -Format s)" | Out-File -Encoding utf8 rh_laptop_online.txt
git add rh_laptop_online.txt
git commit --quiet -m "rh laptop: setup handshake"
Write-Host "  If a GitHub sign-in window opens, approve it." -ForegroundColor White
git push --quiet
if ($LASTEXITCODE -ne 0) {
    git pull --rebase --autostash --quiet
    git push --quiet
}
if ($LASTEXITCODE -ne 0) { Warn "push failed - remote monitoring stays blind until fixed (bot still trades fine)" }
else { Ok "push works - remote monitoring live" }

Step "6/7  Writing config and smoke-testing (DRY - places nothing)"
if (-not $Account) { $Account = Read-Host "  Robinhood AGENTIC account number" }
# WriteAllText with an explicit no-BOM encoder, NOT Out-File -Encoding utf8:
# PS 5.1's "utf8" means utf8-WITH-BOM, and the BOM makes Python's json.load fail.
$cfgJson = @{ account = $Account.Trim(); claude = $claude } | ConvertTo-Json
[System.IO.File]::WriteAllText(
    (Join-Path $RepoDir "rh_config.json"), $cfgJson,
    (New-Object System.Text.UTF8Encoding $false))
Ok "rh_config.json written (gitignored - never leaves this laptop)"
& $py rh_bot.py --selftest
if ($LASTEXITCODE -ne 0) { Die "strategy selftest FAILED - stopping before anything can trade." }
& $py rh_daemon.py --dry --once
if ($LASTEXITCODE -ne 0) { Warn "dry run returned $LASTEXITCODE - review the output above" }
else { Ok "smoke test complete - no orders were placed" }

Step "7/7  Registering the always-on task"
$taskName = "rh-trading-bot"
# DRY is set only by sys.argv, so a task registered without --dry goes live at
# the next logon whether or not anyone chose that. Default to --dry and let
# -Live be the single explicit act that arms real trading.
$dryFlag = if ($Live) { "" } else { " --dry" }
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$py`" rh_daemon.py$dryFlag >> rh_daemon.log 2>&1`"" -WorkingDirectory $RepoDir
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# -AtStartup needs admin (it runs before any user logs on); -AtLogOn does not.
# Try boot+logon first, fall back to logon-only so an unelevated run still works.
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# Watchdog. RestartCount only covers the task FAILING; a daemon killed with Ctrl+C
# or a closed console exits cleanly, leaves the task "Ready", and nothing brings it
# back. That happened on 2026-07-23 with seven live positions and no protective pass
# running for seventeen minutes. This trigger re-fires every 5 minutes forever, and
# MultipleInstances IgnoreNew makes each fire a no-op while the bot is already up,
# so it costs nothing and only acts when the daemon is actually gone.
function New-WatchdogTrigger {
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
    return $t
}

$registered = $false
if ($isAdmin) {
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger @((New-ScheduledTaskTrigger -AtLogOn), (New-ScheduledTaskTrigger -AtStartup),
                       (New-WatchdogTrigger)) `
            -Settings $settings -Description "Robinhood laptop trading bot (always on)" `
            -ErrorAction Stop | Out-Null
        $registered = $true
        Ok "task '$taskName' registered - starts at logon AND boot, restarts if it dies"
    } catch {
        Warn "boot-trigger registration failed ($($_.Exception.Message)) - trying logon-only"
    }
}
if (-not $registered) {
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action `
            -Trigger @((New-ScheduledTaskTrigger -AtLogOn), (New-WatchdogTrigger)) `
            -Settings $settings -Description "Robinhood laptop trading bot (logon)" `
            -ErrorAction Stop | Out-Null
        $registered = $true
        Ok "task '$taskName' registered - starts AT LOGON, restarts if it dies"
        if (-not $isAdmin) {
            Warn "NOT registered for boot (needs admin). The bot starts when you log in."
            Warn "For boot-start too, re-run this script in an ELEVATED PowerShell."
        }
    } catch {
        Warn "could not register the scheduled task: $($_.Exception.Message)"
        Warn "THE BOT WILL NOT AUTO-START. Re-run this script in an elevated PowerShell,"
        Warn "or start it by hand:  $py rh_daemon.py"
    }
}

$haltFile = Join-Path $RepoDir "rh_HALT"
if ($Live) {
    # Arming live has to clear the kill switch too, otherwise the task starts
    # and immediately pauses, which looks identical to a broken daemon.
    if (Test-Path $haltFile) { Remove-Item $haltFile -Force; Ok "kill switch cleared" }

    # A dry ledger records SIMULATED fills the real account never had. Carried into
    # live, session-open reconcile sees positions the broker denies, the corrupt-
    # snapshot guard correctly refuses it, and every pass skips forever: a bot that
    # looks healthy and never trades. Gated on the dry flag in rh_status.json so
    # this can never wipe a genuine live ledger and its hold basis/peak history.
    $statusFile = Join-Path $RepoDir "rh_status.json"
    $ledgerFile = Join-Path $RepoDir "rh_ledger.json"
    if ((Test-Path $statusFile) -and (Test-Path $ledgerFile)) {
        $wasDry = $false
        try { $wasDry = [bool](Get-Content $statusFile -Raw | ConvertFrom-Json).dry } catch { $wasDry = $false }
        if ($wasDry) {
            Remove-Item $ledgerFile -Force
            Ok "simulated dry ledger cleared - live starts from broker truth"
        } else {
            Warn "existing LIVE ledger kept (real positions and hold history preserved)"
        }
    }

    if ($registered) {
        Start-ScheduledTask -TaskName $taskName
        Ok "task started - the bot is LIVE and will place real orders"
    }
} elseif (Test-Path $haltFile) {
    Warn "rh_HALT is present, so the daemon will pause on its next pass."
}

$mode = if ($Live) { "LIVE - placing REAL orders" } else { "DRY - places nothing" }
Write-Host @"

================================================================
  DONE. Mode: $mode

  Repo:    $RepoDir
  Log:     $RepoDir\rh_daemon.log
  STOP IT: create a file named  rh_HALT  in the repo folder
  GO LIVE: re-run this script with  -Live
================================================================

"@ -ForegroundColor Green
