"""Daily digest of both bots, sent by GitHub after the close. No Claude session, no Claude quota, nothing that can freeze.

WHY (Devon 2026-10-17): BOT DAILY CHECK and LAPTOP BOT DAILY CHECK were two scheduled Claude sessions that read mail, looked at the same
CI / ARM A WATCH / watchdog results GitHub already produces, and posted "all green". Both failed silently: the cloud one froze on 2026-10-14
(a command 6 seconds in) and skipped two days unnoticed, the laptop one was killed by the shared session limit the same afternoon. Devon
retired both and asked for this instead: a plain script on a GitHub runner that cannot hang on a prompt and has no session limit. The alerts
that actually page him (CI, ARM A WATCH, the laptop watchdog, the daemon's own alerts, the Sunday audit) are unchanged.

WHEN IT SENDS. Once per trading day, after the bot's own close plus 10 minutes (session_bounds, so a 1 pm early close sends at 13:05).
  * workflow_run on "Alpaca Trading Bot" (fires every 15 minutes, reliably): sends iff the triggering run is the FIRST Arm A run to finish
    after that threshold. No stored state: later runs see an earlier one and stay quiet.
  * schedule (best effort; GitHub throttles crons) and manual dispatch: send iff NO Arm A run has finished after the threshold, i.e. the
    workflow_run path had nothing to fire on. dispatch with force=yes always sends (a test).
It never says calm about what it could not measure: an unreadable source is a flag, not a pass.

Read-only. Delivery is rh_watchdog.alert (email + Slack), which owns the secrets rules.

Run:  python daily_digest.py            (GITHUB_EVENT_NAME / TRIGGER_RUN_ID / FORCE_DIGEST come from the workflow)
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("ALPACA_API_KEY", "unused-in-daily-digest")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-in-daily-digest")

import alpaca_bot as bot  # noqa: E402
import arm_a_downtime as A  # noqa: E402
import arm_b_downtime as B  # noqa: E402

REPO = "ddava000/trading-bot"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
DIGEST_DELAY_MIN = 10          # after the bot's own close
BACKUP_WAIT_MIN = 60           # the schedule path waits this long past the threshold before deciding the workflow_run path missed
ARM_A_RUN_FLOOR = 0.75         # fewer finished Arm A runs than this share of the session's cadence is a flag
ARM_A_UNPLANNED_FLAG_MIN = 15  # window minutes of unplanned Arm A gap in one day
ARM_B_BLIND_FLAG_MIN = 15      # headline blind market-minutes in one day
BAD = ("failure", "cancelled", "timed_out", "startup_failure", "action_required")


# ---------------------------------------------------------------------------------------------------
# Pure logic

def threshold(day):
    """Tz-aware ET moment the digest becomes due on `day`, or None when it is not a trading day."""
    b = bot.session_bounds(day)
    return None if b is None else b[1] + timedelta(minutes=DIGEST_DELAY_MIN)


def should_send(event, now, runs, trigger_run_id=None, force=False):
    """(bool, why). `now` is tz-aware; `runs` is today's finished Arm A runs as [{id, updated_at (aware)}]."""
    th = threshold(now.astimezone(ET).date())
    if force:
        return True, "forced"
    if th is None:
        return False, "not a trading day"
    if now < th:
        return False, "the session is not over"
    after = [r for r in runs if r["updated_at"] >= th]
    if event == "workflow_run":
        if trigger_run_id is None:
            return False, "workflow_run without a trigger id"
        ids = {str(r["id"]) for r in after}
        if str(trigger_run_id) not in ids:
            known = [r for r in runs if str(r["id"]) == str(trigger_run_id)]
            if known:                                                       # it finished BEFORE the threshold
                return False, "this run finished before the close"
            after = after + [{"id": trigger_run_id, "updated_at": now}]     # the API can lag the event that announced the run
        first = sorted(after, key=lambda r: r["updated_at"])[0]
        if str(first["id"]) == str(trigger_run_id):
            return True, "first Arm A run to finish after the close"
        return False, "an earlier run after the close already sent it"
    if now < th + timedelta(minutes=BACKUP_WAIT_MIN):
        return False, "too early for the backup path"
    if after:
        return False, "the workflow_run path already fired"
    return True, "backup: no Arm A run finished after the close"


