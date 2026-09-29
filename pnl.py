#!/usr/bin/env python3
"""Dollar P/L for both arms, reconstructed from committed history.

WHY THIS EXISTS: every report in this repo quoted PERCENTAGES. Devon asked on
2026-09-24 how many DOLLARS each arm has made or lost. Percent hides the thing
he actually funds: Arm B has taken $50 of new cash inside the window and Arm A
has taken none, so the same percent is not the same money.

ACCURACY NOTES, the ones that bite:

1. ARM B STALE EQUITY. publish_degraded() copies the LAST KNOWN equity forward
   into every degraded snapshot so monitoring keeps a number. 610 of 1514
   committed rh_status.json revisions carry `degraded`. Reading equity off those
   reports a mark that was never observed at that time, so this file DROPS every
   snapshot carrying `degraded` and prints how many it dropped. Including them
   silently would render outages as flat days rather than as missing ones.

2. DEPOSITS ARE NOT PROFIT. Arm B's basis rises with each deposit. Dollar P/L is
   equity minus start_equity minus deposits booked ON OR BEFORE that date, so a
   $10 Monday deposit never reads as a $10 Monday gain.

3. ARM A CAPITAL FLOW IS ONLY RECORDED FROM 2026-09-01. Before that the field did
   not exist (the "detected and never persisted" bug). --audit checks the
   uncovered stretch for the cash step a deposit would leave, rather than
   assuming the stretch was clean.

4. THE TWO ARMS ARE SAMPLED AT DIFFERENT TIMES. Arm A publishes every 15 min,
   Arm B every 60s while awake. Same-day rows are not simultaneous marks.
"""
import subprocess
import json
import sys
from collections import OrderedDict

WINDOW_OPEN = "2026-08-24"


def _revisions(path):
    """(commit_iso, blob_sha) for every committed revision of path, newest first."""
    out = subprocess.run(["git", "log", "--format=C %H %cI", "--raw", "--abbrev=40",
                          "--", path], capture_output=True, text=True, check=True).stdout
    pairs, when = [], None
    for line in out.splitlines():
        if line.startswith("C "):
            when = line.split()[2]
        elif line.startswith(":") and when:
            sha = line.split()[3]
            if sha != "0" * 40:
                pairs.append((when, sha))
    return pairs


def _read(shas):
    """Batch blob read: one subprocess for the whole history, not one per commit."""
    p = subprocess.run(["git", "cat-file", "--batch"],
                       input="\n".join(shas) + "\n", capture_output=True, text=True)
    out, res, i = p.stdout, {}, 0
    for _ in shas:
        nl = out.find("\n", i)
        if nl < 0:
            break
        hdr = out[i:nl].split()
        if len(hdr) < 3:
            i = nl + 1
            continue
        size = int(hdr[2])
        res[hdr[0]] = out[nl + 1:nl + 1 + size]
        i = nl + 1 + size + 1
    return res


def _snapshots(path):
    """Parsed snapshots, deduped by blob, oldest first."""
    revs = _revisions(path)
    blobs = _read(list(dict.fromkeys(s for _, s in revs)))
    seen, out = set(), []
    for when, sha in reversed(revs):
        if sha in seen or sha not in blobs:
            continue
        seen.add(sha)
        try:
            out.append((when, json.loads(blobs[sha])))
        except ValueError:
            pass
    return out


def arm_a():
    """Daily end-of-day Arm A equity, its basis, and any recorded capital flow."""
    days, flow = OrderedDict(), {}
    for _, d in _snapshots("status.json"):
        ts = str(d.get("ts") or d.get("as_of_utc") or "")
        day, eq = ts[:10], d.get("equity")
        if not day or eq is None:
            continue
        days[day] = {"equity": float(eq),
                     "cash": d.get("cash"),
                     "baseline": (d.get("baseline") or {}).get("equity")}
        cf = d.get("capital_flow")
        if cf:
            flow[day] = {"state": cf.get("state"), "net": float(cf.get("net") or 0.0)}
    return days, flow


def arm_b():
    """Daily end-of-day Arm B equity from LIVE snapshots only. Degraded ones lie."""
    days, dropped = OrderedDict(), 0
    for _, d in _snapshots("rh_status.json"):
        if d.get("degraded"):
            dropped += 1
            continue
        eq, day = d.get("equity"), str(d.get("ts") or "")[:10]
        if not day or eq is None:
            continue
        days[day] = {"equity": float(eq)}
    return days, dropped


def deposits():
    d = json.load(open("rh_deposits.json"))
    w = d["experiment_window"]
    ev = sorted((e["date"], float(e["amount"]), e.get("confidence")) for e in d["events"])
    return float(w["start_equity"]), ev, w


