"""Daily digest of both bots, sent by GitHub after the close. No Claude session, no Claude quota, nothing that can freeze.

WHY (Devon 2026-10-17): BOT DAILY CHECK and LAPTOP BOT DAILY CHECK were two scheduled Claude sessions that read mail, looked at the same
CI / ARM A WATCH / watchdog results GitHub already produces, and posted "all green". Both failed silently: the cloud one froze on 2026-10-14
(a command 6 seconds in) and skipped two days unnoticed, the laptop one was killed by the shared session limit the same afternoon. Devon
retired both and asked for this instead: a plain script on a GitHub runner that cannot hang on a prompt and has no session limit. The alerts
that actually page him (CI, ARM A WATCH, the laptop watchdog, the daemon's own alerts, the Sunday audit) are unchanged.

WHEN IT SENDS. Once per trading day, from the bot's own close (session_bounds, so 15:55 ET, or 12:55 on a 1 pm early close). ANY trigger
may fire it; the rule is the same for all of them: due, and not already sent today. "Sent" is read from GitHub itself, not stored anywhere: a
run of this workflow whose step "Send the daily digest" ended in success. A failed send therefore retries on the next trigger.
  * workflow_run on "Alpaca Trading Bot": its last run of the day starts at 15:45 and finishes about 15:55:45, just after the close, so it is
    normally the trigger. (A first version waited until 16:05 and so could never fire: the last Arm A run was already over. It went out only
    through the best-effort schedule, three hours late, on its first real day, 2026-10-19.)
  * workflow_run on "Research Brief (morning + intraday)": its evening run is a second chance after the close.
  * schedule (best effort; GitHub throttles crons, so there are two) and manual dispatch. dispatch with force=yes always sends (a test).
The workflow serialises runs (one concurrency group, no cancel-in-progress), so two triggers in the same minute cannot both send.
It never says calm about what it could not measure: an unreadable source is a flag, not a pass.

Read-only. Delivery is rh_watchdog.alert (email + Slack), which owns the secrets rules.

Run:  python daily_digest.py --decide     writes due=true|false to $GITHUB_OUTPUT (the workflow's first step)
      python daily_digest.py --send       builds and delivers (the second step, only when due)
      python daily_digest.py              both, for a local look (FORCE_DIGEST=yes to ignore the clock)
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
DIGEST_DELAY_MIN = 0           # due at the bot's own close: the last Arm A run finishes about 45 seconds after it
SEND_STEP = "Send the daily digest"      # the workflow step whose success means "today's digest went out"
WORKFLOW_FILE = "daily-digest.yml"
OWN_NAME = "DAILY DIGEST"
ARM_A_RUN_FLOOR = 0.75         # fewer finished Arm A runs than this share of the session's cadence is a flag
ARM_A_UNPLANNED_FLAG_MIN = 15  # window minutes of unplanned Arm A gap in one day
SLOW_START_MIN = 4             # a run whose job did not exist until this many minutes after the run was created: GitHub was slow to start it
ARM_B_BLIND_FLAG_MIN = 15      # headline blind market-minutes in one day
# What the laptop's machine block (rh_status.json["machine"], published by rh_daemon) should say. HMB off is the live crash experiment (set
# 2026-10-08, 0 = disabled; an absent value means HMB is back at its default ON). Fast Startup is ON because Devon chose to leave it on
# (2026-10-08); if he turns it off, change this and the digest will start flagging a turn-on instead.
EXPECTED_MACHINE = {"hmb_policy": 0, "fast_startup": 1}
BAD = ("failure", "timed_out", "startup_failure", "action_required")
# A cancelled run is reported but is not a flag by itself: GitHub cancels runs during its own Actions incidents (a laptop watchdog run
# was cancelled that way on 2026-10-19), and a dropped Arm A run shows up in the cadence and unplanned-gap checks anyway.


# ---------------------------------------------------------------------------------------------------
# Pure logic

def threshold(day):
    """Tz-aware ET moment the digest becomes due on `day`, or None when it is not a trading day."""
    b = bot.session_bounds(day)
    return None if b is None else b[1] + timedelta(minutes=DIGEST_DELAY_MIN)


def should_send(now, sent_today, force=False):
    """(bool, why). `now` is tz-aware. The same rule for every trigger: due, and not already sent today."""
    if force:
        return True, "forced"
    th = threshold(now.astimezone(ET).date())
    if th is None:
        return False, "not a trading day"
    if now < th:
        return False, "the session is not over"
    if sent_today:
        return False, "today's digest was already sent"
    return True, "due and not yet sent today"


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
            if name == "ci" and f.get("ci") == "success":
                continue            # CI fails per push and is fixed by the next one; the CI line below says the latest is green
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
        slow = f.get("arm_a_slow_starts") or []
        js = f.get("arm_a_job_stats")
        if slow:
            L.append("  runs GitHub was slow to start (%d+ minutes before the job existed): %d, worst %.0f min" % (SLOW_START_MIN, len(slow), slow[-1][1]))
        if js is None:
            flags.append("COULD NOT READ today's Arm A jobs: the gap figure counts time spent waiting to start as watched and can hide a delayed run")
        elif js.get("fallback") and not js.get("job"):
            flags.append("COULD NOT READ any of today's Arm A jobs (%d run(s)): the gap figure counts time spent waiting to start as watched" % js["fallback"])
        elif js.get("fallback"):
            L.append("  (%d of %d run(s) measured by run span because their job could not be read)" % (js["fallback"], js["fallback"] + js.get("job", 0)))
        if ua >= ARM_A_UNPLANNED_FLAG_MIN:
            flags.append("Arm A was unwatched for %.0f unplanned minutes%s" % (
                ua, (" (GitHub started %d run(s) %d or more minutes late)" % (len(slow), SLOW_START_MIN)) if slow else ""))

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
    mc = (b or {}).get("machine") if b is not None else None
    if b is not None:
        if not mc:
            flags.append("COULD NOT READ the laptop's machine facts (rh_status.json has no machine block)")
            L.append("  laptop machine facts: unreadable")
        else:
            g = mc.get("guards") or {}
            ka = mc.get("keep_awake")
            L.append("  laptop: keep-awake %s, HMB policy %s (want %s), Fast Startup %s (want %s)" % (
                "held" if ka else ("NOT HELD" if ka is not None else "unknown"), g.get("hmb_policy"), EXPECTED_MACHINE["hmb_policy"],
                g.get("fast_startup"), EXPECTED_MACHINE["fast_startup"]))
            if ka is not True:
                flags.append("Arm B keep-awake is %s: the laptop can enter Modern Standby again" % ("NOT HELD" if ka is False else "unknown"))
            for k, want in EXPECTED_MACHINE.items():
                if g.get(k) != want:
                    flags.append("Laptop setting %s is %r, expected %r (None means not set; for HMB that means ON again, which voids the crash experiment)"
                                 % (k, g.get(k), want))
            u = mc.get("last_unclean_shutdown")
            if u is None:
                flags.append("COULD NOT READ the laptop's last unclean shutdown from the Windows event log")
            else:
                when = None
                try:
                    when = datetime.strptime(u["logged_at"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).astimezone(ET)
                except Exception:
                    pass
                L.append("  last unclean shutdown logged %s ET, bugcheck %s, connected standby %s" % (
                    when.strftime("%Y-%m-%d %H:%M") if when else u.get("logged_at"), u.get("bugcheck_hex"), u.get("connected_standby")))
                if when is not None and when.date().isoformat() == d:
                    flags.append("The laptop logged an UNCLEAN SHUTDOWN today at %s ET (bugcheck %s, connected standby %s)" % (
                        when.strftime("%H:%M"), u.get("bugcheck_hex"), u.get("connected_standby")))
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
            e = runs["by_name"][name]
            if name == OWN_NAME and not e["bad"]:
                continue                                    # its own invocations are noise (about 28 a day, one of which sends)
            extra = ""
            if e["bad"]:
                extra += ", %d not green" % len(e["bad"])
            if e.get("cancelled"):
                extra += ", %d cancelled" % e["cancelled"]
            L.append("  %-36s %3d run(s)%s" % (name, e["total"], extra))
        L.append("  CI latest: %s%s" % (ci, " (%s ET)" % f["ci_when"] if f.get("ci_when") else ""))
    inc = f.get("incidents")
    if inc:
        for start, end, impact, name in inc:
            L.append("  GitHub status: %s incident %s to %s ET: %s (cancelled or late runs today may be this, not the bots)" % (impact, start, end, name))
    elif inc is not None:
        L.append("  GitHub status: no major incident today")

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


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


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


def fetch_job_spans(raw, day, fetch=None):
    """{run id: {start, end, wait_min}} for today's successful Arm A runs, from each run's JOB. Runs whose job cannot be read are absent.

    The run's own created-to-updated span counts the time GitHub kept it waiting as watched, which made 2026-10-26 look clean while
    no job was executing for 16 and 18 minutes (see arm_a_downtime.py). Returns {} when there is nothing to read, None if `raw` is None.
    """
    if raw is None:
        return None
    todays = [r for r in raw if r.get("name") == "Alpaca Trading Bot" and r.get("created_at")
              and _utc(r["created_at"]).astimezone(ET).date() == day]
    return A.job_spans(todays, fetch=fetch)


def fetch_ci(fetch=None):
    """{conclusion, when} for the latest COMPLETED ci run, whatever day it was (CI runs only on pushes), or None if unreadable."""
    try:
        rs = (fetch or _api)("actions/workflows/ci.yml/runs?per_page=5&status=completed").get("workflow_runs", [])
        if not rs:
            return None
        return {"conclusion": rs[0].get("conclusion"), "when": _utc(rs[0]["created_at"]).astimezone(ET).strftime("%m-%d %H:%M")}
    except Exception as e:
        print("  [daily digest: could not read the CI result: %s]" % e)
        return None


def fetch_incidents(day, fetch=None):
    """GitHub's own status incidents that started on `day` (ET) as [(start, end, impact, name)], [] for none, None if unreadable."""
    try:
        if fetch is None:
            with urllib.request.urlopen("https://www.githubstatus.com/api/v2/incidents.json", timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            data = fetch()
        out = []
        for i in data.get("incidents", []):
            start = datetime.fromisoformat(i["created_at"].replace("Z", "+00:00")).astimezone(ET)
            if start.date() != day or i.get("impact") not in ("major", "critical"):
                continue
            end = i.get("resolved_at")
            end = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M") if end else "ongoing"
            out.append((start.strftime("%H:%M"), end, i["impact"], i.get("name", "")))
        return sorted(out)
    except Exception as e:
        print("  [daily digest: could not read GitHub's status page: %s]" % e)
        return None


def digest_already_sent(day, fetch=None):
    """True iff a run of THIS workflow created on `day` at or after the due time has a successful SEND_STEP. Read from GitHub, not stored.
    False when unsure: a duplicate digest beats a missing one."""
    th = threshold(day)
    if th is None:
        return False
    fetch = fetch or _api
    try:
        since = (th.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        runs = fetch("actions/workflows/%s/runs?per_page=100&created=%%3E%%3D%s" % (WORKFLOW_FILE, since)).get("workflow_runs", [])
        for r in runs:                                      # newest first
            if _utc(r["created_at"]) < th.astimezone(UTC):
                break
            if r.get("status") != "completed":
                continue                                    # includes the run that is asking
            for j in fetch("actions/runs/%s/jobs" % r["id"]).get("jobs", []):
                for s in j.get("steps", []):
                    if s.get("name") == SEND_STEP and s.get("conclusion") == "success":
                        return True
        return False
    except Exception as e:
        print("  [daily digest: could not tell whether today's digest was sent (%s); treating it as not sent]" % e)
        return False


def summarize_runs(raw, day):
    """Today's runs (by ET date of creation) grouped by workflow name; failures listed, cancellations only counted."""
    by, bad = {}, []
    for r in raw:
        created = _utc(r["created_at"]).astimezone(ET)
        if created.date() != day or r.get("status") != "completed":
            continue
        e = by.setdefault(r["name"], {"total": 0, "bad": [], "cancelled": 0})
        e["total"] += 1
        if r.get("conclusion") in BAD:
            e["bad"].append(r["conclusion"])
            bad.append((r["name"], r["conclusion"], created.strftime("%H:%M")))
        elif r.get("conclusion") == "cancelled":
            e["cancelled"] += 1
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


def gather(now, raw, repo_root=".", ci_info=None, incidents=None, job_spans=None):
    day = now.astimezone(ET).date()
    bounds = bot.session_bounds(day)
    f = {"date": day.isoformat(), "weekday": day.strftime("%a"), "trading_day": bounds is not None,
         "ci": (ci_info or {}).get("conclusion"), "ci_when": (ci_info or {}).get("when"), "incidents": incidents}
    if raw is None:
        f["runs"] = f["arm_a_finished"] = f["arm_a_expected"] = f["arm_a_unplanned"] = None
    else:
        f["runs"] = summarize_runs(raw, day)
        fin = arm_a_finished(raw, day)
        # only runs that finished inside the session count towards the cadence (the pre-open ones exit in seconds)
        inside = [r for r in fin if bounds and bounds[0] <= r["updated_at"].astimezone(ET) <= bounds[1] + timedelta(minutes=15)]
        f["arm_a_finished"] = len(inside)
        f["arm_a_expected"] = int(((bounds[1] - bounds[0]).total_seconds() / 60.0) / 15.0) if bounds else None
        try:
            stats = {}
            arm_a_runs = [r for r in raw if r.get("name") == "Alpaca Trading Bot"]
            ivs = A.intervals(arm_a_runs, job_spans, stats)
            f["arm_a_job_stats"] = stats if job_spans is not None else None
            f["arm_a_slow_starts"] = sorted(
                ((rid, s["wait_min"]) for rid, s in (job_spans or {}).items()
                 if s["wait_min"] >= SLOW_START_MIN and bounds and bounds[0] <= s["start"].replace(tzinfo=ET) <= bounds[1]),
                key=lambda x: x[1]) if job_spans else []
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


def decide(now=None, force=False, sent_today=None):
    """(bool, why): is a digest due right now? sent_today=None reads GitHub; tests pass a bool."""
    now = now or datetime.now(UTC)
    if sent_today is None:
        sent_today = False if force else digest_already_sent(now.astimezone(ET).date())
    return should_send(now, sent_today, force)


def send(now=None, raw=False, deliver=default_deliver, repo_root=".", ci_info=False, incidents=False, job_spans=False):
    """Build and deliver. (exit_code, one_line_summary). raw/ci_info/incidents default to fetching; tests inject (None = a failed read)."""
    now = now or datetime.now(UTC)
    day = now.astimezone(ET).date()
    if raw is False:
        raw = fetch_runs_since((now - timedelta(days=1)).strftime("%Y-%m-%d"))
    if ci_info is False:
        ci_info = fetch_ci()
    if incidents is False:
        incidents = fetch_incidents(day)
    if job_spans is False:
        job_spans = fetch_job_spans(raw, day)
    facts = gather(now, raw, repo_root, ci_info, incidents, job_spans)
    verdict, flags, subject, body = build_report(facts)
    print(body)
    sent = deliver(subject, body)
    if not sent:
        return 1, "DAILY DIGEST: built (%s) but NOT delivered to any channel" % verdict
    return 0, "DAILY DIGEST: sent (%s)%s" % (verdict, " via " + ",".join(sent) if isinstance(sent, (list, tuple)) else "")


def run_once(now=None, force=False, sent_today=None, **kw):
    """decide, then send if due. (exit_code, one_line_summary)."""
    ok, why = decide(now, force, sent_today)
    if not ok:
        return 0, "DAILY DIGEST: not sending (%s)" % why
    return send(now, **kw)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    force = (os.environ.get("FORCE_DIGEST") or "").strip().lower() in ("yes", "true", "1")
    if "--decide" in argv:
        ok, why = decide(force=force)
        print("DAILY DIGEST: %s (%s)" % ("DUE" if ok else "not due", why))
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as fh:
                fh.write("due=%s\n" % ("true" if ok else "false"))
        return 0
    if "--send" in argv:
        rc, line = send()
    else:
        rc, line = run_once(force=force)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
