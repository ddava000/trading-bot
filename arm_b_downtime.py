"""Arm B (the Robinhood laptop) blind market-minutes, measured from committed rh_status.json history.

WHY THIS FILE EXISTS (2026-10-08): experiment.json carries a published figure (2026-08-24 to 2026-09-22: 1484 blind
market-minutes = 852 degraded + 632 no-push) but the method lived only in a mailbox thread and a throwaway script. When
LAPTOP DAILY CHECK asked for last night's crash to be counted, the rule had to be reverse-engineered. This file IS the rule,
and it reproduces 852 / 632 EXACTLY for the period 2026-08-24 to 2026-09-22 11:01 ET.

THE RULE (all times Eastern; snapshots are the committed rh_status.json history, one per laptop push):
  * window: weekdays 09:45-15:55 (the bot's own trading window; 09:45-12:55 on the NYSE 1 pm early closes 2026-11-27, 2026-12-24
    and 2027-11-26), alpaca_bot.MARKET_HOLIDAYS excluded; the window itself comes from alpaca_bot.session_bounds
  * a gap between two consecutive snapshots longer than GAP_MIN (30) minutes is NO-PUSH (machine dead, hung, asleep, or
    unable to push); the window minutes inside it count
  * a shorter gap that follows a DEGRADED snapshot is DEGRADED (alive and pushing, but unable to see or trade the
    account: usage limit, expired login); the window minutes inside it count
  * a degraded snapshot followed by a LONG gap still counts its first DEGRADED_COVER (5) minutes as degraded
EDGE MINUTES: the rule scores the minutes between the last heartbeat of a session (about 15:43-15:47) and the 15:55 close,
and between the 09:45 open and the first push (about 09:46-09:50), as no-push, because heartbeats are only about every 15
minutes. That is up to ~17 min per overnight even when nothing is wrong. Devon decided 2026-10-08 that the HEADLINE figure
EXCLUDES them and the figure with them is the stated upper bound; the published 1484 (19.9%) carried them, 1315 (17.6%) does not.

RESOLUTION FLOOR: the rule works from pushes, so it cannot see an outage that leaves a push gap of GAP_MIN (30) minutes or less.
Heartbeats are about 15 minutes apart, so a shorter threshold would invent outages. Known miss: 2026-10-08 11:27 to 11:32 ET (pushes
11:13 then 11:33), about 5 market-minutes. A push-based measure under-reports; compare it with the daemon log when one is available.

Not a trading input. Read-only: it runs `git log` and prints.

Run:  python arm_b_downtime.py [--since YYYY-MM-DD[THH:MM]] [--until YYYY-MM-DD[THH:MM]] [--episodes] [--per-day [--exclude DAY,DAY]]
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# alpaca_bot reads these at import; this tool never talks to a broker.
os.environ.setdefault("ALPACA_API_KEY", "unused-in-arm-b-downtime")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-in-arm-b-downtime")

GAP_MIN = 30
DEGRADED_COVER = 5
EDGE_TAIL_MIN = 15      # a no-push stretch that STARTS within this many minutes of the close: those minutes are edge
BASELINE_DEGRADED_PER_SESSION = 86   # degraded minutes per session, 2026-09-22 to 10-07, before the quota habit (experiment.json)
EDGE_HEAD_MIN = 6       # a no-push stretch that ENDS within this many minutes of the open: those minutes are edge

SUBJECT = re.compile(r"rh bot (\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}) ET \((\w+)\)")


def holidays():
    import alpaca_bot
    return alpaca_bot.MARKET_HOLIDAYS


def parse_subjects(lines):
    """[(datetime naive Eastern, kind)] from commit subjects, oldest first. Lines that do not match are skipped."""
    out = []
    for line in lines:
        m = SUBJECT.search(line)
        if not m:
            continue
        d, h, mi, kind = m.groups()
        out.append((datetime.strptime("%s %s:%s" % (d, h, mi), "%Y-%m-%d %H:%M"), kind))
    out.sort()
    return out


def session(day, hol):
    """(open, close) naive Eastern for a trading day, or None for a weekend or holiday. The hours come from
    alpaca_bot.session_bounds, so a 1 pm early close is 12:55 here too; `hol` stays a parameter so a caller can pass its own set."""
    if day.weekday() >= 5 or day.strftime("%Y-%m-%d") in hol:
        return None
    import alpaca_bot
    b = alpaca_bot.session_bounds(day)
    if b is None:
        return None
    return (b[0].replace(tzinfo=None), b[1].replace(tzinfo=None))


def window_minutes(a, b, hol):
    """Minutes of the trading window that fall between two moments."""
    total, day = 0.0, a.date()
    while day <= b.date():
        s = session(day, hol)
        if s:
            lo, hi = max(s[0], a), min(s[1], b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
    return total


def _edge_minutes(lo, hi, hol):
    """Window minutes of a no-push stretch that are close-side or open-side edge, not outage."""
    edge = 0.0
    s = session(lo.date(), hol)
    if s and lo >= s[1] - timedelta(minutes=EDGE_TAIL_MIN) and hi >= s[1]:
        edge += window_minutes(lo, min(hi, s[1]), hol)
    s2 = session(hi.date(), hol)
    if s2 and hi <= s2[0] + timedelta(minutes=EDGE_HEAD_MIN) and lo <= s2[0]:
        edge += window_minutes(max(lo, s2[0]), hi, hol)
    return edge


def measure(snaps, start, end, hol, gap_min=GAP_MIN, cover=DEGRADED_COVER):
    """Blind market-minutes between start and end. Returns a dict: degraded, no_push, edge, trading, episodes."""
    ctx = [x for x in snaps if start - timedelta(days=4) <= x[0] <= end + timedelta(days=4)]
    degraded = no_push = edge = 0.0
    episodes = []

    def add(kind, lo, hi, minutes, edge_min=0.0):
        if minutes <= 0:
            return
        if episodes and episodes[-1]["kind"] == kind and (lo - episodes[-1]["end"]).total_seconds() <= 30 * 60:
            ep = episodes[-1]
            ep["end"], ep["minutes"], ep["edge"] = hi, ep["minutes"] + minutes, ep["edge"] + edge_min
        else:
            episodes.append({"kind": kind, "start": lo, "end": hi, "minutes": minutes, "edge": edge_min})

    for (a, ka), (b, _kb) in zip(ctx, ctx[1:]):
        lo, hi = max(a, start), min(b, end)
        if hi <= lo:
            continue
        gap = (b - a).total_seconds() / 60.0
        if gap <= gap_min:
            if ka == "degraded":
                m = window_minutes(lo, hi, hol)
                degraded += m
                add("degraded", lo, hi, m)
            continue
        if ka == "degraded":
            cut = min(hi, a + timedelta(minutes=cover))
            if cut > lo:
                m = window_minutes(lo, cut, hol)
                degraded += m
                add("degraded", lo, cut, m)
                lo = cut
        if hi > lo:
            m = window_minutes(lo, hi, hol)
            e = min(m, _edge_minutes(lo, hi, hol))
            no_push += m
            edge += e
            add("no-push", lo, hi, m, e)
    return {"degraded": degraded, "no_push": no_push, "edge": edge, "trading": window_minutes(start, end, hol),
            "episodes": episodes}


def per_day(snaps, start, end, hol):
    """One row per trading session between start and end: {day, trading, degraded, no_push, edge, blind}.

    blind = degraded + no_push - edge, the HEADLINE rule (edge minutes excluded). Each day is measured on its own window, so
    the rows add up to the period figure to within the edge minutes that straddle midnight.
    """
    rows, day = [], datetime(start.year, start.month, start.day)
    while day <= end:
        if session(day, hol) is not None:
            r = measure(snaps, max(start, day), min(end, day + timedelta(days=1)), hol)
            if not r["trading"]:
                day += timedelta(days=1)                    # a window that holds no trading minute (--until midnight) is not a session
                continue
            rows.append({"day": day.strftime("%Y-%m-%d"), "trading": r["trading"], "degraded": r["degraded"],
                         "no_push": r["no_push"], "edge": r["edge"], "blind": r["degraded"] + r["no_push"] - r["edge"]})
        day += timedelta(days=1)
    return rows


def summarize_days(rows, exclude=()):
    """{sessions, degraded_per_session, blind_per_session, excluded}: the means over the rows not named in `exclude`."""
    kept = [r for r in rows if r["day"] not in set(exclude)]
    n = len(kept)
    return {"sessions": n, "excluded": [r["day"] for r in rows if r["day"] in set(exclude)],
            "degraded_per_session": (sum(r["degraded"] for r in kept) / n) if n else None,
            "blind_per_session": (sum(r["blind"] for r in kept) / n) if n else None}


def git_subjects(since="2026-08-20"):
    out = subprocess.run(["git", "log", "--since=" + since, "--format=%s", "--", "rh_status.json"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def _when(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M") if "T" in s else datetime.strptime(s, "%Y-%m-%d")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    since, until, episodes, daily, exclude = "2026-08-24", None, False, False, ()
    i = 0
    while i < len(argv):
        if argv[i] == "--since":
            since = argv[i + 1]
            i += 1
        elif argv[i] == "--until":
            until = argv[i + 1]
            i += 1
        elif argv[i] == "--episodes":
            episodes = True
        elif argv[i] == "--per-day":
            daily = True
        elif argv[i] == "--exclude":
            exclude = tuple(x.strip() for x in argv[i + 1].split(",") if x.strip())
            i += 1
        i += 1
    snaps = parse_subjects(git_subjects())
    if not snaps:
        print("no rh_status.json history found (run from the repo root, with full history)")
        return 2
    start = _when(since)
    end = _when(until) if until else snaps[-1][0]
    r = measure(snaps, start, end, holidays())
    blind = r["degraded"] + r["no_push"]
    print("period %s -> %s ET, %.0f trading minutes (%.1f sessions)" % (start, end, r["trading"], r["trading"] / 370.0))
    if episodes:
        for e in r["episodes"]:
            print("  %-8s %s -> %s %6.0f min%s" % (e["kind"], e["start"].strftime("%m-%d %H:%M"), e["end"].strftime("%m-%d %H:%M"),
                                                   e["minutes"], ("  (edge %.0f)" % e["edge"]) if e["edge"] else ""))
    if daily:
        rows = per_day(snaps, start, end, holidays())
        print("%-11s %7s %9s %8s %5s %7s" % ("day", "minutes", "degraded", "no-push", "edge", "blind"))
        for x in rows:
            print("%-11s %7.0f %9.0f %8.0f %5.0f %7.0f%s" % (x["day"], x["trading"], x["degraded"], x["no_push"], x["edge"], x["blind"],
                                                           "   (excluded below)" if x["day"] in exclude else ""))
        for label, ex in (("all sessions", ()), ("without the excluded days", exclude)):
            if label != "all sessions" and not exclude:
                continue
            sm = summarize_days(rows, ex)
            if sm["sessions"]:
                print("%-27s %d sessions: degraded %.1f min/session (baseline %d), blind %.1f min/session" % (
                    label, sm["sessions"], sm["degraded_per_session"], BASELINE_DEGRADED_PER_SESSION, sm["blind_per_session"]))
    t = r["trading"] or 1.0
    print("HEADLINE (edge minutes excluded): %.0f blind market-minutes = %.1f%% of trading time" % (blind - r["edge"], 100.0 * (blind - r["edge"]) / t))
    print("upper bound (edge minutes in):    degraded %.0f + no-push %.0f = %.0f = %.1f%%  (%.0f of the no-push are close/open edge minutes)" % (
        r["degraded"], r["no_push"], blind, 100.0 * blind / t, r["edge"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
