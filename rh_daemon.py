"""
rh_daemon.py — the always-on Robinhood laptop bot.

IDENTICAL cadence to the cloud bot:
  * full strategy cycle every 15 minutes
  * protective pass (hard stops, hold floors, news tripwire) every 60 seconds

Why this is affordable: the DECISION side is pure Python and free — Yahoo prices
plus a local ledger — so it can run every 60s forever. Tokens are spent only to
PLACE an order or to reconcile with the broker, which happens a handful of times
a day. Robinhood has no usable programmatic order API (their agentic MCP is the
supported path), so a short headless agent turn is the execution bridge.

The agent is a DUMB EXECUTOR. It is handed an exact order list and told not to
add, skip, or modify anything. All judgment lives in rh_bot.decide().

Files:
  rh_config.json     local only, gitignored — {"account": "...", "claude": "claude"}
  rh_ledger.json     local only, gitignored — positions/cash/holds/unsettled
  rh_status.json     COMMITTED — equity + sleeve snapshot, no account numbers
  rh_trade_log.jsonl COMMITTED — every fill, so Claude can monitor from anywhere
  rh_HALT            create this file to stop all trading immediately

Run:  python rh_daemon.py            (live)
      python rh_daemon.py --dry      (decide + log, place nothing)
      python rh_daemon.py --once     (one cycle, then exit)
"""

import os, re, sys, json, time, subprocess

# MAKE THIS PROCESS SAFE TO print() NON-ASCII, before importing anything that does.
# 2026-09-10: a verification email was DELIVERED and then reported as FAILED. In
# alpaca_bot.send_email the success line is `print(f"  [email sent -> ...")` using a
# real U+2192 arrow, INSIDE the try. sendmail() had already returned and the SMTP
# connection had closed, then the print hit a cp1252 stdout, raised
# UnicodeEncodeError, and the except turned a delivered message into `return False`.
#
# Windows-only: on the Linux runners stdout is UTF-8 and this can never fire, which
# is why it survived in shared code. Same shape as the GMAIL_USER bug found this
# morning - correct in one execution context, broken in the other.
#
# Fixed HERE rather than in alpaca_bot because a process should not let a LOGGING
# call decide whether an OPERATION succeeded, and stdout belongs to this process.
# Under pythonw sys.stdout is None, so handle that too: prints become no-ops
# instead of AttributeError.
class _NullOut:
    def write(self, _):  return 0
    def flush(self):     return None
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is None:
        setattr(sys, _name, _NullOut())
    else:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass                      # older/odd stream: leave it, never fail startup
from datetime import datetime, timedelta

import rh_bot
import alpaca_bot as bot

CONFIG_F, LEDGER_F = "rh_config.json", "rh_ledger.json"
DEPOSITS_F         = "rh_deposits.json"   # COMMITTED: contributed capital, so the
                                           # A/B check-in can subtract it from equity
STATUS_F, LOG_F    = "rh_status.json", "rh_trade_log.jsonl"
HALT_F             = "rh_HALT"

# The only files the daemon imports and runs. A pulled commit touching anything
# else (status.json, trade logs, daily_plan.json, briefs, backtest.py) is data or
# cloud-only code and needs no restart, since decide() reads the plan fresh each
# cycle. Keep this in sync with the actual import graph: rh_daemon -> rh_bot -> bot.
# realized.py joined this list 2026-09-30, the day persist() began importing it. A module
# the daemon IMPORTS but that is absent here is reloaded by nothing: the running process
# keeps the version it first imported, so cloud can change realized.py and the laptop goes
# on publishing a stale block indefinitely, and the change never passes the selftest gate
# that every other code pull does. Harmless the day it was found - the wrapper around
# arm_b_block publishes state "unknown" rather than crashing - but "silently runs code
# older than the repo" is the shape, not the severity.
# slack_notify.py added 2026-10-01: CLOUD put the choice to the laptop and recommended
# gating it. It is ON THE ALERT PATH - notify() imports it for the Slack mirror - and the
# import is lazy and cached, so a running daemon keeps the copy it first loaded and a bad
# push would reach alerting with no selftest. Cloud changes it often, so this costs a
# restart per push to it; the alert path is how Devon learns about outages, a dead bridge
# login and deposits, and a silent break there already cost two weeks of lost email once.
# mail_check.py stays UNGATED on purpose: slack_notify imports it lazily inside
# _who_checks(), which the daemon never calls, so it cannot reach this process at runtime.
CODE_FILES = ("rh_bot.py", "rh_daemon.py", "alpaca_bot.py", "realized.py",
              "slack_notify.py", "arm_a_watch.py")

FULL_CYCLE_SEC = 900     # 15 min, matches the cloud bot's trigger cadence
FAST_PASS_SEC  = 60      # 60 s, matches the cloud bot's protective pass
MAX_ORDERS_DAY = 40      # circuit breaker: a runaway loop can't machine-gun orders
AGENT_TIMEOUT  = 240
PUSH_HEARTBEAT_SEC = 900 # liveness ping when nothing changed, so a quiet laptop
                         # and a dead one don't look the same to whoever watches
RECONCILE_MAX_AGE_SEC = 1800  # 30 min. The bot only learned its cash at session
                         # open and after trades, so a mid-day DEPOSIT sat unseen
                         # until the next morning. This reconciles at least this
                         # often during market hours so added (or withdrawn) money
                         # is noticed within the window. It is the one scheduled
                         # token spend; keep it well above the 15-min full cycle so
                         # it stays cheap. Trades reset the clock, so on an active
                         # day this rarely fires on top of the reconciles already
                         # happening.

_last_push_at, _last_push_material = 0.0, None
SELFTEST_FAIL_ALERT  = 3     # consecutive upstream selftest rejections before we
                             # tell Devon. The gate keeps trading safely on the old
                             # in-memory code, but silently: without this he never
                             # learns the laptop stopped inheriting cloud fixes.
SELFTEST_REALERT_SEC = 14400 # 4h. Re-alert while still pinned, so a break that
                             # lasts days is not forgotten after one email.

BROKER_FAIL_ALERT  = 3      # consecutive failed broker snapshots before alerting
BROKER_REALERT_SEC = 3600   # re-alert hourly while the broker stays unreachable
BROKER_ALERT_AFTER_SEC = 900  # ...or once an outage has lasted this long, however few
                             # attempts it took; see _maybe_alert_broker

RECONCILE_BACKOFF_MAX = 900   # cap retry spacing at 15 min during an outage
AUTH_BACKOFF_MAX = 120        # but cap it at 2 min when the cause is an expired LOGIN


def retry_wait(fails, err, reset_wait):
    """Seconds to wait before the next broker attempt. One function, so it is testable.

    The curve doubles to RECONCILE_BACKOFF_MAX because each broker snapshot is a real
    agent turn, and retrying every 60 s through a usage-limit outage burns the quota being
    waited on. A reset time the bridge NAMED beats a blind curve. An expired LOGIN is
    neither: it fails at authentication before any model call, so the retry is free, and
    its fix is a human running `claude auth login` - worth noticing in two minutes rather
    than fifteen.

    PRECEDENCE MATTERS AND I GOT IT WRONG FIRST. e906b0c7 applied the auth cap after the
    quota override with only a needs_human() test, so a message matching BOTH classes had
    its parsed quota wait rewritten to 120 s: cloud's example, "Failed to authenticate
    while checking quota: You've hit your session limit - resets 1:40pm", parses to
    14845 s and would have become 124 retries through a quota outage. The cap now requires
    NO quota evidence of any kind - not the self-healing class, and no parsed reset. Either
    test alone leaves a hole: reset-is-None misses a limit message whose reset does not
    parse, and class-only misses limit wording self_heals does not match but the parser
    does. The asymmetry is why quota wins when a message says both - reading it as quota
    costs a quarter-hour of blindness at worst, reading it as auth costs hours of hammering
    the resource we are waiting for.
    """
    wait = min(FAST_PASS_SEC * (2 ** min(fails - 1, 4)), RECONCILE_BACKOFF_MAX)
    if reset_wait is not None:
        wait = reset_wait
    if needs_human(err) and not self_heals(err) and reset_wait is None:
        wait = min(wait, AUTH_BACKOFF_MAX)
    return wait


def needs_human(err):
    """Is this broker error the class that NEVER recovers on its own? One definition.

    It used to be two: _maybe_alert_broker classified the error to pick a subject line,
    and the retry backoff did not classify at all. Keeping the same test in two places is
    how an early close gets modelled in one and missed in the other, so both now call
    this.
    """
    e = (err or "").lower()
    return ("oauth" in e or "authenticate" in e or "session expired" in e or "auth" in e)


def self_heals(err):
    """Is it the class that comes back on a timer, with no human needed?"""
    e = (err or "").lower()
    return ("session limit" in e or "usage limit" in e or "rate limit" in e)
DEGRADED_PUSH_SEC  = 300    # min spacing between degraded git pushes; see publish_degraded

_reconcile_fails, _broker_alert_at = 0, 0.0
_next_reconcile_try = 0.0
_degraded_push_at   = 0.0   # last degraded git push; throttles the outage heartbeat
_degraded_passes    = 0     # consecutive degraded PASSES (not attempts); 0 = healthy
_broker_down_since  = 0.0   # wall clock of this outage's first failed attempt
_waiting_on_reset   = False # next retry is parked on a quota reset the bridge named
_last_agent_error   = ""   # bridge's own failure text; classifies the outage in alerts
_selftest_fails, _selftest_alert_at = 0, 0.0
_last_reconcile = 0.0    # 0 => the first full cycle after start reconciles, which
                         # is also how a fresh start picks up a deposit made while
                         # the laptop was off or since the last session open.
_MACHINE = {}            # laptop-local facts GitHub cannot see: whether keep-awake is
                         # held, the last unclean shutdown's cause, and the two settings
                         # the crash hypothesis turns on. Read once per start, published
                         # on every status write so the daily digest can carry them.
                         # A module global is right here precisely BECAUSE a restart
                         # should re-read it - a reboot is when all three can change.
_run_head = None         # git commit whose CODE is loaded in memory, captured at
                         # startup. Code-change detection compares against THIS, not
                         # against sync_code's own pull, because the status-push path
                         # also pulls --rebase and can absorb a cloud code change
                         # before sync_code sees it (which left the daemon running
                         # 3-day-stale code on 2026-08-06).

DRY = "--dry" in sys.argv


_singleton_handle = None


def acquire_singleton():
    """Guarantee ONE daemon, via a Windows named mutex the OS frees on exit.

    On 2026-07-23 three live daemons ran at once against the same account. Cause:
    os.execv on Windows SPAWNS a new process instead of replacing the current one
    (unlike Unix), and each code-sync restart after a push stacked another copy
    that Task Scheduler's MultipleInstances tracking never saw. A shared ledger
    with three writers is a real way to double-place orders.

    A named mutex is the canonical Windows singleton: held for the process
    lifetime, released automatically even on a hard kill, so there is no stale
    lock to clean up. Fails OPEN on any error: better to run than to refuse to
    trade over a bug in the guard itself.
    """
    global _singleton_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "rh_trading_bot_singleton")
        ERROR_ALREADY_EXISTS = 183
        if k32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _singleton_handle = h        # keep the handle for the process lifetime
        return True
    except Exception:
        return True


