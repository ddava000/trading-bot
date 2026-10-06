#!/usr/bin/env python3
"""ARM A WATCH: a dead-man's switch for Arm A, the Alpaca bot. Alerts when it stops running.

WHY IT EXISTS (2026-10-05). GitHub had an Actions incident and Arm A's 15:30 and 15:45 ET runs never got
a machine. The bot checks its stop-losses itself, in a 60 second loop inside each run (there are no stop
orders at the broker), so for about 28 minutes before the close nothing could have fired. Nothing noticed:
rh_watchdog watches the laptop, and status.json's next_expected_utc is published for a monitor that did
not exist. The only signal was GitHub's failed-run emails.

THREE SIGNALS, each measured on the real history before its threshold was set:
  queued      a run has waited N minutes for a machine (a healthy run starts within seconds). Catches an
              Actions incident about 6 minutes in. This is the one that would have caught 10-05.
  no_trigger  no run has been CREATED for N minutes (cron-job.org should create one every 15 minutes).
  stale       status.json has not been committed for N minutes. Normally every 15 (worst gap in 9 trading
              days: 24.5), so a crash loop or a wedged push shows here.
Each has two thresholds. The first emails and posts to Slack ("late"); the second is urgent and also uses
SMS/push when they are configured ("not running").

WHERE IT RUNS, and why two places. A watcher that lives on GitHub Actions shares fate with what it watches:
on 10-05 a watcher triggered by Arm A's own runs would have been blind, because the stuck runs never
completed. So the same check runs (1) as the ARM A WATCH workflow, best effort, and (2) on the Arm B
laptop, which does not depend on GitHub Actions at all (rh_daemon calls run_once() from its loop, see
the integration note in AGENT_MAIL). Each arm then watches the other.

Never places, moves or cancels a trade. Reads status.json, git history and the public Actions runs list.

Usage:
  python arm_a_watch.py                  one check, alert if a threshold was just crossed (stateless windows)
  python arm_a_watch.py --force          send a TEST alert by email and Slack (FORCE_ALERT=true does too)
  python arm_a_watch.py --force-urgent   the same, but also SMS and push, to prove those channels (FORCE_ALERT=urgent)
  python arm_a_watch.py --fetch --ref origin/main      what the laptop does: git fetch, then read origin/main
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# alpaca_bot reads its keys at import; this module never uses them (same pattern as rh_watchdog).
os.environ.setdefault("ALPACA_API_KEY", "unused-in-arm-a-watch")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-in-arm-a-watch")
import alpaca_bot as bot  # noqa: E402  (holiday table and the bot's own Eastern time zone)

STATUS_F = "status.json"
REPO = (os.environ.get("GITHUB_REPOSITORY") or "ddava000/trading-bot").strip()
WORKFLOW_FILE = "alpaca-bot.yml"

# Minutes. (first, urgent). Set from measurements, 2026-10-06:
#   queued:     a run starts within seconds when healthy (started == created in every observed run).
#   no_trigger: cron-job.org fires every 15 minutes with about 7 seconds of jitter.
#   stale:      status.json commits are 15 minutes apart, worst gap 24.5 in 237 market-hours gaps.
THRESHOLDS = {
    "queued": (6, 20),
    "no_trigger": (22, 50),
    "stale": (35, 75),
}
MAX_DELIVERY_TRIES = 3        # exact mode: retry a failed delivery on the next check, then stop (do not repost forever)
COMMIT_LAG_MIN = 11          # normal delay between the ts INSIDE status.json and its commit (p95 10.9)
OPEN_ET, CLOSE_ET = (9, 45), (15, 55)    # the bot's own gate (alpaca_bot.check_market)
QUEUED_STATES = ("queued", "pending", "waiting", "requested")


def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


# A threshold counts as crossed if it fell in the last WINDOW minutes. Wider than the spacing between
# checks so a late check cannot skip it; a rare duplicate beats a missed alert. The laptop passes
# `fired` instead and is exact (once per threshold per episode).
WINDOW_MIN = _env_int("WATCH_WINDOW_MIN", 20)


# ---------------------------------------------------------------------------------------------------
# Pure logic

def session_bounds(now_utc):
    """(open_utc, close_utc) if Arm A is expected to be running today, else None."""
    et = now_utc.astimezone(bot.ET_TZ)
    if et.weekday() >= 5 or et.strftime("%Y-%m-%d") in bot.MARKET_HOLIDAYS:
        return None
    o = et.replace(hour=OPEN_ET[0], minute=OPEN_ET[1], second=0, microsecond=0)
    c = et.replace(hour=CLOSE_ET[0], minute=CLOSE_ET[1], second=0, microsecond=0)
    return o.astimezone(timezone.utc), c.astimezone(timezone.utc)


def parse_status_ts(status):
    """The ts inside status.json as UTC, or None."""
    try:
        return datetime.strptime(status["ts"], "%Y-%m-%dT%H:%M ET").replace(tzinfo=bot.ET_TZ).astimezone(timezone.utc)
    except Exception:
        return None


def _mins(a, b):
    return (a - b).total_seconds() / 60.0


def measure(now_utc, open_utc, commit_utc=None, status=None, runs=None):
    """The three signals in minutes, or None for one that could not be measured. NEVER 0 for unknown:
    a check that cannot see must say so, not report calm.

    Every signal is clamped to the session: an overnight silence is not an outage, so at the open the
    clock starts at the open and not at yesterday's last commit.
    """
    out = {"queued": None, "no_trigger": None, "stale": None}
    fresh = commit_utc
    if fresh is None and status is not None:
        ts = parse_status_ts(status)
        if ts is not None:
            fresh = ts + timedelta(minutes=COMMIT_LAG_MIN)        # no git history: assume the normal lag
    if fresh is not None:
        out["stale"] = max(0.0, _mins(now_utc, max(fresh, open_utc)))
    if runs is not None:
        created = [r["created_at"] for r in runs if r.get("created_at")]
        out["no_trigger"] = max(0.0, _mins(now_utc, max(max(created) if created else open_utc, open_utc)))
        waits = [_mins(now_utc, r["created_at"]) for r in runs
                 if r.get("status") in QUEUED_STATES and r.get("created_at")]
        out["queued"] = max(waits) if waits else 0.0
    return out


def crossings(signals, fired=None, window=None):
    """Which thresholds need an alert NOW.

    fired=None: stateless. A threshold fires if it was crossed within the last `window` minutes.
    fired=dict: exact. Each (signal, threshold) fires once until the signal recovers below its first
    threshold, which clears it. The dict is updated in place.
    """
    window = WINDOW_MIN if window is None else window
    out = []
    for name, ths in THRESHOLDS.items():
        m = signals.get(name)
        if m is None:
            continue
        if fired is not None and m < ths[0]:
            fired.pop(name, None)
            for k in [k for k in fired.get("_failed", {}) if k[0] == name]:
                fired["_failed"].pop(k)
            continue
        for i, t in enumerate(ths):
            if m < t:
                continue
            if fired is None:
                if m - t >= window:
                    continue
            else:
                done = fired.setdefault(name, set())
                if t in done:
                    continue
                done.add(t)
            out.append({"signal": name, "threshold": t, "minutes": m, "urgent": i == len(ths) - 1})
    return out


def _active(fired):
    """True if exact mode has an unresolved episode (the retry counter alone is not one)."""
    return bool(fired) and any(not k.startswith("_") and v for k, v in fired.items())


def _hm(dt):
    return dt.astimezone(bot.ET_TZ).strftime("%H:%M ET")


def describe(c, now_utc, runs):
    m = int(c["minutes"])
    if c["signal"] == "queued":
        waiting = [r for r in (runs or []) if r.get("status") in QUEUED_STATES and r.get("created_at")]
        since = _hm(min(r["created_at"] for r in waiting)) if waiting else "?"
        return ("An Arm A run created at %s has waited %d minutes for GitHub to give it a machine; a healthy "
                "run starts within seconds. This is usually a GitHub Actions incident: "
                "https://www.githubstatus.com" % (since, m))
    if c["signal"] == "no_trigger":
        return ("No Arm A run has been triggered for %d minutes. cron-job.org should trigger one every 15. "
                "Check its job, and the Actions tab." % m)
    return ("Arm A's status.json has not been committed for %d minutes (normally every 15). A run may be "
            "crashing or stuck, or its push is failing." % m)


def build_alert(cs, now_utc, runs):
    """(subject, body, urgent) for the crossings in `cs`. Plain ASCII: Devon prints mail by subject."""
    urgent = any(c["urgent"] for c in cs)
    subject = "ARM A WATCH ALERT: Arm A is not running" if urgent else "ARM A WATCH: Arm A is late"
    lines = ["Arm A (the Alpaca bot) looks stuck as of %s." % _hm(now_utc), ""]
    lines += ["- " + describe(c, now_utc, runs) for c in cs]
    lines += ["",
              "Why it matters: Arm A checks its stop-losses itself, in a loop inside each run, with no stop",
              "orders at the broker. While runs are not happening, open positions are NOT being protected.",
              "A single late run is normal; this fires only past the normal range (see arm_a_watch.py).",
              "",
              "Nothing here places, moves or cancels a trade. If it persists: check githubstatus.com, the",
              "Actions tab, and the cron-job.org job. Arm B (Robinhood, index-only) is unaffected."]
    return subject, "\n".join(lines), urgent


# ---------------------------------------------------------------------------------------------------
# Inputs

def _git(*args, timeout=60):
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def git_commit_time(ref="HEAD", path=STATUS_F):
    """When status.json was last committed on `ref`, UTC, or None (no history in a shallow clone)."""
    try:
        r = _git("log", "-1", "--format=%cI", ref, "--", path)
        s = r.stdout.strip()
        return datetime.fromisoformat(s).astimezone(timezone.utc) if r.returncode == 0 and s else None
    except Exception as e:
        print("  [arm a watch: git log failed: %s]" % e)
        return None


def read_status(ref=None, path=STATUS_F):
    """status.json as a dict from the working tree (ref=None) or from git `ref`, or None."""
    try:
        if ref:
            r = _git("show", "%s:%s" % (ref, path))
            return json.loads(r.stdout) if r.returncode == 0 else None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("  [arm a watch: could not read %s: %s]" % (path, e))
        return None


def fetch_runs(repo=REPO, workflow=WORKFLOW_FILE, token=None, per_page=8):
    """Recent Arm A runs from the Actions API as [{status, conclusion, created_at}], or None on failure.
    The repo is public, so this works without a token; a token only raises the rate limit."""
    url = "https://api.github.com/repos/%s/actions/workflows/%s/runs?per_page=%d" % (repo, workflow, per_page)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "arm-a-watch"}
    token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [{"status": x.get("status"), "conclusion": x.get("conclusion"),
                 "created_at": datetime.strptime(x["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)}
                for x in d.get("workflow_runs", [])]
    except Exception as e:
        print("  [arm a watch: could not read the Actions runs list: %s]" % e)
        return None


def default_deliver(subject, body, urgent):
    """Email + Slack always; SMS and push only when urgent (rh_watchdog.alert owns the secrets rules)."""
    import rh_watchdog
    return rh_watchdog.alert(body, urgent=urgent, subject=subject, title=subject)


# ---------------------------------------------------------------------------------------------------
# One check

def run_once(now=None, fetch=False, ref=None, fired=None, deliver=default_deliver, window=None,
             get_commit_time=git_commit_time, get_status=read_status, get_runs=fetch_runs):
    """One check. Returns (exit_code, one_line_summary, [alerts delivered]).

    exit 0: fine, or an alert was due and delivered.  exit 1: an alert was due and NOT delivered, or
    nothing could be measured at all. Either makes a GitHub run red, which emails the owner: a watcher
    that fails quietly is the failure this exists to remove.
    fired: pass a dict to alert exactly once per threshold and to send a recovery notice (the daemon).
    """
    now = now or datetime.now(timezone.utc)
    bounds = session_bounds(now)
    if bounds is None or not (bounds[0] <= now <= bounds[1]):
        return 0, "ARM A WATCH: market closed, nothing expected", []
    open_utc = bounds[0]
    if fetch:
        try:
            _git("fetch", "-q", "origin", "main", timeout=60)
        except Exception as e:
            print("  [arm a watch: git fetch failed: %s]" % e)
    commit = get_commit_time(ref or "HEAD")
    status = get_status(ref)
    runs = get_runs()
    sig = measure(now, open_utc, commit, status, runs)
    if all(v is None for v in sig.values()):
        return 1, "ARM A WATCH: could not measure anything (no git history, no status.json, no runs list)", []

    def f(k):
        return "unknown" if sig[k] is None else "%.0fm" % sig[k]
    line = "ARM A WATCH: status commit age %s, last run created %s ago, longest wait for a machine %s" % (
        f("stale"), f("no_trigger"), f("queued"))
    had_active = _active(fired)
    cs = crossings(sig, fired, window)
    sent = []
    if cs:
        subject, body, urgent = build_alert(cs, now, runs)
        ok = deliver(subject, body, urgent)
        print("ALERT:", subject)
        if not ok:
            if fired is not None:                       # exact mode: do not let a lost alert count as sent
                failed = fired.setdefault("_failed", {})
                for c in cs:
                    k = (c["signal"], c["threshold"])
                    failed[k] = failed.get(k, 0) + 1
                    if failed[k] < MAX_DELIVERY_TRIES:
                        fired.get(c["signal"], set()).discard(c["threshold"])
            return 1, line + " | ALERT NOT DELIVERED: " + subject, []
        sent.append(subject)
        line += " | ALERTED: " + subject
    elif fired is not None and had_active and not _active(fired):
        # Exact mode only (a stateless run cannot know there was an episode): say it is over.
        subject = "ARM A WATCH: Arm A is running again"
        body = ("Arm A is back inside its normal range as of %s: %s." % (_hm(now), line[len("ARM A WATCH: "):]))
        if deliver(subject, body, False):
            sent.append(subject)
            line += " | RECOVERED"
    return 0, line, sent


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    force = (os.environ.get("FORCE_ALERT") or "").strip().lower()
    urgent_test = "--force-urgent" in argv or force == "urgent"
    if urgent_test or "--force" in argv or force in ("true", "yes"):
        ok = default_deliver("ARM A WATCH: TEST alert",
                             "TEST alert from ARM A WATCH. This is not a real outage and nothing is wrong "
                             "with Arm A. " + ("It was sent as URGENT, so SMS and push were used too."
                                               if urgent_test else "Email and Slack only."), urgent_test)
        print("test alert", "delivered" if ok else "NOT DELIVERED")
        return 0 if ok else 1
    ref = argv[argv.index("--ref") + 1] if "--ref" in argv else None
    rc, line, _ = run_once(fetch="--fetch" in argv, ref=ref)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
