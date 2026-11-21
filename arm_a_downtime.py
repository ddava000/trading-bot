"""Arm A (the Alpaca cloud bot) coverage of the trading window, measured from GitHub Actions run history.

WHY (2026-10-08, Devon): experiment.json carried a downtime figure for Arm B and none for Arm A, so Arm A read as zero by
omission. LAPTOP BOT put it plainly: the two arms must be scored the same way or not at all. This is Arm A's side, with the
same window and the same holidays as arm_b_downtime.py (imported, not copied).

WHAT IT MEASURES. Arm A has no daemon; each cycle is one workflow run (started by cron-job.org every 15 minutes) that lasts about
10 minutes and runs a 60-second protective loop inside it. Its stop-losses are evaluated only inside a run, so a minute with no
run in progress is a minute nothing was watching. Three numbers, all in minutes of the 09:45-15:55 ET window minus MARKET_HOLIDAYS:
  covered    a run was in progress (run_started_at to updated_at, SUCCESSFUL runs only: a failed or cancelled run did not
             protect; the 2026-10-05 15:30 and 15:45 ET runs both ended "failure" after 15 minutes and Arm A had no stop
             checks from about 15:26 to the close)
  by design  no run in progress, in a gap of GAP_MIN (10) minutes or less: the 15-minute trigger spacing, about 4.5 minutes of
             every 15. NOT downtime in any useful sense, reported so nobody mistakes the other number for the whole story
  unplanned  no run in progress, in a gap LONGER than GAP_MIN: a lost run, a queue stall, a GitHub incident
It is a proxy: a successful run is assumed to have run its protective passes. It cannot see a run that succeeded but whose
state push was lost. It also counts a run's WHOLE run_started_at..updated_at span as coverage, so (a) time a run spent waiting
for a machine counts as watched, and (b) when GitHub delays a run's completion event the span is overlong. 2026-10-19 showed (b):
two runs whose jobs finished at 15:44 and 15:56 were recorded as ending 16:08 and 16:16 during an Actions incident, while their
jobs had started within five seconds and run their normal ten minutes, so the figure was right that day for the wrong reason.
The live check for (a) is ARM A WATCH's 'waiting for a machine' signal.

2026-10-27 CLOSED (a) FOR REAL, because it was not small. On 2026-10-26 seven Arm A runs sat 4 to 13 minutes between the run being
created and its JOB being created (GitHub was slow to start them; its status page listed no incident for it), and this tool, which
counted each run's whole created-to-updated span as watched, reported 0 unplanned minutes while no job was executing for 16 minutes
at 10:10 and 18 minutes at 11:55 (34 in all) and the daily digest said OK. `--by-jobs` (and the digest) now measure from each run's
JOB (started_at to completed_at), where the stop-loss loop actually runs; a run whose job cannot be read falls back to its run span
and is counted, never silently. Not a trading input; read-only.

Run:  python arm_a_downtime.py [--since YYYY-MM-DD[THH:MM]] [--until YYYY-MM-DD[THH:MM]] [--episodes] [--by-jobs]
      --by-jobs reads one jobs page per run (needs GITHUB_TOKEN or GH_TOKEN for more than a day or two of history)
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import arm_b_downtime as B

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
GAP_MIN = 10
MIN_EPISODE_MIN = 1      # a sliver under a minute (a run starting 7 seconds after 09:45) is cadence, not a lost run
REPO = "ddava000/trading-bot"
WORKFLOW = "alpaca-bot.yml"


def fetch_runs(repo=REPO, workflow=WORKFLOW, max_pages=25):
    """Completed runs from the public Actions API, newest first. A GITHUB_TOKEN raises the rate limit but is not needed."""
    out = []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "arm-a-downtime"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    for page in range(1, max_pages + 1):
        url = "https://api.github.com/repos/%s/actions/workflows/%s/runs?per_page=100&page=%d" % (repo, workflow, page)
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            batch = json.loads(r.read().decode("utf-8")).get("workflow_runs", [])
        out += batch
        if len(batch) < 100:
            break
    return out


def _et(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(ET).replace(tzinfo=None)


def _headers():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "arm-a-downtime"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def job_span(run, repo=REPO, fetch=None):
    """{start, end, wait_min} for the run's JOB, naive Eastern, or None if it cannot be read or has not finished.

    wait_min is the time between the RUN being created and its JOB being created: how long GitHub made the run wait before any
    machine could even be asked for. start/end are the job's started_at and completed_at (several jobs: the first start, the last end).
    """
    try:
        if fetch is None:
            url = "https://api.github.com/repos/%s/actions/runs/%s/jobs?per_page=30" % (repo, run["id"])
            with urllib.request.urlopen(urllib.request.Request(url, headers=_headers()), timeout=30) as r:
                jobs = json.loads(r.read().decode("utf-8")).get("jobs") or []
        else:
            jobs = fetch(run)
        jobs = [j for j in jobs if j.get("started_at") and j.get("completed_at")]
        if not jobs:
            return None
        start = min(_et(j["started_at"]) for j in jobs)
        end = max(_et(j["completed_at"]) for j in jobs)
        made = min(_et(j["created_at"]) for j in jobs) if all(j.get("created_at") for j in jobs) else start
        created = _et(run["created_at"]) if run.get("created_at") else start
        return {"start": start, "end": end, "wait_min": max(0.0, (made - created).total_seconds() / 60.0)}
    except Exception as e:
        print("  [arm a downtime: could not read the job of run %s: %s]" % (run.get("id"), e))
        return None


def job_spans(runs, fetch=None):
    """{run id: job_span} for the runs that count (completed and successful). A run whose job cannot be read is simply absent."""
    out = {}
    for r in runs:
        if r.get("status") == "completed" and r.get("conclusion") == "success" and r.get("id"):
            sp = job_span(r, fetch=fetch)
            if sp is not None:
                out[r["id"]] = sp
    return out


def intervals(runs, spans=None, stats=None):
    """[(start, end)] naive Eastern for the runs that count: completed AND successful. Sorted.

    With `spans` (from job_spans) a run is covered from its JOB's start to its JOB's end, so the minutes it spent waiting to be
    started are NOT counted as watched; a run with no span falls back to created-to-updated, and `stats` (a dict) counts which was
    used: stats["job"] and stats["fallback"].
    """
    out = []
    for r in runs:
        if r.get("status") == "completed" and r.get("conclusion") == "success" and r.get("run_started_at") and r.get("updated_at"):
            sp = (spans or {}).get(r.get("id"))
            if sp is not None:
                a, b = sp["start"], sp["end"]
                key = "job"
            else:
                a, b = _et(r["run_started_at"]), _et(r["updated_at"])
                key = "fallback"
            if stats is not None:
                stats[key] = stats.get(key, 0) + 1
            if b > a:
                out.append((a, b))
    out.sort()
    return out


def merge(ivs):
    merged = []
    for a, b in ivs:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def measure(ivs, start, end, hol, gap_min=GAP_MIN):
    """covered / by_design / unplanned window-minutes between start and end. The span is clipped to the first and last run so a
    period that starts before the data (or runs past it) is not charged for minutes nobody observed."""
    merged = merge(ivs)
    if not merged:
        return {"trading": 0.0, "covered": 0.0, "by_design": 0.0, "unplanned": 0.0, "episodes": []}
    start, end = max(start, merged[0][0]), min(end, merged[-1][1])
    covered = by_design = unplanned = 0.0
    episodes = []
    for a, b in merged:
        covered += B.window_minutes(max(a, start), min(b, end), hol) if min(b, end) > max(a, start) else 0.0
    for (_a1, b1), (a2, _b2) in zip(merged, merged[1:]):
        lo, hi = max(b1, start), min(a2, end)
        if hi <= lo:
            continue
        m = B.window_minutes(lo, hi, hol)
        if (a2 - b1).total_seconds() / 60.0 > gap_min and m >= MIN_EPISODE_MIN:
            unplanned += m
            episodes.append({"start": lo, "end": hi, "minutes": m})
        else:
            by_design += m
    return {"trading": B.window_minutes(start, end, hol), "covered": covered, "by_design": by_design, "unplanned": unplanned,
            "episodes": episodes}


def _in(t, start, end):
    return start <= t <= end


def _when(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M") if "T" in s else datetime.strptime(s, "%Y-%m-%d")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    since, until, episodes, by_jobs = "2026-08-24", None, False, False
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
        elif argv[i] == "--by-jobs":
            by_jobs = True
        i += 1
    runs = fetch_runs()
    spans, stats = None, {}
    if by_jobs:
        lo = _when(since) - timedelta(days=1)
        wanted = [r for r in runs if r.get("created_at") and _et(r["created_at"]) >= lo]
        spans = job_spans(wanted)
        intervals(wanted, spans, stats)                   # only to count how many of THIS period's runs were measured by job
    ivs = intervals(runs, spans)
    if not ivs:
        print("no successful Arm A runs found")
        return 2
    start = _when(since)
    end = _when(until) if until else datetime(9999, 1, 1)
    r = measure(ivs, start, end, B.holidays())
    t = r["trading"] or 1.0
    in_period = sum(1 for a, b in ivs if b > start and a < end)
    if by_jobs:
        slow = sorted((s_["wait_min"], rid) for rid, s_ in (spans or {}).items() if s_["wait_min"] >= 4 and _in(spans[rid]["start"], start, end))
        print("measured from each run's JOB: %d runs by job, %d by run span (job unreadable or outside the history read); "
              "runs GitHub took 4+ minutes to start: %d%s" % (stats.get("job", 0), stats.get("fallback", 0), len(slow),
                                                              (" (worst %.0f min)" % slow[-1][0]) if slow else ""))
    print("Arm A, %s -> %s ET: %d successful runs, %.0f trading minutes (%.1f sessions)" % (
        max(start, ivs[0][0]), min(end, ivs[-1][1]), in_period, r["trading"], r["trading"] / 370.0))
    if episodes:
        for e in r["episodes"]:
            print("  unplanned %s -> %s %5.0f min" % (e["start"].strftime("%m-%d %H:%M"), e["end"].strftime("%m-%d %H:%M"), e["minutes"]))
    print("  run in progress %.0f (%.1f%%) | gaps of %d min or less (the 15-minute cadence, by design) %.0f (%.1f%%) | "
          "UNPLANNED %.0f (%.1f%%)" % (r["covered"], 100 * r["covered"] / t, GAP_MIN, r["by_design"], 100 * r["by_design"] / t,
                                       r["unplanned"], 100 * r["unplanned"] / t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