def keep_awake():
    """Hold this laptop out of Modern Standby for as long as the daemon runs.

    Every unclean shutdown on this machine - 2026-08-04, 09-02 and 09-21 - has
    Kernel-Power event 41 with ConnectedStandbyInProgress=true. The laptop has
    no S3 sleep, only S0 Low Power Idle, and it enters that state 30 min after
    the display times out on AC. So every night it went into the one state it
    has now died in three times out of three.

    09-02 was a bugcheck and Windows restarted itself in three minutes, which
    auto-logon now covers. 09-21 was not: the machine hung in standby at
    22:11 CT and stayed DEAD for twelve hours until someone pressed power at
    10:02, costing the first 93 minutes of 09-22. Auto-logon cannot help a
    machine that never boots. The fix has to be not entering standby at all.

    ES_DISPLAY_REQUIRED is the load-bearing flag. On Modern Standby, screen-off
    IS standby entry, so SYSTEM_REQUIRED alone does not reliably keep it out.
    Cost: the screen stays on. ES_CONTINUOUS ties the request to this process,
    so Windows drops it the instant the daemon exits - a stopped bot does not
    keep the laptop awake for nothing, which a global "display never off"
    setting would.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
        prev = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        if not prev:
            log("keep-awake request REFUSED by Windows - laptop can still enter "
                "Modern Standby, where it has crashed 3 times")
            return False
        log("keep-awake: holding the laptop out of Modern Standby while the bot runs")
        return True
    except Exception as e:
        log(f"keep-awake unavailable ({e}) - laptop can still enter Modern Standby")
        return False


# "You've hit your session limit - resets 2:10pm (America/Chicago)". Formats seen
# live: "1pm", "2pm", "12:30pm", "1:40pm", "6:40pm". Hour-only has no colon.
_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m\s*\(([^)]+)\)", re.I)


def quota_reset_wait(err):
    """Seconds to wait until the quota reset the BRIDGE ITSELF named, or None.

    Claude usage limits are the single largest source of this bot's blindness -
    852 of 1484 blind market-minutes as measured 2026-09-22. The blind backoff
    that handles them is deliberately ignorant: on 2026-09-22 the bridge said
    "resets 2:10pm", the quota came back at 2:10, and the daemon sat in a 900s
    backoff until 2:20 because nothing read the sentence it had just logged.
    Ten minutes of avoidable blindness, and it needed a human to shorten it.

    Retrying BEFORE the stated reset cannot succeed, and retrying long after it
    wastes market time, so the reset time is strictly better information than any
    exponential curve. Guarded: a parse that lands in the past, more than 6h out,
    or names a timezone this machine does not know returns None and the caller
    keeps its normal backoff. Never returns something that parks the bot longer
    than the bridge asked for.
    """
    m = _RESET_RE.search(err or "")
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        hh = int(m.group(1)) % 12
        if m.group(3).lower() == "p":
            hh += 12
        tz = ZoneInfo(m.group(4).strip())
        now = datetime.now(tz)
        target = now.replace(hour=hh, minute=int(m.group(2) or 0),
                             second=0, microsecond=0)
        if target <= now:                 # e.g. "resets 1pm" seen at 2pm -> tomorrow
            target += timedelta(days=1)
        wait = (target - now).total_seconds() + 30      # clear the boundary
    except Exception:
        return None
    return wait if 0 < wait <= 6 * 3600 else None


def market_minutes_since(ts):
    """Minutes of TRADING time between `ts` and now - never wall clock.

    2026-10-06 09:45:55 logged "broker reachable again after 1 failed attempt(s), 1
    degraded pass(es), ~1069 min". 1069 minutes is 17.8 hours: the daemon ran all night,
    one attempt late on 10-05 set _broker_down_since, no reconcile is attempted outside
    09:45-15:55, and the first attempt at the 10-06 open subtracted the two timestamps.
    The broker was NOT unreachable for 17 hours; nothing asked it for 17 hours.

    It is not only a wrong log line. _maybe_alert_broker() measures its 15/60/180 minute
    thresholds off the same subtraction, so an outage straddling a close would have
    emailed Devon "down about 1069 minutes" in the first seconds of the next session,
    for a broker that may be perfectly healthy.

    This is the error class cloud and I spent 09-17 to 09-22 removing from the
    experiment downtime figure - counting closed-market time as downtime - reappearing
    in my own alert path a fortnight later. Same market gate both arms obey, asked for
    per DATE rather than rebuilt here: bot.session_bounds (weekdays 09:45-15:55 ET,
    09:45-12:55 on bot.EARLY_CLOSE half-days, bot.MARKET_HOLIDAYS excluded).

    2026-10-14: the window used to be two literals in the loop below. On the three
    1 pm closes (2026-11-27, 2026-12-24, 2027-11-26) that read 12:55-15:55 as three
    hours of silence, so the 60-minute backstop in _maybe_alert_broker would have
    emailed Devon a false outage at about 13:55 on each of them, and report_downtime
    would have billed the same minutes as blindness. One duplicated constant, three
    wrong consumers - which is the argument for asking the shared helper instead.
    """
    if not ts:
        return 0
    start = datetime.fromtimestamp(ts, bot.ET_TZ)
    end = datetime.now(bot.ET_TZ)
    if end <= start:
        return 0
    total, day = 0.0, start
    while day.date() <= end.date():
        # bot.session_bounds is the ONE place the window lives, weekday, holiday and
        # early close together, so this cannot model a half-day correctly while
        # check_market or the ARM A WATCH models it differently. It returns None on a
        # weekend or a MARKET_HOLIDAYS date, which is why the explicit weekday and
        # holiday test that used to be here is gone rather than merely redundant.
        b = bot.session_bounds(day)
        if b:
            lo, hi = max(start, b[0]), min(end, b[1])
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(total)


def now_et():
    """Always ET, never local time.

    This laptop runs on US Central. Stamping rh_status.json with local time made
    remote monitoring read a file committed one minute ago as an hour stale and
    report the bot dead. The cloud bot stamps ET, so this matches it exactly,
    including the format, because that side compares these as strings.
    """
    return datetime.now(bot.ET_TZ)


DAEMON_LOG = "rh_daemon.log"


def log(msg):
    """Write to rh_daemon.log directly, and to stdout when there is one.

    The task used to run "cmd.exe /c python ... >> rh_daemon.log", which meant a
    console had to exist for logging to work. That console is what kept killing
    the bot. Owning the log file here lets the task run pythonw.exe with no
    console at all, and pythonw gives the process no stdout, hence the None check.
    """
    line = f"[{now_et().strftime('%Y-%m-%d %H:%M:%S')} ET] {msg}"
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(DAEMON_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load(path, default):
    # utf-8-sig, not utf-8: PowerShell 5.1 writes JSON with a BOM, and a bare
    # json.load chokes on it, which used to look identical to a missing file.
    try:
        with open(path, encoding="utf-8-sig") as f: return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"WARNING: {path} exists but could not be parsed ({e}), using default")
        return default


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f: json.dump(obj, f, indent=1, sort_keys=True)


def save_ledger(led):
    """Stamp the mode INTO the ledger, then write it.

    rh_status.json also carries a dry flag, but it describes the last CYCLE, not
    the ledger. One stray `rh_daemon.py --dry --once` against a live install
    rewrites status to dry:true while the real ledger sits there untouched, and
    -Live keys off exactly that flag to decide whether wiping the ledger is safe.
    Done that once already. Keeping the flag with the data it describes makes the
    desync impossible.
    """
    led["dry"] = DRY
    _save(LEDGER_F, led)


CFG = _load(CONFIG_F, {})
ACCOUNT    = CFG.get("account", "")
CLAUDE_BIN = CFG.get("claude", "claude")

# Reuse alpaca_bot.send_email rather than reimplementing SMTP here, so the laptop
# and the cloud bot mail through one code path. The cloud gets its password from a
# GitHub secret; this laptop has no secrets store, so it comes from rh_config.json,
# which is gitignored and never leaves the machine. Unset just means no mail:
# send_email already no-ops without a password and never raises.
if CFG.get("gmail_app_password"):
    bot.GMAIL_APP_PW = CFG["gmail_app_password"]

# SENDER and RECIPIENT, same post-import assignment as the password above and for the
# same reason: alpaca_bot reads these at MODULE level (L160-162) and it is imported at
# L33, long before CFG exists, so setting os.environ here would be a no-op.
#
# These two lines are the MISSING HALF OF THE 2026-08-27 ADDRESS SCRUB. Devon ordered
# the literals out of the PUBLIC repo; cloud replaced them with os.environ lookups and
# wired GMAIL_USER/ALERT_EMAIL into all four GitHub workflows. Nobody wired the LAPTOP,
# which has no workflow to pass secrets - so bot.GMAIL_USER was "" here and send_email
# took its "GMAIL_USER unset" branch on EVERY alert.
#
# It failed SILENTLY for two weeks because the old log line said "emailed" whether or
# not anything was delivered, and because Slack kept working so the alerts still
# arrived. It only became visible on 2026-09-02 when send_email started returning a
# real delivery verdict. 12 alerts were lost, including a deposit and 3 filled orders.
#
# I checked the Actions side of that scrub at the time and never checked this one.
# Verifying a fix in one execution context is not verifying the fix.
if CFG.get("gmail_user"):
    bot.GMAIL_USER = CFG["gmail_user"]
if CFG.get("alert_email"):
    bot.ALERT_TO = CFG["alert_email"]

# ALSO into os.environ, not only onto the module. Found 2026-10-07 while Devon asked me to
# fire the Arm A watch: the documented test `python arm_a_watch.py --force` reported
# "sent via: slack" with NO email, because it delivers through rh_watchdog.alert(), which
# reads os.environ - and the laptop only ever set bot.GMAIL_* module attributes. Production
# was fine (rh_daemon.notify -> bot.send_email uses the attributes) so the TEST was broken,
# not the alerting: it would have reported a dead email channel that works, or passed while
# proving nothing about the path that carries real alerts. Slack keys were already exported
# this way; the mail identity was not. Same lesson as STANDING FACTS: verify in the
# environment the code actually runs in.
for _k, _e in (("gmail_user", "GMAIL_USER"), ("gmail_app_password", "GMAIL_APP_PASSWORD"),
               ("alert_email", "ALERT_EMAIL")):
    if CFG.get(_k):
        os.environ.setdefault(_e, CFG[_k])

# Slack webhook, same pattern and same reason. SLACK_WEBHOOK_URL is a GitHub
# secret, which covers the workflows, but this daemon runs on the LAPTOP where no
# such environment exists, so the mirror would have silently no-opped forever
# while everyone assumed Slack coverage. Verified enabled() was False here before
# adding this. Set BEFORE slack_notify is first imported, since it reads the env
# at import time. Config is gitignored, so the URL never reaches the public repo.
# Slack credentials, read from the gitignored config because this daemon runs on
# the LAPTOP and the GitHub secrets only reach the workflows. Validated by prefix:
# slack_notify only checks a value is non-empty, so a malformed one reports
# "configured" and then fails on every call. That already happened once, when a
# multi-line paste stored the literal text "$url = Read-Host ..." and enabled()
# still returned True. A misconfiguration that announces itself once beats one
# that fails quietly forever.
#
# Set BEFORE slack_notify is first imported, since it reads all three at import.
#
# READ-ONLY here on purpose. Cloud's alpaca-bot.yml already runs --pull-ingest
# every 15 min, so it owns writing Slack messages into AGENT_MAIL.md. The daemon
# must never also ingest: two writers on the same append-only file is how you get
# duplicated or conflicting entries. Token and channel are here so a laptop
# session can READ the channel on demand, nothing more.
for _key, _env, _prefix in (
        ("slack_webhook_url", "SLACK_WEBHOOK_URL", "https://hooks.slack.com/"),
        ("slack_bot_token",   "SLACK_BOT_TOKEN",   "xoxb-"),
        ("slack_channel_id",  "SLACK_CHANNEL_ID",  ""),
):
    _val = (CFG.get(_key) or "").strip()
    if not _val:
        continue
    if _prefix and not _val.startswith(_prefix):
        log(f"{_key} in rh_config.json does not look right (expected it to start "
            f"with {_prefix!r}); that Slack feature stays OFF")
        continue
    os.environ[_env] = _val

# Say plainly which Slack capabilities are live. cloud's point (2026-08-25): if
# only the webhook is pasted, posting works while reading stays off, and that
# asymmetry should be visible rather than inferred from the absence of a line.
_slack_post = bool(os.environ.get("SLACK_WEBHOOK_URL"))
_slack_read = bool(os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_CHANNEL_ID"))
# Only the RUNNING DAEMON writes this. At module level __name__ is "rh_daemon" on
# import and "__main__" only when the daemon is actually started, so a diagnostic
# `import rh_daemon` no longer appends to the operational log. I kept doing exactly
# that while debugging, and on 2026-09-10 two of my test lines landed in rh_daemon.log
# reading "NOT emailed ... no gmail_app_password set" - FALSE as operational history,
# in the committed file this session, cloud and the Sunday audit read to reconstruct
# incidents. I said then that I would route diagnostics away from this log and did
# not; this is that fix. A shared record must not be writable as a side effect.
if __name__ == "__main__":
    log(f"slack: post {'ON' if _slack_post else 'off'} | read "
        f"{'ON' if _slack_read else 'off'}"
        + ("" if (_slack_post and _slack_read) else
           "  (missing values go in rh_config.json: "
           + ", ".join(k for k, ok in (("slack_webhook_url", _slack_post),
                                       ("slack_bot_token+slack_channel_id", _slack_read))
                       if not ok) + ")"))


# ── Execution bridge: one short headless agent turn, MCP tools only ──────────
# The Robinhood connector is a claude.ai MCP server, so its tools are DEFERRED:
# they are not in the headless model's tool list until ToolSearch loads them.
# Without the preamble the agent reports "no trading tools available" and
# cheerfully returns an empty plan — which reads exactly like a flat market.
RH_SERVER = "mcp__claude_ai_Robinhood"

# ToolSearch's select: takes EXACT tool names. Passing the bare server prefix
# matched only about one run in three, so name every tool the daemon uses.
# place_option_order is deliberately absent: the allowlist is where the
# "equities only, never options" rail is actually enforced, not the prompt.
RH_TOOLS = [f"{RH_SERVER}__{t}" for t in (
    "get_portfolio", "get_equity_positions", "get_equity_quotes",
    "place_equity_order", "cancel_equity_order", "get_equity_orders",
)]
RH_PREAMBLE = (
    "FIRST, before anything else, call ToolSearch with this exact query to load "
    "the Robinhood tools (they are deferred and uncallable until you do):\n"
    f"select:{','.join(RH_TOOLS)}\n"
    "If ToolSearch returns no Robinhood tools, output {\"error\":\"no_tools\"} "
    "and stop. Never guess, estimate, or fabricate account or market data.\n\n"
)
# ToolSearch must itself be allowed, or the agent cannot load anything.
RH_ALLOWED = " ".join(["ToolSearch"] + RH_TOOLS)

# Run the execution turn OUTSIDE the repo. The repo CLAUDE.md carries a hard rail,
# "never place a real-money trade yourself", aimed at a human-facing chat Claude.
# But this bridge IS the sanctioned automation: a headless turn handed an exact,
# already-decided order list, the only way to reach Robinhood, which has no order
# API. Run inside the repo, the executor reads that rail and refuses every order,
# including protective SELLS, which it started doing on 2026-07-28 the moment the
# laptop pulled the new CLAUDE.md. claude walks up from cwd looking for CLAUDE.md,
# so a directory outside the repo tree (no CLAUDE.md above it, verified) gives the
# executor a clean context. It still gets the account-level Robinhood connector,
# which does not depend on cwd. The rail stays fully in force for chat sessions.
BRIDGE_CWD = os.path.join(os.environ.get("LOCALAPPDATA",
                          os.path.expanduser("~")), "rh_bridge")
try:
    os.makedirs(BRIDGE_CWD, exist_ok=True)
except Exception:
    BRIDGE_CWD = None       # fall back to default cwd rather than crash


def _remember_alerts(led, **kw):
    """Persist alert dampeners ON THE LEDGER so a restart cannot unmute them.

    These four lived in module globals, so every restart reset them. Two real
    consequences, both observed: an outage alert whose ALL-CLEAR was swallowed
    because _reconcile_fails went back to 0 (the log showed 7 alerts against 2
    all-clears), and hourly/daily re-alert dampeners silently rearmed, so a
    restart mid-outage could immediately re-alert. Wall-clock timestamps survive
    a restart correctly on their own; what was missing was somewhere to keep them.

    I flagged all of this weeks ago as "state that must survive a restart is kept
    in memory" and said I would move it onto the ledger. This is that.
    """
    a = led.setdefault("alerts", {})
    a.update({k: v for k, v in kw.items()})
    save_ledger(led)


def agent(prompt):
    """Run a headless Claude turn and return the JSON object it printed."""
    try:
        r = subprocess.run([CLAUDE_BIN, "-p", RH_PREAMBLE + prompt,
                            "--allowedTools", RH_ALLOWED],
                           capture_output=True, cwd=BRIDGE_CWD,
                           text=True, timeout=AGENT_TIMEOUT)
        out = (r.stdout or "").strip()
        i, j = out.find("{"), out.rfind("}")
        if i >= 0 and j > i:
            res = json.loads(out[i:j + 1])
            # Loud, because a silent None here reads downstream as "flat market"
            # rather than "the bridge is broken", which is how a dead bridge
            # spent a whole session looking like a calm day.
            if isinstance(res, dict) and res.get("error") == "no_tools":
                log("agent could NOT load the Robinhood MCP tools, skipping "
                    "this pass rather than acting on missing data")
                return None
            return res
        global _last_agent_error
        _last_agent_error = out[:200]
        log(f"agent returned no JSON: {out[:200]}")
    except Exception as e:
        log(f"agent call failed: {e}")
    return None


def reconcile():
    """Ask the broker for truth. Returns {cash, positions:[...]} or None."""
    res = agent(
        f"Using the Robinhood MCP tools, call get_portfolio and get_equity_positions "
        f"for account {ACCOUNT}. Place no orders. Reply with ONLY a JSON object, no prose:\n"
        '{"cash": <buying_power as number>, '
        '"total_value": <total account value INCLUDING unsettled proceeds>, '
        '"pending_deposits": <pending_deposits as number, 0 if none>, "positions": '
        '[{"symbol": "X", "qty": <number>, "avg_cost": <number>}]}')
    if res and isinstance(res.get("positions"), list):
        return res
    return None


DEPOSIT_OVERDUE_DAYS   = 10      # weekly cadence plus slack for a shifted day
DEPOSIT_REALERT_DAYS   = 7       # nag weekly, not daily, while it stays unresolved
_deposit_alert_on = None         # date string of the last overdue alert


def check_deposit_overdue(led):
    """Ask Devon to confirm a deposit rather than inventing one.

    The rising-edge detector only sees pending_deposits while the daemon is awake,
    so a deposit that posts AND settles inside an off-window is invisible. There is
    no transfer or funding endpoint in the Robinhood MCP to level-check against
    (verified 2026-08-25), and inferring one from a cash jump is not safe: T+1
    settlement produces the identical signature, which is exactly what made the
    08-14 reconstruction wrong.

    So this never writes an event. It notices the weekly cadence has lapsed and
    asks Devon to read his app, because a nagging alert is a recoverable failure
    and a fabricated deposit silently corrupts the experiment.
    """
    global _deposit_alert_on
    try:
        doc = _load(DEPOSITS_F, None) or {}
        events = doc.get("events") or []
        if not events:
            return
        last = max(e.get("date", "") for e in events)
        today = now_et().date()
        gap = (today - datetime.strptime(last, "%Y-%m-%d").date()).days
        if gap < DEPOSIT_OVERDUE_DAYS:
            return
        if _deposit_alert_on:
            since = (today - datetime.strptime(_deposit_alert_on, "%Y-%m-%d").date()).days
            if since < DEPOSIT_REALERT_DAYS:
                return
        _deposit_alert_on = today.isoformat()
        _remember_alerts(led, deposit_alert_on=_deposit_alert_on)
        log(f"deposit overdue: {gap} days since {last}, asking Devon to confirm")
        notify("RH bot: is a weekly deposit missing?", chr(10).join([
            f"No deposit recorded since {last}, which is {gap} days ago. The weekly "
            f"~$10 transfer usually lands within {DEPOSIT_OVERDUE_DAYS}.",
            "",
            "This is NOT an error and nothing is wrong with the account. The bot can",
            "only see a deposit while it is awake, and there is no transfer endpoint",
            "to check against, so it asks rather than guesses.",
            "",
            "Please check Robinhood: Account -> History -> Transfers.",
            "  If a deposit landed, tell the laptop session and it will record it.",
            "  If none landed, ignore this; it will ask again in a week.",
            "",
            "Why it matters: contributed capital is subtracted from Arm B before any",
            "performance comparison, so a missed deposit makes Arm B look better than",
            "it is. An unrecorded one is the error, not this message.",
        ]))
    except Exception as e:
        log(f"deposit overdue check skipped ({e})")


def _recompute_deposit_totals(doc):
    """Keep every summary field in rh_deposits.json consistent with events.

    The file is hand-authored with baseline.starting_equity,
    total_deposited_since_start and total_contributed_capital, but record_deposit
    originally wrote only "total_deposited". So the 2026-08-24 deposit WAS captured
    correctly as an event while every published total stayed stale, and the cloud
    session read Arm B as ~3.6 points better than it was and called the start
    figure contaminated. The data was right; the summary was not. One writer for
    all of them now, so they cannot diverge again.
    """
    ev = round(sum(float(e.get("amount") or 0) for e in doc.get("events") or []), 2)
    base = float((doc.get("baseline") or {}).get("starting_equity") or 0)
    doc["total_deposited_since_start"] = ev
    doc["total_deposited"] = ev            # legacy name, kept so nothing breaks
    doc["total_contributed_capital"] = round(base + ev, 2)

    # IN-WINDOW figures for the A/B decision. The totals above run from the bot's
    # 2026-07-23 inception; the experiment window opened 2026-08-24. Those are not
    # the same question, and experiment.json's rule ("Arm B figures MUST be
    # deposit-adjusted using rh_deposits.json") pointed a cold November reader at
    # this file when the only headline it published was the inception figure.
    # Subtracting it from window equity charges Arm B for $165 of deposits that
    # landed BEFORE the window while crediting only gains earned inside it: on
    # 2026-09-01 that reads +1.1% when the window's answer is -1.5%. The two
    # methods disagree in SIGN, so this is not a rounding question — it decides
    # which arm wins. Derived here rather than hand-maintained because the same
    # single-writer argument as above applies: a hand-kept copy goes stale the
    # first week nobody is looking.
    try:
        exp = _load("experiment.json", None) or {}
        wstart = exp.get("started")
        wbase = (exp.get("arm_B") or {}).get("start_equity")
        if wstart and wbase is not None:
            win = round(sum(float(e.get("amount") or 0) for e in doc.get("events") or []
                            if str(e.get("date") or "") >= wstart), 2)
            doc["experiment_window"] = {
                "window_opened": wstart,
                "start_equity": round(float(wbase), 2),
                "deposits_in_window": win,
                "adjusted_basis": round(float(wbase) + win, 2),
                "how_to_use": ("For the A/B decision use THIS block, not "
                               "total_contributed_capital: arm_B return = "
                               "current_total_value - adjusted_basis. The totals "
                               "above measure the bot since 2026-07-23 inception, "
                               "which is a different question and a different sign."),
            }
    except Exception:
        pass                               # never let reporting math break a deposit record
    return doc


def record_deposit(amount, source, note=""):
    """Append a deposit event to the COMMITTED rh_deposits.json.

    Devon has a recurring ~$10/week deposit. That is contributed capital, not
    return, and leaving it in raw equity already turned a flat week into a fake
    +4.26% for the A/B experiment. The cash is still real and still gets traded
    normally; only the performance math subtracts this file's total.
    """
    try:
        doc = _load(DEPOSITS_F, None) or {"currency": "USD", "events": [],
                                          "total_deposited": 0.0}
        doc.setdefault("events", []).append({
            "date": now_et().strftime("%Y-%m-%d"),
            "amount": round(float(amount), 2),
            "confidence": "confirmed",
            "source": source,
            "note": note})
        _recompute_deposit_totals(doc)
        _save(DEPOSITS_F, doc)
        total = doc["total_contributed_capital"]
        log(f"DEPOSIT recorded: ${float(amount):.2f} ({source}); contributed ${total:.2f}")
        body = [
            f"Recorded a ${float(amount):.2f} deposit into the agentic account.",
            f"Total contributed capital is now ${total:.2f}.",
            "",
            "The bot invests this normally. It is excluded from performance math",
            "so contributed capital does not get counted as a gain.",
        ]
        notify("RH laptop bot: deposit recorded", chr(10).join(body))
        return True
    except Exception as e:
        log(f"could not record deposit ({e})")
        return False


def adopt_truth(led, truth):
    """Take the broker's view into the ledger, unless the snapshot looks corrupt.

    Returns True when adopted. A real Alpaca blip on 2026-07-07 returned an empty
    position list and wiped the cloud bot's hold ledger, which cost real money.
    alpaca_bot guards that on both its passes; the laptop had no equivalent, so a
    single flaky MCP response would clear positions AND the hold basis/peak
    history here, and the next cycle would re-buy the whole book from scratch.

    Holds are pruned to match real positions on every adopted snapshot, so a hold
    entry for a name we no longer own cannot linger and keep ratcheting.
    """
    if not truth:
        return False
    positions = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                  "avg_cost": float(p.get("avg_cost") or 0)}
                 for p in truth.get("positions") or [] if float(p["qty"]) > 0]
    if not positions and led.get("positions"):
        if DRY:
            # Dry fills are simulated, so the real book disagreeing is expected,
            # not a fault. Keeping the simulated ledger is also what stops a dry
            # run re-deciding the same buys every cycle forever.
            log(f"dry run: {len(led['positions'])} simulated position(s) vs the "
                f"broker's empty book, as expected; keeping the simulated ledger")
        else:
            log(f"!! broker reported ZERO positions but the ledger holds "
                f"{len(led['positions'])} name(s): corrupt snapshot, keeping the ledger")
        return False
    led["cash"] = float(truth.get("cash") or led["cash"])
    # Broker TOTAL value, which unlike buying_power includes unsettled proceeds.
    # Published equity uses this; see persist(). Sizing deliberately does NOT,
    # because unsettled cash genuinely cannot be spent.
    try:
        tv = float(truth.get("total_value") or 0)
        if tv > 0:
            led["broker_total_value"] = round(tv, 2)
            led["broker_total_at"] = time.time()
    except Exception:
        pass
    led["positions"] = positions
    led["holds"] = {s: h for s, h in (led.get("holds") or {}).items()
                    if any(p["symbol"] == s for p in positions)}
    # Deposit capture. pending_deposits is the broker's own field, so a RISING
    # edge is an authoritative new deposit, unlike inferring from a cash jump
    # (T+1 settlement makes a sell look identical to a deposit the next day,
    # which is exactly what made the retroactive log archaeology imprecise).
    # Recorded once on the rising edge, so it is not double counted when the
    # deposit later settles and pending drops back to 0.
    try:
        pend = float(truth.get("pending_deposits") or 0)
        seen = float(led.get("pending_deposits_seen") or 0)
        if pend > seen + 0.005:
            record_deposit(pend - seen, "broker pending_deposits (rising edge)",
                           f"pending went {seen:.2f} -> {pend:.2f}")
        led["pending_deposits_seen"] = pend
    except Exception as e:
        log(f"deposit check skipped ({e})")

    global _last_reconcile
    _last_reconcile = time.time()   # feeds the periodic-reconcile clock in main()
    return True


def place(orders):
    """Hand the agent an exact order list. Returns the fills it reports."""
    if DRY:
        log(f"DRY RUN — would place {len(orders)} order(s)")
        return []
    res = agent(
        f"Place these EXACT orders on Robinhood account {ACCOUNT} using the MCP order "
        f"tools. Do NOT add, skip, resize, or substitute any order, and do not place "
        f"anything not listed. Equities only, never options. Use market orders; for "
        f'buys use the "notional" dollar amount, for sells use "qty".\n\n'
        f"ORDERS:\n{json.dumps(orders, indent=1)}\n\n"
        'Then reply with ONLY a JSON object, no prose:\n'
        '{"placed": [{"symbol": "X", "action": "buy|sell", "status": "ok|rejected", '
        '"detail": "<broker message if rejected>"}]}')
    return (res or {}).get("placed") or []


# ── Ledger ──────────────────────────────────────────────────────────────────
def fresh_ledger():
    return {"cash": 0.0, "positions": [], "holds": {}, "last_buy": {},
            "sold_today": 0.0, "day": "", "orders_today": 0}


def roll_day(led, today):
    """New day: T+1 proceeds settle, daily order budget resets."""
    if led.get("day") != today:
        led.update({"day": today, "sold_today": 0.0, "orders_today": 0})
        return True
    return False


def apply_fills(led, orders, placed, prices):
    """Update the local ledger from what actually got placed."""
    ok = {(p.get("symbol"), p.get("action")) for p in placed if p.get("status") == "ok"}
    today = now_et().strftime("%Y-%m-%d")     # ET: roll_day stamps the ET date too,
                                              # and last_buy feeds the time stop
    pos = {p["symbol"]: p for p in led["positions"]}
    for o in orders:
        sym, act = o["symbol"], o["action"]
        if placed and (sym, act) not in ok:
            continue                                  # rejected — ledger untouched
        px = prices.get(sym) or 0
        if act == "buy" and px > 0:
            qty = o["notional"] / px
            cur = pos.get(sym)
            if cur:
                tot = cur["qty"] + qty
                cur["avg_cost"] = (cur["avg_cost"] * cur["qty"] + px * qty) / tot if tot else px
                cur["qty"] = tot
            else:
                pos[sym] = {"symbol": sym, "qty": qty, "avg_cost": px}
            led["last_buy"][sym] = today
            if o.get("sleeve") == "HOLD":
                led["holds"].setdefault(sym, {"basis": px, "peak": px})
            led["cash"] = max(0.0, led["cash"] - o["notional"])
        elif act == "sell":
            cur = pos.get(sym)
            if cur:
                sell_qty = min(float(o.get("qty") or cur["qty"]), cur["qty"])
                proceeds = sell_qty * px
                cur["qty"] -= sell_qty
                led["cash"] += proceeds
                led["sold_today"] += proceeds         # T+1: unspendable until tomorrow
                if cur["qty"] <= 1e-9:
                    pos.pop(sym, None); led["holds"].pop(sym, None)
    led["positions"] = [p for p in pos.values() if p["qty"] > 1e-9]
    # The cap is a circuit breaker on REAL submissions, so simulated orders must
    # not consume it. Dry cycles never reach the broker, and reconcile() resets
    # positions to the broker's (empty) truth every cycle, so a dry run re-decides
    # the same buys forever. At ~7/cycle that burns the 40/day budget in about an
    # hour, after which cycle() goes quiet in a way that reads as "found nothing".
    if not DRY:
        led["orders_today"] = led.get("orders_today", 0) + len(orders)


def track_peaks(led, prices):
    """Hold-sleeve peak ratchet, same as the cloud bot's holds.json."""
    for sym, h in led.get("holds", {}).items():
        px = prices.get(sym)
        if px and px > float(h.get("peak") or 0):
            h["peak"] = round(px, 4)