def _pct(x):
    return "n/a" if x is None else "%+.2f%%" % x


def _usd(x):
    return "n/a" if x is None else ("-$%.2f" % abs(x) if x < 0 else "+$%.2f" % x)


def build_report(f):
    """(verdict, flags, subject, body) from the facts dict. Pure. Every unknown becomes a flag, never a pass."""
    flags = []
    L = []
    d = f["date"]
    # ---- every workflow today
    runs = f.get("runs")
    if runs is None:
        flags.append("COULD NOT READ today's workflow runs from the Actions API")
    else:
        for name, conclusion, when in runs["bad"]:
            flags.append("RUN %s: %s at %s ET" % (conclusion.upper(), name, when))
    ci = f.get("ci")
    if ci is None:
        flags.append("COULD NOT READ the latest CI result")
    elif ci != "success":
        flags.append("CI latest run is %s" % ci)

    # ---- Arm A
    a = f.get("status")
    L.append("ARM A (Alpaca, cloud)")
    if a is None:
        flags.append("COULD NOT READ status.json")
        L.append("  status.json unreadable")
    else:
        L.append("  equity $%.2f, day %s, vs baseline %s, halted %s, VIX live %s" % (
            a.get("equity", 0), _usd(a.get("day_pnl")), _pct(a.get("vs_baseline_pct")), a.get("halted"), a.get("vix_live")))
        if a.get("halted"):
            flags.append("ARM A IS HALTED")
        if a.get("vix_live") is False:
            flags.append("Arm A VIX feed is not live (fallback value in use)")
    td = f.get("trading_day", True)
    done, expect = f.get("arm_a_finished"), f.get("arm_a_expected")
    if not td:
        L.append("  not a trading day: no run cadence expected")
    elif done is None or expect is None:
        flags.append("COULD NOT COUNT today's Arm A runs")
    else:
        L.append("  runs finished today: %d (about %d expected for this session)" % (done, expect))
        if done < ARM_A_RUN_FLOOR * expect:
            flags.append("ARM A RAN %d times, under %d%% of the %d expected" % (done, int(ARM_A_RUN_FLOOR * 100), expect))
    ua = f.get("arm_a_unplanned")
    if not td:
        pass
    elif ua is None:
        flags.append("COULD NOT MEASURE Arm A's unplanned gaps")
    else:
        L.append("  unplanned unwatched minutes today: %.0f" % ua)
        if ua >= ARM_A_UNPLANNED_FLAG_MIN:
            flags.append("Arm A was unwatched for %.0f unplanned minutes" % ua)

    # ---- Arm B
    b = f.get("rh")
    L.append("ARM B (Robinhood, laptop)")
    if b is None:
        flags.append("COULD NOT READ rh_status.json")
        L.append("  rh_status.json unreadable")
    else:
        L.append("  equity $%.2f, last heartbeat %s ET, degraded %s" % (b.get("equity", 0), b.get("ts"), b.get("degraded")))
        if b.get("broker_total_value_held_back") is not None:
            flags.append("Arm B equity guard held back a reading (%s); the published figure is unconfirmed" % b["broker_total_value_held_back"])
    m = f.get("arm_b")
    if not td:
        L.append("  not a trading day: no blind minutes to measure")
    elif m is None:
        flags.append("COULD NOT MEASURE Arm B's blind minutes")
    else:
        blind = m["degraded"] + m["no_push"] - m["edge"]
        L.append("  blind market-minutes today: %.0f (degraded %.0f, no-push %.0f, close/open edge %.0f not counted)" % (
            blind, m["degraded"], m["no_push"], m["edge"]))
        if blind >= ARM_B_BLIND_FLAG_MIN:
            flags.append("Arm B was blind %.0f market-minutes today" % blind)

    # ---- workflows
    if runs is not None:
        L.append("GITHUB")
        for name in sorted(runs["by_name"]):
            L.append("  %-36s %3d run(s)%s" % (name, runs["by_name"][name]["total"],
                                                "" if not runs["by_name"][name]["bad"] else ", %d not green" % len(runs["by_name"][name]["bad"])))
        L.append("  CI latest: %s" % ci)

    # ---- realized and mail
    L.append("REALIZED (shared reader, not tax advice)")
    rl = f.get("realized_lines")
    if rl is None:
        flags.append("COULD NOT READ the realized gain/loss lines")
        L.append("  unreadable")
    else:
        L.extend("  " + x for x in rl)
    mail = f.get("mail_today")
    if mail is not None:
        L.append("AGENT_MAIL entries today: %d" % mail)

    verdict = "OK" if not flags else "ATTENTION (%d)" % len(flags)
    head = ["DAILY DIGEST %s, %s: %s" % (d, f["weekday"], verdict)]
    if flags:
        head.append("")
        head.append("WHAT NEEDS A LOOK")
        head.extend("  - " + x for x in flags)
    head.append("")
    body = "\n".join(head + L) + "\n\nSent by GitHub (daily_digest.py), not by a Claude session. The alerts that page you are separate and unchanged."
    subject = "Trading bots daily digest %s: %s" % (d, verdict)
    return verdict, flags, subject, body