def build():
    a_days, a_flow = arm_a()
    b_days, b_dropped = arm_b()
    b_start, ev, w = deposits()

    a_base = None
    for day in sorted(a_days):
        if day >= WINDOW_OPEN and a_days[day]["baseline"]:
            a_base = float(a_days[day]["baseline"])
            break

    rows = []
    for day in sorted(set(a_days) | set(b_days)):
        if day < WINDOW_OPEN:
            continue
        a, b = a_days.get(day), b_days.get(day)
        dep = sum(amt for dt, amt, _ in ev if WINDOW_OPEN <= dt <= day)
        net = a_flow.get(day, {}).get("net", 0.0)
        rows.append({
            "date": day,
            "a_equity": a["equity"] if a else None,
            "a_pl": (a["equity"] - a_base - net) if (a and a_base) else None,
            "a_flow": a_flow.get(day, {}).get("state"),
            "b_equity": b["equity"] if b else None,
            "b_deposits": dep,
            "b_pl": (b["equity"] - b_start - dep) if b else None,
        })
    return rows, a_base, b_start, b_dropped, ev, w


def main():
    rows, a_base, b_start, b_dropped, ev, w = build()

    print("DOLLAR P/L SINCE THE WINDOW OPENED %s" % WINDOW_OPEN)
    print("Arm A basis (start_equity) $%.2f   Arm B start_equity $%.2f" % (a_base, b_start))
    print("Arm B in-window deposits $%.2f, adjusted_basis $%.2f"
          % (w["deposits_in_window"], w["adjusted_basis"]))
    print("Dropped %d degraded Arm B snapshots (stale equity, never observed)" % b_dropped)
    print()
    print("date        ArmA eq   ArmA P/L  flow  |  ArmB eq   dep    ArmB P/L")
    print("-" * 68)
    for r in rows:
        ae = "%9.2f" % r["a_equity"] if r["a_equity"] is not None else "        -"
        ap = "%9.2f" % r["a_pl"] if r["a_pl"] is not None else "        -"
        be = "%8.2f" % r["b_equity"] if r["b_equity"] is not None else "       -"
        bp = "%9.2f" % r["b_pl"] if r["b_pl"] is not None else "        -"
        print("%s %s %s %-5s | %s %6.2f %s"
              % (r["date"], ae, ap, (r["a_flow"] or "-")[:5], be, r["b_deposits"], bp))

    last_a = [r for r in rows if r["a_pl"] is not None][-1]
    last_b = [r for r in rows if r["b_pl"] is not None][-1]
    print("-" * 68)
    print("ARM A (Alpaca hybrid):   %+8.2f dollars  as of %s (equity %.2f)"
          % (last_a["a_pl"], last_a["date"], last_a["a_equity"]))
    print("ARM B (Robinhood index): %+8.2f dollars  as of %s (equity %.2f, $%.2f paid in)"
          % (last_b["b_pl"], last_b["date"], last_b["b_equity"], last_b["b_deposits"]))
    print("COMBINED:                %+8.2f dollars" % (last_a["a_pl"] + last_b["b_pl"]))
    print()
    # The dollars above are EQUITY change, which includes unrealized paper P&L. Tax only
    # cares about positions actually SOLD, which is a different and usually smaller
    # number, so print both and label them. (Devon, 2026-09-29)
    print("REALIZED, sold positions only (this is the taxable number, not the equity change):")
    try:
        import realized as _R
        for _ln in _R.repo_report_lines():
            print("  " + _ln)
    except Exception as _e:
        print("  realized totals UNAVAILABLE (%s)" % _e)
    print()
    print("No conclusion before 2026-11-24. Deposits are basis, not profit.")

    if "--audit" in sys.argv:
        a_days, _ = arm_a()
        print()
        print("=== AUDIT: Arm A capital-flow coverage gap ===")
        have = [r for r in rows if r["a_equity"] is not None]
        covered = [r for r in rows if r["a_flow"]]
        print("capital_flow recorded on %d of %d Arm A days; first recorded %s"
              % (len(covered), len(have), covered[0]["date"] if covered else "never"))
        print("distinct recorded states: %s"
              % sorted({r["a_flow"] for r in covered if r["a_flow"]}))
        gap = [r for r in have if not r["a_flow"]]
        if gap:
            print("UNCOVERED: %s .. %s" % (gap[0]["date"], gap[-1]["date"]))
            print("A deposit leaves a cash step with no market cause. Daily moves there:")
            prev = None
            for r in gap:
                eq = r["a_equity"]
                d = "" if prev is None else "  change %+.2f" % (eq - prev)
                print("   %s equity %8.2f  cash %7.2f%s"
                      % (r["date"], eq, a_days[r["date"]]["cash"] or 0.0, d))
                prev = eq


if __name__ == "__main__":
    main()