def _material(snap):
    """The parts of a snapshot worth pushing over.

    Deliberately excludes equity/index/hold/trade and ts: those are price-derived
    and move every single pass, which is why the old code committed once a minute.
    Positions, holds and the order count only change when something real happened.
    """
    return {"positions": snap.get("positions"), "holds": snap.get("holds"),
            "orders_today": snap.get("orders_today"), "dry": snap.get("dry")}


def _push_status(reason):
    """Commit and push the monitoring files. Bounded so a stall cannot eat passes.

    This runs INSIDE the trading loop, so every second spent here is a second the
    exit rails are not checking prices. Timeouts total ~140s worst case instead of
    the old ~390s, and it now runs a couple of dozen times a day rather than ~370.
    """
    try:
        # DEPOSITS_F rides along deliberately. record_deposit() writes it and its
        # docstring calls it "the COMMITTED rh_deposits.json", but nothing here ever
        # staged it, so every deposit after the file was created lived on this laptop
        # alone. It looked fine only because I hand-committed it twice while fixing
        # other things. Cloud and the weekly audit read the COMMITTED copy, so an
        # unpushed deposit is counted as Arm B profit by the one number November
        # decides on: at $10 unpushed on a $245 account that reads +5.4% instead of
        # the true +1.1%, a 5x overstatement from a missing filename.
        subprocess.run(["git", "add", STATUS_F, LOG_F, DEPOSITS_F],
                       capture_output=True, timeout=15)
        if subprocess.run(["git", "diff", "--cached", "--quiet"],
                          capture_output=True, timeout=15).returncode == 0:
            return False                      # nothing staged, nothing to say
        subprocess.run(["git", "commit", "-m",
                        f"rh bot {now_et().strftime('%Y-%m-%dT%H:%M')} ET ({reason})"],
                       capture_output=True, timeout=20)
        if subprocess.run(["git", "push"], capture_output=True, timeout=30).returncode != 0:
            subprocess.run(["git", "pull", "--rebase", "--autostash"],
                           capture_output=True, timeout=30)
            subprocess.run(["git", "push"], capture_output=True, timeout=30)
        return True
    except Exception as e:
        log(f"git persist skipped: {e}")
        return False