# ---------------------------------------------------------------------------------------------------
# Gathering (every source is injectable so the pure parts can be tested)

def _api(path, token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "daily-digest"}
    token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (REPO, path), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_runs_since(day_iso):
    """Every workflow run created on or after `day_iso` (YYYY-MM-DD, UTC), across the repo, or None on failure."""
    out = []
    try:
        for page in range(1, 6):
            d = _api("actions/runs?created=%%3E%%3D%s&per_page=100&page=%d" % (day_iso, page))
            batch = d.get("workflow_runs", [])
            out += batch
            if len(batch) < 100:
                break
        return out
    except Exception as e:
        print("  [daily digest: could not read the runs list: %s]" % e)
        return None


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def summarize_runs(raw, day):
    """Today's runs (by ET date of creation) grouped by workflow name, with the not-green ones listed."""
    by, bad = {}, []
    for r in raw:
        created = _utc(r["created_at"]).astimezone(ET)
        if created.date() != day or r.get("status") != "completed":
            continue
        e = by.setdefault(r["name"], {"total": 0, "bad": []})
        e["total"] += 1
        if r.get("conclusion") in BAD:
            e["bad"].append(r["conclusion"])
            bad.append((r["name"], r["conclusion"], created.strftime("%H:%M")))
    return {"by_name": by, "bad": bad}


def arm_a_finished(raw, day):
    """[{id, updated_at}] for today's finished Arm A runs (any conclusion)."""
    out = []
    for r in raw:
        if r.get("name") != "Alpaca Trading Bot" or r.get("status") != "completed":
            continue
        if _utc(r["created_at"]).astimezone(ET).date() == day:
            out.append({"id": r["id"], "updated_at": _utc(r["updated_at"])})
    return out