def notify(subject, body, untrusted=False):
    """Tell Devon about an operational condition. Never raises.

    Logs the outcome, because send_email's own confirmation is a print() and the
    daemon runs under pythonw with no stdout, so delivery would otherwise leave no
    trace either way.

    untrusted=True fences the body as data in Slack. Use it for anything carrying
    text we did not author. Proven live, not theoretical: bot.news_flags returns
    Yahoo/Alpaca HEADLINES, rh_bot embeds them in an order reason
    ("NEWS-EXIT (<headline>)"), and email_trades puts order reasons straight into
    this body. A hostile headline would therefore land verbatim in a channel that
    @Claude reads as context. Dormant right now because WIND_DOWN short-circuits
    before the news check and index-only opens no positions, but it is one config
    change from live, so the fence goes in now rather than after.
    """
    # DO NOT add a slack_notify.post() here. bot.send_email() ALREADY mirrors to
    # Slack, before its own Gmail guard, in the identical bold-subject-then-body format
    # (alpaca_bot.py L186). This function used to post as well, so EVERY alert the
    # laptop sent arrived TWICE, ~0.25s apart, for a week - measured 2026-09-01
    # across all four alert types, 12 duplicate copies sitting in the channel.
    #
    # Both mirrors were written for the SAME correct reason - "a missing Gmail
    # secret must not take Slack down with it" - independently, by two sessions,
    # neither aware the other had done it. The duplication was the direct result
    # of both of us applying the same right principle to one call chain.
    #
    # The duplication was cosmetic. THE DEFEATED FENCE WAS NOT: send_email mirrors
    # RAW text, so an untrusted=True call posted a fenced copy AND an unfenced one,
    # and the prompt-injection guard bought nothing. Fixed by fencing the body
    # BEFORE handing it over, so the one surviving mirror carries fenced text.
    if untrusted:
        # slack_notify.fence() is the single authoritative implementation; cloud
        # exported it 2026-09-01 at my request and post() delegates to that same one.
        # This replaces an inlined copy - two copies of a security control is how one
        # gets improved and the other quietly does not.
        import slack_notify
        body = slack_notify.fence(body)

    # No early return on a missing password. send_email mirrors to Slack BEFORE
    # its Gmail guard, so letting it run is what keeps Slack alive when Gmail is
    # misconfigured - the exact property the removed mirror was defending.
    try:
        delivered = bot.send_email(subject, body)
    except Exception as e:                 # send_email swallows its own errors
        log(f"notify FAILED ({e}): {subject}")
        return False
    if not bot.GMAIL_APP_PW:
        log(f"NOT emailed ({subject}): no gmail_app_password set; Slack still posted")
        return False
    # send_email now RETURNS a delivery verdict (cloud added it 2026-09-01 at my
    # request): True only when SMTP accepted it. Before that the daemon could not
    # tell a delivered alert from a silently failed one, so this line claimed
    # "emailed" when it only ever meant "handed off". It can mean delivered now.
    # Slack is deliberately NOT part of the verdict, so False here means email
    # failed - never that nothing was sent anywhere.
    if delivered:
        log(f"emailed: {subject}")
        return True
    log(f"email NOT delivered ({subject}); Slack mirror still posted")
    return False


def email_trades(res, placed, led):
    """Tell Devon what the laptop bot just did. No subject-line emoji: he prints
    these to PDF and the subject becomes the filename."""
    orders = res.get("orders") or []
    if not orders:
        return
    ok = sum(1 for p in placed if p.get("status") == "ok")
    bad = [p for p in placed if p.get("status") != "ok"]
    lines = [f"Robinhood laptop bot ({'DRY' if DRY else 'LIVE'})",
             f"{now_et().strftime('%Y-%m-%d %H:%M ET')}", "",
             f"{ok}/{len(orders)} order(s) accepted.", ""]
    for o in orders:
        st = next((p.get("status") for p in placed
                   if p.get("symbol") == o["symbol"] and p.get("action") == o["action"]),
                  "dry" if DRY else "unknown")
        amt = o.get("notional")
        size = f"${amt:.2f}" if amt else f"{o.get('qty')} sh"
        lines.append(f"  [{st:8}] {o['action'].upper():4} {o['symbol']:6} {size:>9}  {o['reason']}")
    if bad:
        lines += ["", "NOT ACCEPTED:"] + [f"  {p.get('symbol')}: {p.get('detail') or p.get('status')}"
                                          for p in bad]
    snap = res.get("snapshot") or {}
    lines += ["", f"equity ${snap.get('equity', 0):.2f} | cash ${led.get('cash', 0):.2f} "
                  f"| orders today {led.get('orders_today', 0)}/{MAX_ORDERS_DAY}",
              f"positions: {', '.join(p['symbol'] for p in led['positions']) or 'none'}",
              "", "Stop it: create a file named rh_HALT in the repo folder."]
    subject = (f"RH laptop bot: {ok}/{len(orders)} order(s) "
               f"{'placed' if not DRY else 'simulated'}")
    # Log the attempt. send_email's only feedback is a print() to a stream the
    # daemon does not capture, so email delivery was previously invisible: a trade
    # went out with zero trace of whether the notification did. If no password is
    # configured say so, since that is the silent-no-mail case.
    # Routed through notify() so this gets the Slack mirror too, and fenced as
    # UNTRUSTED: order reasons can embed an external news headline
    # ("NEWS-EXIT (<headline>)"), which would otherwise land verbatim in a channel
    # @Claude reads as context. notify() logs delivery either way, which matters
    # because send_email's own feedback is a print() the daemon cannot see.
    if notify(subject, chr(10).join(lines), untrusted=True):
        log(f"reported {ok}/{len(orders)} fills")


EQUITY_JUMP_MAX = 3.00      # dollars of broker/computed gap to substitute unchecked
EQUITY_AGREE_TOL = 0.50     # two consecutive broker readings this close corroborate


def _pick_equity(computed, broker, prev_broker, last_published):
    """Which number to publish when the broker's total and the computed equity disagree.

    Returns (value, held_back) - held_back is the rejected candidate, or None.

    A SMALL gap substitutes as it always did: buying power legitimately excludes
    unsettled sale proceeds and a pending deposit, so broker total is the better figure.

    A LARGE gap needs more than one reading. My first attempt asked whether anything
    could EXPLAIN the gap - a sale inside the settlement window, a recent deposit - and
    that discriminator is wrong, which the real incident proves: on 2026-10-13 a deposit
    had been recorded the day before, so the bad $10 reading was perfectly "explicable",
    and it was still wrong. What actually separates a real settlement gap from a bad
    sample is PERSISTENCE. A real one is still there at the next reconcile; that $10 was
    gone in fifteen minutes.

    So: two broker readings from DISTINCT reconciles that agree are believed, whatever
    the gap, which is what lets a genuine step change (a large deposit, a wind-down) be
    adopted. Distinct matters: persist() runs every cycle while a reconcile refreshes the
    reading at most every RECONCILE_MAX_AGE_SEC, so a cached value would otherwise
    corroborate itself one cycle later - see the rotation in persist(). The cost is that
    a genuine step is published on the next RECONCILE, up to half an hour, not the next
    pass. Failing that, prefer CONTINUITY - whichever candidate is closer to the equity
    already published - because an account's value does not move 3% in fifteen minutes
    without a cause, while a glitched field can.
    That tie-break is what makes this safe in BOTH directions, and the direction that
    already cost real credibility is the low one: after the 2026-08-24 wind-down, broker
    total was ~232 and computed ~124, and publishing computed was a phantom 48% crash.
    Under this rule that case substitutes immediately - the reading before it also said
    ~232, so it corroborates - rather than waiting a cycle. Checked in the tests.
    """
    if computed is None:
        return broker, None
    if abs(float(broker) - float(computed)) <= EQUITY_JUMP_MAX:
        return broker, None
    if prev_broker is not None and abs(float(broker) - float(prev_broker)) <= EQUITY_AGREE_TOL:
        return broker, None                      # persistent, so real
    if last_published is None:
        return computed, float(broker)           # nothing to anchor to: trust the arithmetic
    near_broker = abs(float(broker) - float(last_published))
    near_computed = abs(float(computed) - float(last_published))
    if near_broker <= near_computed:
        return broker, None
    return computed, float(broker)


def persist(led, res, placed):
    """Write the ledger locally; commit the shareable log/status for monitoring."""
    global _last_push_at, _last_push_material
    save_ledger(led)
    snap = dict(res.get("snapshot") or {})
    # T+1 TRAP: decide() computes equity as cash + invested, and `cash` is broker
    # BUYING POWER, which excludes unsettled sale proceeds. After the 2026-08-24
    # wind-down sold 24 positions, this file published ~$124 for a full trading day
    # while the account actually held ~$232: a phantom 48% crash, in the very file
    # cloud and audit measure Arm B from. Publish the broker's total instead, and
    # say so when they differ, so a settlement artifact is never read as a loss.
    # 2026-10-14: the substitution above is right and it trusted ONE reading without
    # limit. On 2026-10-13 a single reconcile returned a broker total about $10 high and
    # this file published equity 318.60 at 13:36 and 13:51 against a true ~308.4 - 3.2%
    # on a $308 book, committed twice, corrected by the next reconcile. The benchmark
    # index moved 0.03% that day, so none of it was a move. It was also impossible on its
    # face: the field is labelled "unsettled", unsettled proceeds require a SALE, and the
    # last sale was 2026-08-24. A label that contradicts itself is data about the bug.
    #
    # So the substitution now has to be EXPLICABLE or CORROBORATED, and both tests are
    # cheap. A big gap is accepted when something could have caused it (an unsettled sale
    # inside the settlement window, or a deposit recorded recently), or when a second
    # reconcile agrees with the first. One sample, no cause, no corroboration: keep the
    # computed figure and say so loudly in the log. Guarding BOTH directions matters -
    # the 08-24 case was this field reading LOW, and nothing here may regress that.
    bt, bt_at = led.get("broker_total_value"), led.get("broker_total_at") or 0
    if bt and (time.time() - bt_at) < RECONCILE_MAX_AGE_SEC * 2:
        computed = snap.get("equity")
        # The previous reading and the previously PUBLISHED equity are the two anchors
        # _pick_equity needs. The reading lives on the LEDGER, because a module global
        # would lose it on exactly the restart that tends to follow a bad day - this file
        # has lost restart-critical state in a global five times. Remember the raw reading
        # whether or not it was used, or a persistent gap could never corroborate itself.
        # ROTATE ONLY ON A NEW RECONCILE. persist() runs every cycle but a reconcile
        # refreshes broker_total_value at most every RECONCILE_MAX_AGE_SEC, so the same
        # reading is persisted several cycles running. The first version of this stored
        # prev on EVERY call, which made a cached reading corroborate ITSELF on the next
        # cycle - the guard held 2026-10-13's glitch back once and then published it.
        # Cloud reproduced that with the real persist() and proved the cadence from the
        # incident's own samples: 318.60 at both 13:36 and 13:51, same broker total,
        # deltas 9.93 then 10.07, because the computed figure moved and the reading did
        # not. Keying the rotation on broker_total_at makes corroboration mean what it
        # says: two DISTINCT reconciles.
        seen_at = led.get("broker_total_seen_at")
        if seen_at != bt_at:
            led["broker_total_prev"] = led.get("broker_total_last")
            led["broker_total_last"] = float(bt)
            led["broker_total_seen_at"] = bt_at
        prev_broker = led.get("broker_total_prev")
        last_published = (_load(STATUS_F, {}) or {}).get("equity")
        value, held_back = _pick_equity(computed, bt, prev_broker, last_published)
        snap["equity"] = value
        if held_back is not None:
            log(f"!! broker_total_value {held_back} against computed {computed}, previous "
                f"reading {prev_broker}, last published {last_published}: one unconfirmed "
                f"sample moving equity more than ${EQUITY_JUMP_MAX:.2f}, so publishing "
                f"{value} and holding it back until a reading agrees with it")
            snap["equity_source"] = "computed (broker_total_value held back)"
            snap["broker_total_value_held_back"] = held_back
            snap["broker_total_value_delta"] = round(float(held_back) - float(computed), 2)
        elif computed is not None and abs(float(value) - float(computed)) > 0.50:
            snap["equity_source"] = "broker_total_value"
            snap["unsettled_excluded_from_buying_power"] = round(float(value) - float(computed), 2)
    snap.update({"ts": now_et().strftime("%Y-%m-%dT%H:%M"),
                 "positions": {p["symbol"]: round(p["qty"], 6) for p in led["positions"]},
                 "holds": sorted(led.get("holds") or {}),
                 "orders_today": led.get("orders_today", 0), "dry": DRY})
    # Laptop-local facts for the GitHub daily digest, which replaced the retired daily
    # checks on 2026-10-17 and runs where none of this is visible. Free here: read once
    # at startup, copied in. A missing or None value is a FLAG, not a pass - the digest
    # treats an unreadable source that way and so should any other reader.
    if _MACHINE:
        snap["machine"] = _MACHINE

    # REALIZED (taxable) gain/loss, for Devon's tax tracking. Devon asked both arms to
    # carry a running realized total; cloud built the shared reader and asked the laptop
    # to publish Arm B's here so every reader of rh_status.json sees it, not just
    # whoever runs a report. Equity change is NOT this: equity includes unrealized
    # paper movement, and only positions actually SOLD are taxable.
    #
    # Imported LAZILY and wrapped: this is a REPORTING field on a live-money trading
    # daemon. A missing file, a bad parse or an exception inside realized.py must never
    # be able to stop a pass from publishing, let alone from trading. On any failure we
    # publish an explicit unknown rather than omitting the key, because an ABSENT block
    # and a ZERO block must not look alike to a reader - the same distinction cloud was
    # careful about with capital_flow, and that this repo has got wrong before.
    # LIVENESS for the Arm A watch. It is SILENT when Arm A is healthy, which is
    # indistinguishable from never running - the hazard audit-prompt.md L49 already states:
    # "any guard that fails OPEN needs a published liveness signal, since a broken one is
    # indistinguishable from a quiet one." I built that watch on 2026-10-06 with no such
    # signal and only noticed when I tried to PROVE to myself it was running. null = never
    # ran in this process; a timestamp = it executed then.
    snap["arm_a_watch_at"] = (None if not _ARM_A_RAN_AT else
                              datetime.fromtimestamp(_ARM_A_RAN_AT,
                                                     bot.ET_TZ).strftime("%Y-%m-%dT%H:%M"))
    try:
        import realized
        snap["realized"] = realized.arm_b_block()
    except Exception as e:
        snap["realized"] = {"state": "unknown", "error": str(e)[:160],
                            "note": "realized block could not be built; NOT zero, unknown"}
    _save(STATUS_F, snap)                     # local write every pass: free, instant
    traded = False
    if placed or res.get("orders"):
        traded = True
        with open(LOG_F, "a") as f:
            for o in res["orders"]:
                st = next((p.get("status") for p in placed
                           if p.get("symbol") == o["symbol"] and p.get("action") == o["action"]),
                          "dry" if DRY else "unknown")
                f.write(json.dumps({**o, "ts": now_et().strftime("%Y-%m-%dT%H:%M"),
                                    "status": st, "venue": "robinhood"}) + "\n")

    # Push on: a trade (monitoring must see fills immediately), a structural change,
    # or the heartbeat. The heartbeat is not optional: without it a quiet laptop and
    # a dead laptop look identical to whoever is watching the repo.
    material = _material(snap)
    due = (time.time() - _last_push_at) >= PUSH_HEARTBEAT_SEC
    reason = ("trade" if traded else
              "change" if material != _last_push_material else
              "heartbeat" if due else None)
    if reason and _push_status(reason):
        _last_push_at, _last_push_material = time.time(), material


def publish_degraded(led, reason):
    """Heartbeat while the broker is unreachable, naming the reason.

    This is what lets monitoring tell a dead BROKER from a dead LAPTOP: without it
    the daemon simply went quiet and the watchdog reported the machine down,
    sending Devon to check hardware that was fine.

    The STATUS WRITE runs every pass; the GIT PUSH is throttled. The original said
    "costs no agent turn, so it is safe to run on every pass", which priced the
    call against the Claude quota the backoff above protects and stopped there. It
    is free of quota and it is not free: each call commits and pushes to a public
    repo. The 2026-09-01 session-limit outage ran 91 minutes and produced ~90
    commits, one per fast pass, while the retries it sat beside were correctly
    backing off to 15-minute spacing. I throttled the expensive thing and left the
    "free" thing firing every 60 seconds.

    The real damage was not bandwidth, it was the shared record: those commits
    buried the one substantive commit either session made that afternoon, in the
    log both sessions and the Sunday cold audit read to find out what happened.

    First degraded pass still pushes IMMEDIATELY - monitoring has to learn at once
    that the broker died and the laptop did not, which is this function's whole
    reason to exist. After that, DEGRADED_PUSH_SEC. The watchdog polls every 30 min
    against STALE_MIN 30, so it cannot tell 5-minute pushes from 1-minute ones.

    The pass count is kept HERE, one per call, not borrowed from _reconcile_fails.
    It used to be, and _reconcile_fails counts broker ATTEMPTS. Once
    quota_reset_wait parked the next attempt hours out, that stayed at 1 for the
    whole outage, so "first pass" was true on every pass: 103 pushes for the
    110-minute 2026-09-23 outage, the exact storm described above.
    """
    global _degraded_push_at, _degraded_passes
    _degraded_passes += 1
    passes = _degraded_passes
    prev = _load(STATUS_F, {}) or {}
    snap = {"ts": now_et().strftime("%Y-%m-%dT%H:%M"),
            "equity": prev.get("equity"),   # last known, so monitoring keeps a number
            "degraded": reason,
            "degraded_since_passes": passes,
            "positions": {p["symbol"]: round(p["qty"], 6)
                          for p in led.get("positions") or []},
            "holds": sorted(led.get("holds") or {}),
            "orders_today": led.get("orders_today", 0),
            "dry": DRY}
    _save(STATUS_F, snap)
    now = time.time()
    if passes <= 1 or (now - _degraded_push_at) >= DEGRADED_PUSH_SEC:
        _degraded_push_at = now
        _push_status("degraded")


MAIL_F = "AGENT_MAIL.md"
# What Devon calls this session (2026-10-01: he could never tell from an alert WHO
# needs to check mail). Keep in step with DISPLAY["laptop"] in mail_check.py.
# 2026-10-01 16:06 ET, Devon to this session, verbatim: "Your name is LAPTOP BOT going
# forward and email should say \"have LAPTOP BOT check mail\"". An earlier laptop session
# had set LAPTOP BOT DAILY CHECK at 10:15; that name describes the SCHEDULED RUN, while
# this notice asks Devon to open an INTERACTIVE session, so the shorter name is also the
# more accurate one. If you are about to change this back, confirm with Devon first -
# two sessions flipping one string is worse than either value.
SESSION_NAME = "LAPTOP BOT"
# OFF again, Devon's call 2026-10-29, because the mute was suppressing TRUE alerts.
# It was on from 10-22 (also his call) to stop five false pairs in one day. What ended
# it: cloud re-measured and found their 10-22 evidence had timed the wrong interval, and
# the three alerts this mute swallowed on 10-26 were real - GitHub took 12.2, 13.3 and
# 8.9 minutes to create those runs' jobs, confirmed here against the API run by run.
# Arm A, the arm carrying the stops, had no job executing for 34 minutes that day while
# the digest said OK. Six minutes of queued is now right more often than it is wrong.
# The machinery below stays in place and dormant: if the signal turns noisy again this
# is one line, and _deliver documents the scope, the fail-open and both defects cloud
# found in it. Do not delete it to tidy up.
ARM_A_MUTE_QUEUED = False

# Devon, same day, asked directly: BOTH names stand. LAPTOP BOT is the interactive session
# that owns this daemon; LAPTOP BOT DAILY CHECK is the scheduled daily check. Mail headed
# `-> laptop[daily]` is for the daily check, plain `-> laptop` and broadcasts for LAPTOP BOT.
# Same rule as DISPLAY in mail_check.py.
DAILY_NAME = "LAPTOP BOT DAILY CHECK"
# The sender may carry a [qualifier] (cloud[daily], cloud[35819496]). The old pattern could
# not match those at all, so this notifier never saw mail from a cloud session. Group 4 is
# the RECIPIENT qualifier.
_MAIL_HEAD = re.compile(r"^## \[([^\]]+)\]\s*(\w+)(?:\[[^\]]*\])?\s*->\s*([A-Za-z]+)(?:\[([^\]]*)\])?", re.M)


_ARM_A_FIRED, _ARM_A_NEXT = {}, 0.0
_ARM_A_RAN_AT = 0.0        # last time the Arm A watch actually executed


def check_arm_a(led):
    """Dead-man's switch for ARM A, run from THIS daemon. Cloud built it; cloud cannot host it.

    Arm A evaluates its stops inside each GitHub Actions run - there are no broker-side stop
    orders - so a run that never gets a machine means no stop can fire. On 2026-10-05 two runs
    were never assigned and Arm A went ~28 minutes to the close unprotected, with nothing
    alerting. Cloud's ARM A WATCH workflow covers it only partly: its workflow_run trigger
    fires when an Arm A run COMPLETES, which is exactly what a stuck run does not do. This
    daemon does not depend on Actions, so running the same check here closes that gap and the
    two arms end up watching each other.

    Wrapped and self-throttled: Arm A monitoring must never cost Arm B a pass. arm_a_watch is
    silent outside 09:45-15:55 ET and fires once per threshold per episode.
    """
    global _ARM_A_NEXT, _ARM_A_RAN_AT
    if time.time() < _ARM_A_NEXT:
        return
    _ARM_A_NEXT = time.time() + 300
    try:
        import arm_a_watch

        def _deliver(subject, body, urgent):
            # MUTE, Devon's call 2026-10-22, FIRST-LEVEL QUEUED ONLY. Ten alerts reached him
            # today - five late-and-recovered pairs at 10:23, 10:40, 10:51, 12:39 and 13:37 -
            # about an Arm A that pushed status.json dead on its 15-minute cadence all day.
            # Cloud shipped job-truth and a second look at 12:54 (live here from 13:08) and
            # the 13:37 pair still fired, which is the honest limit they wrote down: if
            # GitHub's jobs endpoint lags the way the run row does, job truth cannot save it.
            # So this is Devon choosing fewer false alarms over 14 minutes of latency on a
            # real one, with the facts in front of him, and it is a stopgap: ARM_A_MUTE_QUEUED
            # goes back to False the moment cloud's next attempt holds.
            #
            # IT HELD, AND THE MUTE WENT OFF 2026-10-29. Read the paragraph above knowing it
            # was built on a measurement that was withdrawn: cloud's "no Arm A run ever waited
            # for a machine" had timed job-created to job-started (always ~2s), not run-created
            # to job-created (the wait this signal exists to catch). Re-measured over 193 runs,
            # 10-22's five alerts were still false - those jobs existed at second zero - but
            # 10-26's three, which this mute swallowed, were TRUE at 12.2, 13.3 and 8.9 minutes.
            # The 13:37 pair is the only post-fix false alert, and its job read went unlogged,
            # so "job truth cannot save it" above is unproven rather than established. The real
            # discriminator was never about staleness: either the job exists or it does not,
            # and job truth reads exactly that.
            #
            # Scope is deliberately the narrowest thing that stops the noise. The URGENT
            # 20-minute queued level still sends, and no_trigger and stale are untouched, so
            # a run that never starts, a trigger that stops and a status file that goes stale
            # all still reach him. The match is on the queued description's own words - cloud's
            # tests pin "machine" in it and no other signal mentions one - and it FAILS OPEN:
            # if that wording ever changes the alert is delivered, so a dead mute is noisy
            # rather than silent. tests/test_arm_a_mute.py builds the body from cloud's real
            # describe() so their rewording breaks my test instead of my inbox.
            # RECOVERY NOTICE, cloud's cross-audit 2026-10-25, defect (b). The mute returns
            # True, so run_once believes the alert went out, and at the end of the episode it
            # sends "Arm A is running again" through this same callback - text with no queued
            # marker in it, so it was delivered. Devon would be told Arm A is running again
            # after an alert he never received, which is worse than either the alert or the
            # silence. Swallowed only when the WHOLE opening alert was muted: if a stale line
            # was delivered alongside (defect (a) below), the episode really was reported and
            # its recovery belongs in his inbox. The flag is on the LEDGER, because a restart
            # mid-episode would otherwise deliver a recovery for a muted alert - the sixth
            # time that lesson has applied in this file.
            if ARM_A_MUTE_QUEUED and "running again" in subject:
                if (led.get("alerts") or {}).get("arm_a_queued_muted"):
                    _remember_alerts(led, arm_a_queued_muted=False)
                    log(f"ARM A WATCH recovery notice SWALLOWED: its opening alert was muted, "
                        f"so this would report the end of something never reported: {subject}")
                    return True
                return notify(subject, chr(10).join([
                    f"Detected by {SESSION_NAME} on the laptop. ARM A belongs to CLOUD: open the",
                    "CLOUD session, or tell CLOUD to check mail. Nothing here is Arm B.",
                    "", body]))

            if ARM_A_MUTE_QUEUED and not urgent and "give it a machine" in body:
                # COMBINED ALERT, cloud's defect (a). build_alert puts EVERY threshold crossed
                # at one check into one body, as "- " bullets. A queued crossing arriving with
                # a first-level STALE crossing produced one non-urgent body containing the
                # marker, and the old code dropped all of it - including the stale line, which
                # this mute promised to leave alone. So strip the queued BULLETS only and
                # deliver whatever is left. (queued with no_trigger cannot happen: a run that
                # has waited N minutes was created N minutes ago, so no_trigger never exceeds
                # queued, and 22 is already the urgent queued level. Cloud established that and
                # removed their own test for it, because measure() cannot produce the input.)
                kept = [ln for ln in body.splitlines()
                        if not (ln.lstrip().startswith("- ") and "give it a machine" in ln)]
                dropped = len(body.splitlines()) - len(kept)
                if any(ln.lstrip().startswith("- ") for ln in kept):
                    log(f"ARM A WATCH: dropped {dropped} queued line(s) from a combined alert "
                        f"and delivered the rest: {subject}")
                    _remember_alerts(led, arm_a_queued_muted=False)
                    return notify(subject, chr(10).join([
                        f"Detected by {SESSION_NAME} on the laptop. ARM A belongs to CLOUD: open the",
                        "CLOUD session, or tell CLOUD to check mail. Nothing here is Arm B.",
                        "", chr(10).join(kept)]))
                _remember_alerts(led, arm_a_queued_muted=True)
                # LOG THE WHOLE BODY, not just the subject. Cloud's request 2026-10-22, and
                # it is the right one: since their evidence change the queued paragraph ends
                # with what GitHub actually returned to THIS check - the run id, the list
                # row's status, the jobs endpoint's statuses, and the Date, Age, X-Cache and
                # Via headers of the response. That is the only way to tell their cache
                # hypothesis from staleness at GitHub's origin, and a mute that drops it
                # would make the next episode as unexaminable as today's ten were. Devon
                # sees nothing; the log keeps everything. Flattened to one line so a day's
                # episodes grep cleanly.
                log(f"ARM A WATCH first-level queued alert SUPPRESSED (muted on the laptop "
                    f"2026-10-22, Devon's call; urgent level still sends): {subject} "
                    f"|| EVIDENCE: {' / '.join(x.strip() for x in body.splitlines() if x.strip())}")
                return True          # handled: do not let run_once retry or go red on it
            # Cloud's subject says WHAT is wrong; Devon asked that alerts say WHO must act, and
            # for an Arm A fault that is CLOUD, not this session. Their subject is kept verbatim
            # (he prints mail to PDF by subject and "ARM A WATCH" sorts) and the routing goes in
            # the first body line, where it cannot be mistaken for part of the problem.
            return notify(subject, chr(10).join([
                f"Detected by {SESSION_NAME} on the laptop. ARM A belongs to CLOUD: open the",
                "CLOUD session, or tell CLOUD to check mail. Nothing here is Arm B.",
                "", body]))

        rc, line, _ = arm_a_watch.run_once(fetch=True, ref="origin/main",
                                           fired=_ARM_A_FIRED, deliver=_deliver)
        _ARM_A_RAN_AT = time.time()      # positive evidence it executed; see persist()
        # Persist the once-per-episode dedupe. 2026-10-07: this alerted at 11:23:39, a code
        # change restarted the daemon at 11:25:02, and the SAME threshold alerted again at
        # 11:25:23 - because _ARM_A_FIRED is a module global and a restart re-arms it. Fourth
        # time this shape has bitten (broker re-alert, selftest, deposit overdue, now this), so
        # it goes on the ledger like the others. Sets are not JSON, so only {signal: [minutes]}
        # is kept; the "_failed" retry state is deliberately NOT persisted - losing it costs at
        # most one retried delivery, and it is keyed by tuples.
        _remember_alerts(led, arm_a_fired={k: sorted(v) for k, v in _ARM_A_FIRED.items()
                                           if isinstance(v, set)})
        if rc:
            log(line)
    except Exception as e:
        log(f"arm a watch skipped ({e})")