def gather(now, raw, repo_root="."):
    day = now.astimezone(ET).date()
    f = {"date": day.isoformat(), "weekday": day.strftime("%a"), "trading_day": bot.session_bounds(day) is not None}
    if raw is None:
        f["runs"] = f["ci"] = f["arm_a_finished"] = f["arm_a_expected"] = f["arm_a_unplanned"] = None
    else:
        f["runs"] = summarize_runs(raw, day)
        ci = [r for r in raw if r.get("name") == "ci" and r.get("status") == "completed"]
        f["ci"] = sorted(ci, key=lambda r: r["created_at"])[-1].get("conclusion") if ci else None
        bounds = bot.session_bounds(day)
        fin = arm_a_finished(raw, day)
        # only runs that finished inside the session count towards the cadence (the pre-open ones exit in seconds)
        inside = [r for r in fin if bounds and bounds[0] <= r["updated_at"].astimezone(ET) <= bounds[1] + timedelta(minutes=15)]
        f["arm_a_finished"] = len(inside)
        f["arm_a_expected"] = int(((bounds[1] - bounds[0]).total_seconds() / 60.0) / 15.0) if bounds else None
        try:
            ivs = A.intervals([r for r in raw if r.get("name") == "Alpaca Trading Bot"])
            if bounds and ivs:
                res = A.measure(ivs, bounds[0].replace(tzinfo=None), bounds[1].replace(tzinfo=None), B.holidays())
                f["arm_a_unplanned"] = res["unplanned"]
            else:
                f["arm_a_unplanned"] = None
        except Exception as e:
            print("  [daily digest: arm a gap measure failed: %s]" % e)
            f["arm_a_unplanned"] = None

    def load(name):
        try:
            with open(os.path.join(repo_root, name), encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            print("  [daily digest: could not read %s: %s]" % (name, e))
            return None

    f["status"], f["rh"] = load("status.json"), load("rh_status.json")
    try:
        since = (day - timedelta(days=3)).isoformat()
        snaps = B.parse_subjects(B.git_subjects(since=since))
        bounds = bot.session_bounds(day)
        f["arm_b"] = B.measure(snaps, bounds[0].replace(tzinfo=None), bounds[1].replace(tzinfo=None), B.holidays()) if snaps and bounds else None
    except Exception as e:
        print("  [daily digest: arm b measure failed: %s]" % e)
        f["arm_b"] = None
    try:
        import realized
        f["realized_lines"] = list(realized.repo_report_lines())
    except Exception as e:
        print("  [daily digest: realized reader failed: %s]" % e)
        f["realized_lines"] = None
    try:
        import mail_check
        with open(os.path.join(repo_root, "AGENT_MAIL.md"), encoding="utf-8") as fh:
            es = mail_check.entries(fh.read())
        f["mail_today"] = sum(1 for e in es if str(e.get("ts", "")).startswith(day.isoformat()))
    except Exception:
        f["mail_today"] = None
    return f


# ---------------------------------------------------------------------------------------------------

def default_deliver(subject, body):
    import rh_watchdog
    return rh_watchdog.alert(body, urgent=False, subject=subject, title=subject)


def run_once(now=None, event=None, trigger_run_id=None, force=False, raw=False, deliver=default_deliver, repo_root="."):
    """(exit_code, one_line_summary). raw=False means fetch from the API; tests pass a list (or None to simulate a failed read)."""
    now = now or datetime.now(UTC)
    event = event if event is not None else os.environ.get("GITHUB_EVENT_NAME", "")
    trigger_run_id = trigger_run_id if trigger_run_id is not None else (os.environ.get("TRIGGER_RUN_ID") or None)
    day = now.astimezone(ET).date()
    if raw is False:
        raw = fetch_runs_since((now - timedelta(days=1)).strftime("%Y-%m-%d"))
    send, why = should_send(event, now, arm_a_finished(raw, day) if raw else [], trigger_run_id, force)
    if not send:
        return 0, "DAILY DIGEST: not sending (%s)" % why
    facts = gather(now, raw, repo_root)
    verdict, flags, subject, body = build_report(facts)
    print(body)
    sent = deliver(subject, body)
    if not sent:
        return 1, "DAILY DIGEST: built (%s) but NOT delivered to any channel" % verdict
    return 0, "DAILY DIGEST: sent (%s)%s" % (verdict, " via " + ",".join(sent) if isinstance(sent, (list, tuple)) else "")


def main():
    force = (os.environ.get("FORCE_DIGEST") or "").strip().lower() in ("yes", "true", "1")
    rc, line = run_once(force=force)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