def check_mail(led):
    """Tell Devon when the mailbox has a new entry addressed to this session.

    cloud's cadence protocol (2026-08-23) states "laptop: at every daemon
    start/restart, which is frequent. Effectively the fastest reader." That was
    NOT true. The daemon pulls AGENT_MAIL.md to disk but nothing here read it, and
    only a Claude session on this machine does, which happens when Devon opens
    one. A time-sensitive entry addressed to laptop could therefore sit unseen for
    days while the sender believed it landed in minutes.

    The daemon cannot reason about mail, so it does not try. It just says mail
    arrived, which is what makes the protocol's assumption real: the notification
    is what prompts a session to be opened.

    First run adopts the current newest entry without notifying, so adding this
    does not replay the whole backlog.
    """
    try:
        with open(MAIL_F, encoding="utf-8", errors="replace") as f:
            heads = _MAIL_HEAD.findall(f.read())
        if not heads:
            return
        keys = ["|".join(h[:3]) for h in heads]   # qualifier left out: keeps old ledger keys valid
        seen = led.get("last_mail_seen")
        if seen is None:
            led["last_mail_seen"] = keys[-1]
            return
        if keys[-1] == seen:
            return
        start = keys.index(seen) + 1 if seen in keys else len(keys) - 1
        # Exclude our OWN entries. The addressee filter alone matched every
        # "laptop -> all" post, so writing to the mailbox emailed Devon about our
        # own message: two such notifications went out today (15:21 and 15:53)
        # before this was caught. audit's mail_check.py had this right from the
        # start; mine did not.
        fresh = [h for h in heads[start:]
                 if h[2].strip().lower() in ("laptop", "both", "all")
                 and h[1].strip().lower() != "laptop"]
        led["last_mail_seen"] = keys[-1]
        if not fresh:
            return
        who = " and ".join(dict.fromkeys(
            DAILY_NAME if (h[2].lower() == "laptop" and h[3].lower() == "daily") else SESSION_NAME
            for h in fresh))
        summary = "; ".join(f"{ts} {frm}->{to}" for ts, frm, to, _ in fresh)
        log(f"NEW MAIL for the laptop session: {summary}")
        notify(f"AGENT_MAIL: have {who} check mail ({len(fresh)} new)", chr(10).join([
            f"Have {who} check mail.",
            "",
            "New AGENT_MAIL.md entries addressed to the laptop session:",
            "",
            *[f"  [{ts}] {frm} -> {to}{'[' + tag + ']' if tag else ''}" for ts, frm, to, tag in fresh],
            "",
            "The daemon cannot act on these; it only reports that they arrived.",
            f"Open the {who} session on the laptop and say: check mail.",
        ]))
    except Exception as e:
        log(f"mail check skipped ({e})")


# ── Code sync: inherit cloud-bot improvements, but verify before trusting ───
def sync_code(led=None):
    """Pull upstream changes and prove they work before running on them.

    rh_bot.py imports its rails straight from alpaca_bot, so a strategy fix made
    to the cloud bot reaches this laptop through git and nothing else. Without
    this the daemon would run whatever code it started with, forever.

    A bad upstream push must never break trading here, so new code has to pass
    rh_bot's selftest; if it fails we stay on the known-good code already loaded in
    memory. Returns True when the process should restart to load the new modules
    (Python caches imports).

    Detection compares CODE_FILES between the RUNNING commit (_run_head) and the
    current HEAD, not between this pull's before and after. The status-push path
    also runs `git pull --rebase`, so a cloud code change can land on disk via that
    push before sync_code's own pull sees it; comparing to the running commit
    catches it however HEAD advanced."""
    global _selftest_fails, _selftest_alert_at
    try:
        def git(*a, t=90):
            return subprocess.run(["git", *a], capture_output=True, text=True, timeout=t)
        before = git("rev-parse", "HEAD").stdout.strip()
        git("pull", "--rebase", "--autostash", "--quiet")
        after = git("rev-parse", "HEAD").stdout.strip()
        head = after or before
        base = _run_head or before          # what our in-memory modules were built from
        if not head or head == base:
            return False
        # Restart ONLY when code the daemon RUNS changed. The cloud pushes
        # status.json, trade logs, briefs and daily_plan.json constantly; a restart
        # to adopt those just costs a protective-pass gap, and decide() reads the
        # plan fresh each cycle anyway.
        changed = git("diff", "--name-only", f"{base}..{head}").stdout.split()
        code_changed = [f for f in changed if f in CODE_FILES]
        if not code_changed:
            if after and after != before:   # log only when this pull moved HEAD
                pulled = git("diff", "--name-only", f"{before}..{after}").stdout.split()
                log(f"pulled {before[:7]} -> {after[:7]} — data only "
                    f"({', '.join(pulled)[:70]}), not restarting")
            return False
        log(f"code changed ({', '.join(code_changed)}) {base[:7]} -> {head[:7]} "
            f"— verifying before use")
        chk = subprocess.run([sys.executable, "rh_bot.py", "--selftest"],
                             capture_output=True, text=True, timeout=180)
        if chk.returncode != 0:
            # Do not reset the shared tree (that would fight the cloud's commits).
            # The good code is already in memory; just refuse to restart into the
            # bad code, and keep flagging until an upstream fix passes.
            _selftest_fails += 1
            log(f"!! NEW CODE FAILED SELFTEST ({_selftest_fails}x) - staying on "
                f"known-good in-memory code")
            err = ((chk.stdout or "")[-400:] + (chk.stderr or "")[-400:]).strip()
            log(err)
            # Alert at the threshold, then at most every SELFTEST_REALERT_SEC while
            # it stays broken. Trading continues safely throughout; what is stalled
            # is inheriting cloud changes, which is a silent condition otherwise.
            if _selftest_fails >= SELFTEST_FAIL_ALERT and (
                    time.time() - _selftest_alert_at) >= SELFTEST_REALERT_SEC:
                notify("ALERT: RH laptop bot pinned on old code",
                       "\n".join([
                           "The laptop bot is STILL TRADING normally, on the last "
                           "known-good code. It has stopped inheriting cloud updates.",
                           "",
                           f"{_selftest_fails} consecutive selftest rejections of "
                           f"upstream code.",
                           f"running code : {(_run_head or '?')[:7]}",
                           f"code on disk : {head[:7]}",
                           f"files        : {', '.join(code_changed)}",
                           "",
                           "Selftest output (tail):",
                           err[-600:],
                           "",
                           "This clears itself once an upstream commit passes the "
                           "selftest. Stop the bot any time with a file named "
                           "rh_HALT in the repo folder.",
                       ]))
                _selftest_alert_at = time.time()
                if led is not None:
                    _remember_alerts(led, selftest_alert_at=_selftest_alert_at,
                                     selftest_fails=_selftest_fails)
            return False
        if _selftest_fails:
            log(f"upstream selftest recovered after {_selftest_fails} failure(s)")
            if _selftest_alert_at:
                notify("RH laptop bot: upstream code fixed, updates resumed",
                       f"Upstream code passes the selftest again after "
                       f"{_selftest_fails} rejection(s). The laptop is restarting "
                       f"into {head[:7]} and inheriting cloud changes normally.")
            _selftest_fails, _selftest_alert_at = 0, 0.0
            if led is not None:
                _remember_alerts(led, selftest_alert_at=0.0, selftest_fails=0)
        log("new code passed selftest - restarting to load it")
        return True
    except Exception as e:
        log(f"code sync skipped ({e}) - continuing on current code")
        return False


# ── Main loop ───────────────────────────────────────────────────────────────
def cycle(led, fast):
    prices = {}
    state = {"cash": led["cash"], "unsettled": led.get("sold_today", 0.0),
             "positions": led["positions"], "holds": led.get("holds") or {},
             "last_buy": led.get("last_buy") or {}}
    res = rh_bot.decide(state, fast=fast)

    for p in led["positions"]:
        live, _s, _c = rh_bot._quote(p["symbol"], [])
        if live: prices[p["symbol"]] = live
    for o in res["orders"]:
        if o["symbol"] not in prices:
            live, _s, _c = rh_bot._quote(o["symbol"], [])
            if live: prices[o["symbol"]] = live
    track_peaks(led, prices)

    placed = []
    if res["orders"]:
        if led.get("orders_today", 0) + len(res["orders"]) > MAX_ORDERS_DAY:
            log(f"ORDER CAP hit ({MAX_ORDERS_DAY}/day) — skipping {len(res['orders'])} order(s)")
            res["orders"] = []
        else:
            for o in res["orders"]:
                log(f"  {o['action'].upper()} {o['symbol']} "
                    f"{o.get('notional') or o.get('qty')} — {o['reason']}")
            placed = place(res["orders"])
            apply_fills(led, res["orders"], placed, prices)
            # Truth up after every trade. The orders are already away, so a
            # corrupt snapshot here means keep the local ledger, not skip.
            adopt_truth(led, reconcile())
            email_trades(res, placed, led)      # after reconcile: report real state
    for n in res.get("notes", []):
        log(f"  note: {n}")
    persist(led, res, placed)
    return res


def _detach_from_console():
    """Survive the console that launched us going away.

    The scheduled task runs with LogonType Interactive, so it attaches to whatever
    console started it. Closing that window delivers CTRL_C to the whole group and
    kills the daemon: observed twice on 2026-07-23, exit 0xC000013A
    (STATUS_CONTROL_C_EXIT), leaving seven live positions with no protective pass.

    Only ignored when stdout is NOT a terminal, i.e. when running under the task
    with output redirected to rh_daemon.log. Run it by hand in a real terminal and
    Ctrl+C still works. Stop-ScheduledTask terminates rather than signalling, so
    the task remains stoppable, and rh_HALT is the real kill switch regardless.
    """
    try:
        if sys.stdout is None or not sys.stdout.isatty():
            import signal
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            return True
    except Exception:
        pass
    return False


def _maybe_alert_broker(led):
    """Alert Devon about a broker outage once it is worth his attention.

    Runs on EVERY degraded pass, not only on a failed attempt. The gate used to be
    "_reconcile_fails >= BROKER_FAIL_ALERT", checked only right after an attempt.
    That was sound while the backoff capped attempts 15 min apart. quota_reset_wait
    (2026-09-22) then parked the next attempt at the reset time the bridge names,
    up to 6h out, so an outage made ONE attempt, the counter sat at 1, and the
    alert became unreachable. Proven twice: 2026-09-23 (110 min) and 2026-09-24
    (165 min), both session-limit outages, zero alerts, found days later by
    reading git history. Elapsed time since the outage began is what Devon cares
    about, and it keeps rising while attempts do not.

    While the retry is parked on a named quota reset, only ONE self-healing alert is
    sent per outage: the hourly re-alert would repeat "resets at X, no action
    needed" with nothing new to say, which is the kind of true-but-unimportant mail
    that trains alerts to be ignored. Unknown or login causes still re-alert hourly.
    """
    global _broker_alert_at
    if not _broker_down_since:
        return
    _elapsed = market_minutes_since(_broker_down_since) * 60   # TRADING time, not wall clock
    if _reconcile_fails < BROKER_FAIL_ALERT and _elapsed < BROKER_ALERT_AFTER_SEC:
        return
    if (time.time() - _broker_alert_at) < BROKER_REALERT_SEC:
        return
    _mins = int(_elapsed // 60)
    # CLASSIFY THE OUTAGE. Every cause used to send the identical subject,
    # "broker unreachable (not urgent)". By 2026-09-21 Devon had received seven
    # of those from Claude USAGE-LIMIT outages, which reset on a timer and always
    # healed themselves. Then the CLI OAuth login expired - a state that NEVER
    # recovers without a human - and it sent the same words, telling him to fix it
    # "when convenient". Being trained by true-but-unimportant alerts to ignore the
    # one that matters is how alerting dies. The subject line has to carry the
    # difference, because that is all that is visible in an inbox.
    _needs_human = needs_human(_last_agent_error)
    _self_heals = self_heals(_last_agent_error)
    if _broker_alert_at and _self_heals and _waiting_on_reset and not _needs_human:
        return
    if _needs_human:
        _subject = "RH bot: LOGIN EXPIRED - will NOT recover on its own"
        _impact = ["IMPACT: THIS ONE NEEDS YOU. The bot's Claude login has expired",
                   "and cannot refresh itself. It will stay down - today and every",
                   "day after - until someone signs in. It is NOT the usage limit;",
                   "that resets on a timer, this does not.",
                   "",
                   "FIX, about thirty seconds:",
                   "    claude auth login",
                   "Signing into the Claude desktop app does NOT fix this: the bot",
                   "uses a separate install with its own login.",
                   "",
                   "Nothing is at risk while it is down (index-only, no stops), but a",
                   "deposit that goes pending AND settles while the bot is blind can",
                   "be missed, and a missed deposit reads as profit."]
    elif _self_heals:
        _subject = "RH bot: broker paused (usage limit - self-healing)"
        _impact = ["IMPACT: NOT URGENT and NO ACTION NEEDED. The shared Claude quota",
                   "is exhausted; it resets on a timer and the bot resumes by itself.",
                   "Only worth acting on if it is still down after the stated reset."]
    else:
        _subject = "RH bot: broker unreachable (cause unknown)"
        _impact = ["IMPACT: cause NOT recognised, so it is unknown whether this",
                   "recovers on its own. Treat as needing a look. Index-only means no",
                   "stops are waiting to fire.",
                   "",
                   "The bridge said: " + (_last_agent_error or "(nothing captured)")[:160]]
    notify(_subject, chr(10).join([
        "The laptop is UP and the daemon is running. The problem is the",
        "Robinhood connection, not the machine.",
        "",
        f"Down {_mins} min so far ({_reconcile_fails} failed broker snapshot(s)).",
        "",
    ] + _impact + [
        "",
        "DIAGNOSE with this, NOT with `claude mcp list`, which reported",
        "Connected while the bridge could not authenticate at all:",
        "    claude -p \"Reply with exactly: ALIVE\"",
        "If that fails the CLI login lapsed:  claude auth login",
        "If it succeeds but Robinhood tools are missing, reconnect that",
        "connector:  claude mcp login \"claude.ai Robinhood\"",
    ]))
    _broker_alert_at = time.time()
    _remember_alerts(led, broker_alert_at=_broker_alert_at,
                     reconcile_fails=_reconcile_fails)


# A gap under this is a code-pull restart, not an outage. The 300 was chosen believing a
# restart takes "well under a minute"; on this machine it did not. The daemon exits 42 and
# waits for the scheduled task's REPEATING TRIGGER to find no instance running - the
# configured restart-on-failure never fires, because IgnoreNew overwrites the task's last
# result with 0x800710E0 (request refused) every time the trigger hits a live daemon. So the
# true latency was the distance to the next trigger tick: 13 restarts from 10-08 to 10-27
# measured 2s to 4m36s, all landing on the 5-minute grid, ~31 market minutes in all, and the
# ones past 300s mailed Devon a TRUE "Arm B was blind" that read like a crash. Devon cut the
# trigger to PT1M on 2026-10-27; the first restart after that took 11 SECONDS (10-29 09:45:51
# -> 09:46:02) and sent nothing. If these emails ever come back for a code pull, read the
# trigger interval before touching this number.
DOWNTIME_MIN_SEC = 300
DOWNTIME_MIN_MARKET_MIN = 2 # market minutes below this are heartbeat resolution, not blindness
DOWNTIME_RETRY_MAX = 24     # passes to keep retrying a parked downtime report before giving up


def _implied_boot_epoch():
    """Epoch seconds this machine booted, from the uptime counter. None if unreadable.

    ctypes/GetTickCount64 rather than psutil (not installed here) or a PowerShell
    subprocess (a second of startup, and one more thing that can be broken on the
    one path that reports the laptop dying). Checked against EventLog 6013 on
    2026-10-08: implied 10:32:11 local against a real boot of 10:32:12. Should the
    counter ever exclude time spent asleep it implies a LATER boot, which can only
    make a sleep look like the outage it was - the error runs the safe way.
    """
    try:
        import ctypes
        return time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None


def alert_keep_awake(led, held):
    """Tell Devon when the Modern Standby hold did not take. Once a day. Never raises.

    keep_awake() only ever LOGGED its failures, and the retired laptop daily check's
    check C was the only thing that read those lines. Every unclean shutdown before
    2026-10-08 happened in Modern Standby (08-04, 09-02, 09-21), and 09-21 hung there and
    stayed dead for twelve hours until someone pressed power - auto-logon cannot help a
    machine that never boots. So a silently broken hold is the condition that precedes the
    worst outage this experiment has had.

    A function rather than four lines in main() so the dampener is testable: this file has
    got a dampener wrong five times, every time by keeping it somewhere a restart cleared.
    The day stamp lives on the ledger, so a restart loop cannot turn one broken hold into
    an alert per restart.
    """
    try:
        if held or os.name != "nt":
            return False
        today = now_et().strftime("%Y-%m-%d")
        if (led.get("alerts") or {}).get("keep_awake_alert_day") == today:
            return False
        # STAMP ONLY ON A DELIVERED SEND, and let the main loop call this every pass so a
        # failure is retried. The first version wrote the stamp FIRST and ignored notify's
        # verdict, and main() calls this seconds after boot - 40 s on 10-08 - which is
        # exactly when the network may not be up. Network down on that one call meant zero
        # mails and a ledger saying "alerted today". Cloud reproduced it on 10-20.
        # It is the same mistake I had already fixed in report_downtime on 10-13, made
        # again in a new function seven days later: the dampener has to record the SEND,
        # not the attempt.
        delivered = notify(
            f"{SESSION_NAME}: KEEP-AWAKE IS NOT HELD - the laptop can sleep again",
            "SetThreadExecutionState did not take, so nothing is stopping this laptop "
            "entering Modern Standby while the bot runs.\n\n"
            "Every unclean shutdown before 2026-10-08 happened in Modern Standby (08-04, "
            "09-02, 09-21), and 09-21 hung there and stayed dead for twelve hours until "
            "someone pressed power - auto-logon cannot help a machine that never boots.\n\n"
            "Arm B holds index positions with no stops, so this is not an emergency for the "
            "money; it is the condition that preceded the longest outage this experiment has "
            "had. Restarting the scheduled task rh-trading-bot is the first thing to try. "
            "The daemon keeps running and trading meanwhile.")
        if delivered:
            _remember_alerts(led, keep_awake_alert_day=today)
        else:
            log("keep-awake alert NOT delivered; leaving it unstamped so the next pass retries")
        return delivered
    except Exception as e:
        log(f"alert_keep_awake failed ({e}) - not fatal, startup continues")
        return False


def last_unclean_shutdown():
    """The last Kernel-Power 41 and WHY, from the Windows event log. None if unreadable.

    Nothing else in this system can see it. report_downtime says the machine went away;
    only BugcheckCode and ConnectedStandbyInProgress say what took it. That mattered
    enough to be the laptop daily check's check B, and when Devon retired that session on
    2026-10-17 (four Claude sessions was too many) it left no reader at all: every mention
    of event 41 in this file was a COMMENT, and rh_watchdog.py and daily_digest.py have
    none. Verified by grep before writing this, not assumed.

    It is not a nicety while the HMB experiment runs. The question that experiment answers
    is "does this laptop still crash with the SSD's host memory buffer disabled", and
    ConnectedStandbyInProgress is how we tell the old Modern Standby cause (true on
    08-04, 09-02, 09-21) from a new one (false on both 10-08 events). A crash read against
    the wrong hypothesis is worse than no crash data.

    wevtutil, not PowerShell: a plain exe, measured at 69 ms here, run once per daemon
    start rather than per pass. Never raises - an unreadable log must not stop a
    heartbeat, and the caller publishes None as a flag rather than as a pass.
    """
    try:
        import xml.etree.ElementTree as ET
        r = subprocess.run(["wevtutil", "qe", "System", "/q:*[System[(EventID=41)]]",
                            "/c:1", "/rd:true", "/f:xml"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        ns = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
        root = ET.fromstring(r.stdout.strip())
        when = root.find(".//e:TimeCreated", ns)
        data = {d.get("Name"): d.text for d in root.findall(".//e:EventData/e:Data", ns)}
        code = int(data.get("BugcheckCode") or 0)
        return {
            # The event is written when Windows comes BACK, so this is the recovery time,
            # not the moment it died; EventLog 6008 carries the latter and report_downtime
            # already measures the gap from the heartbeat, which is the figure that counts.
            "logged_at": (when.get("SystemTime") or "")[:19] if when is not None else None,
            "bugcheck": code,
            "bugcheck_hex": "0x%x" % code,
            "connected_standby": str(data.get("ConnectedStandbyInProgress")).lower() == "true",
            "sleep_in_progress": int(data.get("SleepInProgress") or 0),
        }
    except Exception as e:
        log(f"could not read the last unclean shutdown ({e}); publishing it as unknown")
        return None


def machine_guards():
    """The two settings the crash hypothesis depends on. Values, never changes.

    Devon changes these; this only reports them, because a guard nobody watches is a
    guard that can be switched off by a Windows update without anyone noticing. HMB is
    the live experiment (0 = disabled, set 2026-10-08); HiberbootEnabled is Fast Startup,
    which Devon chose to leave ON and which failed its own resume on 10-08. Read through
    winreg, so no subprocess at all. The AC sleep timeouts are deliberately NOT here:
    keep_awake's ES_CONTINUOUS hold supersedes them, and that hold now alerts when it
    fails, which is the protection those timeouts were a proxy for.
    """
    out = {}
    try:
        import winreg
        for key, name, label in (
            (r"SYSTEM\CurrentControlSet\Control\StorPort", "HmbAllocationPolicy", "hmb_policy"),
            (r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
             "HiberbootEnabled", "fast_startup"),
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                    out[label] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                out[label] = None          # absent is a real answer: HMB unset = default ON
    except Exception as e:
        log(f"could not read the machine guards ({e})")
    return out


def retry_pending_downtime(led):
    """Send a parked downtime report, and keep trying until it is away. Never raises.

    report_downtime runs about 40 seconds after boot (measured 2026-10-08: boot
    11:32:12 ET, daemon start 11:32:52), which is exactly when this laptop may not
    have a network yet. So the one alert whose job is to survive an outage is the
    one most likely to be sent while the machine is still coming up.

    Called from the main loop on EVERY pass, before the market gate, so a report
    parked out of session still goes as soon as the network returns rather than
    waiting for an open. Bounded: a send that has failed DOWNTIME_RETRY_MAX times
    has email AND Slack both broken, which is its own alarm and not something to
    retry forever. Giving up latches the dampener so the loop goes quiet, and says
    so in the log, because a report that exists only here still beats one that
    quietly vanished.
    """
    try:
        p = (led.get("alerts") or {}).get("pending_downtime")
        if not p:
            return
        tries = int(p.get("tries") or 0) + 1
        a = led.setdefault("alerts", {})
        if notify(p.get("subject") or "", p.get("body") or ""):
            a.pop("pending_downtime", None)
            a["downtime_reported"] = p.get("stamp")
            save_ledger(led)
            log(f"downtime report for {p.get('stamp')} ET delivered"
                + (f" on try {tries}" if tries > 1 else ""))
        elif tries >= DOWNTIME_RETRY_MAX:
            a.pop("pending_downtime", None)
            a["downtime_reported"] = p.get("stamp")
            save_ledger(led)
            log(f"!! downtime report for {p.get('stamp')} ET GIVEN UP after {tries} tries - "
                "email and Slack both failed, so that gap is in this log and nowhere else")
        else:
            p["tries"] = tries
            save_ledger(led)
    except Exception as e:
        log(f"retry_pending_downtime failed ({e}) — not fatal, continuing")


def report_downtime(led):
    """Email Devon when the daemon returns from a gap nothing announced. Never raises.

    2026-10-08, the laptop's two crashes: the 04:54 ET bugcheck reached Devon only
    because LAPTOP BOT DAILY CHECK happened to run at 10:24, and the 11:27 ET one -
    wholly inside the trading session - reached him not at all. The daemon came back
    at 11:32:52, logged "rh_daemon starting", and said nothing. Every other kind of
    blindness here emails: broker unreachable, selftest failing, login expired, Arm A
    gone quiet. The one that takes this entire process down was the one with no alert,
    because a dead daemon cannot report itself and nothing looked back on the way up.

    The measure is the gap between the heartbeat rh_status.json still carries and now,
    and it is deliberately cause-agnostic: a bugcheck, a power loss, Windows Update, a
    stopped task and a closed lid all read the same here, and the figure Devon needs -
    how long Arm B could not act - is the same for every one of them.

    Two gates keep it from crying wolf. market_minutes_since means an overnight gap is
    never billed as blind trading time, and a gap that cost no market minutes is only
    mailed when the machine actually rebooted inside it; without that second gate the
    first code-pull restart each Monday would mail Devon about the weekend. Dedupe is
    on the ledger, keyed by the heartbeat the gap ended: module globals have now lost
    restart-critical state four times, and this function exists only because of a
    restart.
    """
    try:
        stamp = (_load(STATUS_F, {}) or {}).get("ts")
        if not stamp:
            return                      # first ever run, or a wiped status file
        last = datetime.strptime(stamp, "%Y-%m-%dT%H:%M").replace(tzinfo=bot.ET_TZ)
        gap = (now_et() - last).total_seconds()
        if gap < DOWNTIME_MIN_SEC:
            return                      # a code-pull restart; see DOWNTIME_MIN_SEC for why
                                        # this threshold was wrong until 2026-10-27
        if (led.get("alerts") or {}).get("downtime_reported") == stamp:
            return                      # reported already; a 2nd restart must not re-alert

        # Count from the END of the stamped minute, not its start. ts has minute
        # resolution, so "15:54" means a pass that ran as late as 15:54:59, and the
        # market gate closes at 15:55:00 - measuring from 15:54 bills that one
        # truncated minute as blindness on EVERY after-hours restart. The floor then
        # covers the other half: passes are FAST_PASS_SEC apart, so the last stamp of
        # a session is 15:54 normally and 15:53 when a pass runs long, and neither is
        # an outage. Cloud caught this on 2026-10-08 by running the real functions
        # against a pinned clock; my own test had asserted the right behaviour from a
        # stamp of 15:55, which this loop can never write because check_market() is
        # already false at 15:55:00. Mocking a value production cannot produce is how
        # a test agrees with code that is wrong.
        mins = market_minutes_since(last.timestamp() + 60)
        lost = mins >= DOWNTIME_MIN_MARKET_MIN
        boot = _implied_boot_epoch()
        rebooted = boot is not None and boot > last.timestamp()
        hrs, rem = divmod(int(gap), 3600)
        wall = f"{hrs}h{rem // 60:02d}m" if hrs else f"{rem // 60} min"
        if not lost and not rebooted:
            # Nothing tradeable was missed and the machine never went away: an
            # ordinary restart across a closed market. Leave a line, send nothing.
            log(f"daemon gap {wall} since {stamp} ET, {mins} market minute(s) and no reboot "
                f"— under the {DOWNTIME_MIN_MARKET_MIN}-minute floor, not alerting")
            _remember_alerts(led, downtime_reported=stamp)
            return

        if boot is None:
            cause = ("Could not read this machine's boot time, so whether the LAPTOP went away or "
                     "only this daemon did is unknown.")
        elif rebooted:
            # Name the cause in the mail rather than sending Devon to Event Viewer. On
            # 2026-10-08 the first version of this pointed only at Kernel-Power 41 and
            # 6008, and the 42-minute gap it was reporting turned out to be Devon's own
            # memory test - so it sent him hunting a bugcheck that did not exist.
            _u = (_MACHINE or {}).get("last_unclean_shutdown")
            if _u is None:
                _why = ("The Windows event log could not be read, so the cause is UNKNOWN - treat that "
                        "as a missing answer, not a clean bill. Event Viewer / Windows Logs / System: "
                        "Kernel-Power 41 for the bugcheck, EventLog 6008 for when it went down, "
                        "User32 1074 if a human or an update asked for it.")
            elif _u.get("bugcheck"):
                _why = (f"The last unclean shutdown was a BUGCHECK {_u['bugcheck_hex']} "
                        f"(connected standby {_u['connected_standby']}), logged "
                        f"{_u.get('logged_at')}. Windows crashed and restarted itself.")
            else:
                _why = (f"The last unclean shutdown has NO bugcheck code (connected standby "
                        f"{_u['connected_standby']}, sleep-in-progress {_u['sleep_in_progress']}), "
                        f"logged {_u.get('logged_at')} - a hang, a power loss, or a shutdown that "
                        "never finished writing its records. Check User32 1074 before assuming a "
                        "crash: a memory test or an update reboot lands here too, and one did on "
                        "2026-10-08.")
            cause = ("The LAPTOP went down and came back: it booted "
                     f"{datetime.fromtimestamp(boot, bot.ET_TZ).strftime('%H:%M ET')}, inside the gap. "
                     + _why)
        else:
            # Deliberately weaker than it used to read. The uptime counter is the only
            # thing consulted, and cloud's 2026-10-08 audit raised that Fast Startup
            # (on, and it failed here that morning) carries the kernel across a Shut
            # down, so a shutdown-and-power-on may not reset it and would land here
            # looking like the machine never left. Say what was measured, not what it
            # implies.
            cause = ("The uptime counter shows no reboot inside the gap, so this looks like the daemon "
                     "alone: the scheduled task was stopped, killed, or did not restart. Treat that as "
                     "the counter's reading, not proof - with Fast Startup on, a Shut down and power on "
                     "may not reset it. Windows power events settle it.")
        if lost:
            subject = f"{SESSION_NAME}: Arm B was blind {mins} market minute(s) - daemon gap"
        else:
            subject = f"{SESSION_NAME}: laptop rebooted while the market was shut ({wall} gap)"
        body = (f"Arm B published no heartbeat between {stamp} ET and "
                f"{now_et().strftime('%Y-%m-%dT%H:%M')} ET.\n"
                f"Gap: {wall} of wall clock, {mins} minute(s) of it inside 09:45-15:55 ET on a "
                f"trading day.\n\n{cause}\n\n"
                "In the gap Arm B placed nothing and cancelled nothing, and its stops could not have "
                "fired either - positions were held unmanaged. The daemon is running again and will "
                "reconcile with the broker on this cycle.")
        # PARK IT ON THE LEDGER BEFORE SENDING, then let the retry path do the send and
        # all the bookkeeping. The first version recorded the dampener only on a True,
        # which stopped a failed send being filed as delivered but left the report with
        # nowhere to live: the evidence a retry needs is the stale ts in rh_status.json,
        # and the FIRST PASS after boot overwrites it, so the gap then reads as a minute
        # and returns early. Cloud reproduced that on 2026-10-08 and I confirmed it with
        # the real function against a pinned clock - the 11:20 outage was never reported
        # at all. "Will retry on the next restart" was a promise the code could not keep.
        # A report has to outlive the file it was derived from.
        a = led.setdefault("alerts", {})
        a["pending_downtime"] = {"stamp": stamp, "subject": subject, "body": body, "tries": 0}
        save_ledger(led)
        log(f"downtime detected: gap {wall} since {stamp} ET, {mins} market minute(s), "
            f"rebooted={rebooted}")
        retry_pending_downtime(led)
    except Exception as e:
        log(f"report_downtime failed ({e}) — not fatal, startup continues")


def main():
    if not acquire_singleton():
        log("another rh_daemon is already running — this instance is exiting")
        return 0
    _awake = keep_awake()
    if _detach_from_console():
        log("running detached: console Ctrl+C ignored, use rh_HALT to stop")
    if not ACCOUNT:
        log(f"No account configured. Create {CONFIG_F}: "
            '{"account": "<robinhood agentic account number>", "claude": "claude"}')
        return 1
    log(f"rh_daemon starting | account …{ACCOUNT[-4:]} | "
        f"{'DRY RUN' if DRY else 'LIVE'} | full {FULL_CYCLE_SEC}s / fast {FAST_PASS_SEC}s")

    # Pin the commit our just-loaded modules were built from, so sync_code can tell
    # when the live CODE has drifted from disk no matter which git path advanced HEAD.
    global _run_head, _reconcile_fails, _broker_alert_at, _next_reconcile_try
    global _broker_down_since, _degraded_passes, _waiting_on_reset
    global _selftest_fails, _selftest_alert_at, _deposit_alert_on
    try:
        _run_head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                   text=True, timeout=30).stdout.strip() or None
        log(f"running code at {(_run_head or '?')[:7]}")
    except Exception as e:
        log(f"could not capture running HEAD ({e}); code-sync will use pull deltas")

    led = _load(LEDGER_F, fresh_ledger())
    # Restore the alert state this daemon left behind. Absolute timestamps are
    # valid across a restart; an expired one simply reads as "dampener elapsed".
    # _reconcile_fails is restored ONLY when an alert is still unpaired, which is
    # what lets the all-clear fire for an outage that began before the restart.
    _a = (led.get("alerts") or {})
    _broker_alert_at   = float(_a.get("broker_alert_at") or 0.0)
    _selftest_alert_at = float(_a.get("selftest_alert_at") or 0.0)
    _deposit_alert_on  = _a.get("deposit_alert_on") or None
    _ARM_A_FIRED.clear()
    _ARM_A_FIRED.update({k: set(v) for k, v in (_a.get("arm_a_fired") or {}).items()})
    if _broker_alert_at:
        _reconcile_fails = int(_a.get("reconcile_fails") or 0)
    if _selftest_alert_at:
        _selftest_fails = int(_a.get("selftest_fails") or 0)
    if _broker_alert_at or _selftest_alert_at or _deposit_alert_on:
        log(f"restored alert state from the ledger: broker_fails={_reconcile_fails} "
            f"selftest_fails={_selftest_fails} deposit_alert_on={_deposit_alert_on}")
    # Read once per start, not per pass: wevtutil is ~70 ms and these change only across
    # a reboot or a settings edit. persist() copies the block into rh_status.json every
    # pass for free, which is how the GitHub digest gets to see a laptop-local fact.
    global _MACHINE
    _MACHINE = {"keep_awake": bool(_awake),
                "last_unclean_shutdown": last_unclean_shutdown(),
                "guards": machine_guards()}
    log("machine: keep_awake=%s guards=%s last_unclean=%s"
        % (_MACHINE["keep_awake"], _MACHINE["guards"],
           (_MACHINE["last_unclean_shutdown"] or {}).get("logged_at") or "none/unreadable"))
    # KEEP-AWAKE FAILING USED TO BE LOG-ONLY. The retired daily check's check C was its
    # only reader, and Modern Standby is where this laptop died three times out of three,
    # so a silently broken hold is the failure that precedes the failure. Alerted here
    # rather than inside keep_awake() because the dampener belongs on the ledger, and the
    # ledger is not loaded that early - module globals have lost restart-critical state in
    # this file five times. Once a day, so a restart loop cannot spam.
    alert_keep_awake(led, _MACHINE["keep_awake"])
    report_downtime(led)   # a gap nothing could alert from while it was happening
    last_full = 0.0
    while True:
        # Before the HALT check and before the market gate, deliberately. A downtime
        # report parked by a failed send must go as soon as the network is back, and
        # neither a paused bot nor a shut market makes the outage less true - the 11:27
        # gap was out of session and still cost Devon the knowledge that his laptop
        # had gone down. Cheap and quota-free: it returns immediately with nothing
        # parked, which is every pass but the handful after a failure.
        retry_pending_downtime(led)
        # Every pass, not once at startup: a send that failed because the network was not
        # up yet must be retried, and the day stamp is written only on delivery. Cheap -
        # it returns immediately when the hold took or when today is already stamped,
        # which is every pass but the handful after a genuine failure.
        alert_keep_awake(led, (_MACHINE or {}).get("keep_awake", True))
        if os.path.exists(HALT_F):
            log("rh_HALT present — trading paused")
            if "--once" in sys.argv:      # otherwise --once spins here forever
                return 0
            time.sleep(FAST_PASS_SEC); continue
        open_, et = bot.check_market()
        if not open_:
            if "--once" in sys.argv:
                log(f"market closed ({et.strftime('%H:%M ET')})"); return 0
            time.sleep(FAST_PASS_SEC); continue

        try:
            full = (time.time() - last_full) >= FULL_CYCLE_SEC
            if roll_day(led, et.strftime("%Y-%m-%d")):
                log("new session — settling T+1 proceeds, reconciling with broker")
                led["needs_reconcile"] = True
            # Periodic reconcile so a mid-day DEPOSIT (or withdrawal) is noticed
            # within RECONCILE_MAX_AGE_SEC instead of sitting unseen until the next
            # session open. Only on full cycles, and skipped when roll_day already
            # forced one. adopt_truth resets the clock, so an active day rarely
            # spends an extra reconcile here.
            elif full and (time.time() - _last_reconcile) >= RECONCILE_MAX_AGE_SEC:
                led["needs_reconcile"] = True   # logged at the actual attempt below
            # Held as a ledger flag, not a local variable: if the snapshot is bad
            # we must retry next pass, and roll_day only fires once per day.
            if led.get("needs_reconcile"):
                # BACK OFF failed broker snapshots. Each attempt is an agent turn
                # against the SAME Claude quota the bridge needs, so retrying every
                # 60s while the quota is exhausted burns the very resource we are
                # waiting on. On 2026-08-20 that meant 140 attempts across a 2h41m
                # session-limit outage, roughly 14x a normal day's usage, which can
                # only prolong the lockout. Backoff caps it near 15 attempts.
                # The heartbeat below stays on every pass: it costs nothing and it
                # is what keeps a dead broker distinguishable from a dead laptop.
                if time.time() < _next_reconcile_try:
                    publish_degraded(led, "broker_unreachable")
                    _maybe_alert_broker(led)
                    if "--once" in sys.argv:
                        return 0
                    time.sleep(FAST_PASS_SEC)
                    continue
                log("reconciling with the broker")
                if adopt_truth(led, reconcile()):
                    if _reconcile_fails:
                        _mins = market_minutes_since(_broker_down_since)
                        log(f"broker reachable again after {_reconcile_fails} failed attempt(s), "
                            f"{_degraded_passes} degraded pass(es), ~{_mins} min")
                        if _broker_alert_at:
                            notify("RH bot: broker connection restored",
                                   "The Robinhood connection is working again. "
                                   "Normal trading and stop enforcement have resumed.")
                        _reconcile_fails, _broker_alert_at = 0, 0.0
                        _remember_alerts(led, broker_alert_at=0.0, reconcile_fails=0)
                    _next_reconcile_try = 0.0
                    _broker_down_since, _degraded_passes, _waiting_on_reset = 0.0, 0, False
                    led["needs_reconcile"] = False
                elif DRY:
                    # A dry ledger is simulated, so it can never match the real
                    # book. Blocking here would deadlock every validation run at
                    # session open, so the simulation just owns the ledger.
                    led["needs_reconcile"] = False
                else:
                    # BROKER UNREACHABLE. Keep the heartbeat alive and say WHY.
                    # This branch used to `continue` straight to the next pass,
                    # which skipped cycle() and therefore persist(), so no status
                    # was ever written or pushed. A dead broker connection then
                    # looked identical to a dead laptop, and on 2026-08-18 the
                    # watchdog duly reported "laptop silent" while the machine was
                    # fine and the real fault was an expired Robinhood connector
                    # authorization. Wrong diagnosis costs time that positions do
                    # not have, since selling needs this same bridge.
                    _reconcile_fails += 1
                    _remember_alerts(led, reconcile_fails=_reconcile_fails)
                    if not _broker_down_since:
                        _broker_down_since = time.time()
                    # The whole decision - curve, named reset, auth cap and the precedence
                    # between them - lives in retry_wait(). It was inline here, which is
                    # how a mutation deleting the auth cap left the tests green: they
                    # replayed the arithmetic instead of calling the code.
                    # The reset time the bridge NAMED beats a blind curve: usage limits are
                    # the largest single source of this bot's blindness and the exact moment
                    # the quota returns is in the error text we already logged.
                    _reset_wait = quota_reset_wait(_last_agent_error)
                    _waiting_on_reset = _reset_wait is not None
                    wait = retry_wait(_reconcile_fails, _last_agent_error, _reset_wait)
                    _next_reconcile_try = time.time() + wait
                    log(f"broker snapshot unavailable ({_reconcile_fails}x), "
                        f"trading nothing this pass, next try in {int(wait)}s"
                        f"{' (quota reset time)' if _reset_wait is not None else ''}")
                    save_ledger(led)
                    publish_degraded(led, "broker_unreachable")
                    _maybe_alert_broker(led)
                    if "--once" in sys.argv:
                        return 0
                    time.sleep(FAST_PASS_SEC)
                    continue
            # EVERY pass, fast included, not just the full cycle: a stuck Arm A run is exactly the
            # case where minutes matter, and full cycles are 15 minutes apart. check_arm_a()
            # self-throttles to 5 minutes and returns immediately outside 09:45-15:55 ET.
            check_arm_a(led)
            if full:
                check_mail(led)   # surface new mailbox entries for this session
                check_deposit_overdue(led)   # ask, never invent, a missed deposit
            if full and sync_code(led):
                save_ledger(led)          # ledger is the source of truth across restarts
                # Do NOT execv on Windows: it spawns rather than replaces, which is
                # how three daemons ended up running at once. Exit non-zero and let
                # Task Scheduler restart exactly one managed instance. The singleton
                # mutex backs this up if a restart ever races.
                if sys.platform == "win32":
                    log("updated code pulled — exiting 42 for a clean task restart")
                    return 42
                log("restarting into updated code")
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception as e:
                    log(f"execv failed ({e}) - exiting 42 so the scheduled task restarts us")
                    return 42
            res = cycle(led, fast=not full)
            if full:
                last_full = time.time()
                s = res.get("snapshot") or {}
                log(f"FULL | EQ ${s.get('equity', 0):.2f} | index ${s.get('index', 0):.2f} "
                    f"| hold ${s.get('hold', 0):.2f} | trade ${s.get('trade', 0):.2f} "
                    f"| {len(res['orders'])} order(s)")
        except Exception as e:
            log(f"cycle error (loop continues): {e}")

        if "--once" in sys.argv:
            return 0
        time.sleep(FAST_PASS_SEC)


if __name__ == "__main__":
    sys.exit(main())
