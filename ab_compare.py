#!/usr/bin/env python3
"""A/B scoreboard for the 2026-11-24 decision: Arm A (Alpaca hybrid) against Arm B (Robinhood index), read-only and hypothetical.

experiment.json fixes the rule, and this follows it: both arms measured against SPY, both adjusted for capital contributed INSIDE the
window only, the BUNDLE reading (Devon 2026-10-13: "which strategy plus platform bundle keeps running"), so Arm B's laptop downtime
stays in its figure and nothing is corrected for it. It reads committed history through pnl.build() and touches no broker, no
order and no live file.

WHAT IT ADDS TO pnl.py. pnl.py gives dollars. A decision needs three more things, and this prints them:
  1. A TIME-WEIGHTED return for each arm (a deposit is not a gain, and a late deposit does not drag or flatter the figure), computed
     twice, with each deposit landing at the START of its day and at the END of it, because the day of a deposit is known and the hour
     is not. If the two disagree the difference is the honest width of the answer.
  2. The risk each arm took for its return: worst day, deepest peak-to-trough fall of the time-weighted index, share of up days.
  3. HOW SURE the gap is. The two arms see the same market, so the gap is judged on the PAIRED daily differences: mean, standard
     error, a t statistic, and the number of trading days it would take to tell a gap this size from luck at 95%. With a couple of
     hundred dollars per arm and a few weeks of data that number is usually far larger than the data in hand; saying so is the point.

NOT A VERDICT. It never recommends an arm and never touches strategy or risk. Realized figures are not tax advice. A sample this small
is shown with its error bar, and the memo repeats the standing caveats from experiment.json.

Run:  python ab_compare.py [--memo] [--no-spy]
"""
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import pnl

WINDOW_OPEN = pnl.WINDOW_OPEN
DECIDE_NO_EARLIER_THAN = "2026-11-24"
Z95 = 1.96


# ---------------------------------------------------------------------------------------------------
# Pure arithmetic (all unit-tested)

def daily_returns(equity, deposits_cum=None, when="start"):
    """[(date, return)] from a date->equity mapping, treating each deposit as landing at the START or the END of its day.

    deposits_cum: date -> cumulative deposits booked on or before that date (None means no deposits). A day with no equity, a day
    after a gap, or a day whose prior equity is not positive yields no return. start: r = E_t / (E_{t-1} + D_t) - 1. end: r = (E_t - D_t) / E_{t-1} - 1.
    """
    days = sorted(d for d, e in equity.items() if e is not None)
    out, prev = [], None
    for d in days:
        e = float(equity[d])
        if prev is not None and prev[1] > 0:
            dep = 0.0
            if deposits_cum is not None:
                dep = float(deposits_cum.get(d, 0.0)) - float(deposits_cum.get(prev[0], 0.0))
            r = e / (prev[1] + dep) - 1.0 if when == "start" else (e - dep) / prev[1] - 1.0
            out.append((d, r))
        prev = (d, e)
    return out


def chain(returns):
    """Cumulative time-weighted return of [(date, r)]."""
    idx = 1.0
    for _, r in returns:
        idx *= 1.0 + r
    return idx - 1.0


def drop_glitches(equity, max_move=0.25):
    """(cleaned equity, [(date, value)] dropped). An observation is a glitch when it sits more than `max_move` away from the MEDIAN of its
    neighbours (up to two on each side): a stale or partial mark, not a market move, for a diversified account of a few hundred dollars.

    First found 2026-10-28: Arm B's first row, 2026-08-24, reads 124.00 between a $231.30 start and 239.74 the next day, which made a
    +93% "day" and a +91% time-weighted return. Dropped observations are LISTED by the caller, never silent.
    """
    days = sorted(d for d, e in equity.items() if e is not None)
    vals = [float(equity[d]) for d in days]
    keep, dropped = {}, []
    for i, d in enumerate(days):
        nb = vals[max(0, i - 2):i] + vals[i + 1:i + 3]
        if len(nb) >= 2:                                    # one neighbour cannot say which of the two is wrong
            nb = sorted(nb)
            med = nb[len(nb) // 2] if len(nb) % 2 else 0.5 * (nb[len(nb) // 2 - 1] + nb[len(nb) // 2])
            if med > 0 and abs(vals[i] / med - 1.0) > max_move:
                dropped.append((d, vals[i]))
                continue
        keep[d] = vals[i]
    return keep, dropped


def anchored(equity, base, base_day):
    """The equity mapping with the declared starting equity added as day zero (so the first real day is measured from the real basis)."""
    out = dict(equity)
    out[base_day] = float(base)
    return out


def max_drawdown(returns):
    """Deepest peak-to-trough fall of the cumulative index, as a negative fraction (0.0 if it never fell)."""
    idx, peak, worst = 1.0, 1.0, 0.0
    for _, r in returns:
        idx *= 1.0 + r
        peak = max(peak, idx)
        worst = min(worst, idx / peak - 1.0)
    return worst


def risk_summary(returns):
    rs = [r for _, r in returns]
    n = len(rs)
    if not n:
        return {"days": 0, "worst_day": None, "best_day": None, "up_days": None, "stdev": None, "max_drawdown": None}
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else None
    return {"days": n, "worst_day": min(rs), "best_day": max(rs), "up_days": sum(1 for x in rs if x > 0) / n,
            "stdev": sd, "max_drawdown": max_drawdown(returns)}


def paired_gap(ra, rb):
    """Judge A minus B on the days both have a return. {days, mean, se, t, days_needed} (days_needed None if the mean gap is ~0)."""
    a, b = dict(ra), dict(rb)
    diffs = [a[d] - b[d] for d in sorted(set(a) & set(b))]
    n = len(diffs)
    if n < 2:
        return {"days": n, "mean": None, "se": None, "t": None, "days_needed": None, "sd": None}
    mean = sum(diffs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1))
    se = sd / math.sqrt(n)
    t = mean / se if se > 0 else None
    # days of data a gap of this size needs before it clears 95% against this much day-to-day noise: n = (z * sd / mean)^2
    needed = math.ceil((Z95 * sd / abs(mean)) ** 2) if mean and abs(mean) > 1e-12 else None
    return {"days": n, "mean": mean, "se": se, "t": t, "days_needed": needed, "sd": sd}


def spy_returns(closes):
    """[(date, r)] from a date->close mapping."""
    days = sorted(closes)
    return [(days[i], closes[days[i]] / closes[days[i - 1]] - 1.0) for i in range(1, len(days)) if closes[days[i - 1]]]


# ---------------------------------------------------------------------------------------------------
# Inputs

def fetch_spy(start_iso, fetch=None):
    """date -> SPY close from Yahoo's public chart endpoint, or None if it cannot be read. No key, no account."""
    try:
        if fetch is None:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=6mo&interval=1d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ab-compare"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            data = fetch()
        res = data["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        out = {}
        for ts, c in zip(res["timestamp"], closes):
            if c is None:
                continue
            day = datetime.fromtimestamp(ts, timezone.utc).astimezone(_et()).strftime("%Y-%m-%d")
            if day >= (datetime.strptime(start_iso, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d"):
                out[day] = float(c)
        return out or None
    except Exception as e:
        print("  [ab_compare: could not read SPY: %s]" % e)
        return None


def _et():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def load():
    """(rows, a_base, b_start, ev, window) from committed history, via pnl.build()."""
    rows, a_base, b_start, b_dropped, ev, w = pnl.build()
    return {"rows": rows, "a_base": a_base, "b_start": b_start, "events": ev, "window": w, "b_dropped": b_dropped}


def series(rows):
    """(a_equity, b_equity, b_deposits_cum) as date-keyed dicts from pnl rows."""
    a = {r["date"]: r["a_equity"] for r in rows if r["a_equity"] is not None}
    b = {r["date"]: r["b_equity"] for r in rows if r["b_equity"] is not None}
    dep = {r["date"]: r["b_deposits"] for r in rows}
    return a, b, dep


# ---------------------------------------------------------------------------------------------------
# The scoreboard

def score(rows, a_base, b_start, spy=None):
    """Everything the memo needs, as a dict. rows are pnl.build() rows; spy is date->close or None."""
    a, b, dep = series(rows)
    a, a_dropped = drop_glitches(a)
    b, b_dropped = drop_glitches(b)
    first_day = rows[0]["date"] if rows else None
    day0 = (datetime.strptime(first_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d") if first_day else None
    if first_day:
        a = anchored(a, a_base, day0)
        b = anchored(b, b_start, day0)
        dep = dict(dep, **{day0: 0.0})
    out = {"rows": len(rows), "first": first_day, "last": rows[-1]["date"] if rows else None,
           "glitches": {"Arm A": a_dropped, "Arm B": b_dropped}, "a_base": a_base}
    last = rows[-1] if rows else None
    out["a_pl"] = last["a_pl"] if last else None
    out["b_pl"] = last["b_pl"] if last else None
    b_basis = (b_start + last["b_deposits"]) if last else None
    out["a_pct"] = 100.0 * out["a_pl"] / a_base if (last and out["a_pl"] is not None and a_base) else None
    out["b_pct"] = 100.0 * out["b_pl"] / b_basis if (last and out["b_pl"] is not None and b_basis) else None
    out["b_basis"] = b_basis
    ra = daily_returns(a)
    rb_s, rb_e = daily_returns(b, dep, "start"), daily_returns(b, dep, "end")
    out["twr_a"] = 100.0 * chain(ra)
    out["twr_b_start"], out["twr_b_end"] = 100.0 * chain(rb_s), 100.0 * chain(rb_e)
    out["risk_a"], out["risk_b"] = risk_summary(ra), risk_summary(rb_s)
    out["gap"] = paired_gap(ra, rb_s)
    out["gap_end_convention"] = paired_gap(ra, rb_e)
    if spy:
        rs = [(d, r) for d, r in spy_returns(spy) if rows and d >= rows[0]["date"] and d <= rows[-1]["date"]]
        out["spy_twr"] = 100.0 * chain(rs)
        out["spy_risk"] = risk_summary(rs)
        out["gap_a_vs_spy"] = paired_gap(ra, rs)
        out["gap_b_vs_spy"] = paired_gap(rb_s, rs)
    else:
        out["spy_twr"] = None
    return out


def _usd(x):
    return "n/a" if x is None else ("-$%.2f" % abs(x) if x < 0 else "+$%.2f" % x)


def _pct(x, nd=2):
    return "n/a" if x is None else "%+.*f%%" % (nd, x)


def _frac(x, nd=2):
    return "n/a" if x is None else "%+.*f%%" % (nd, 100.0 * x)


def memo(s, downtime=None):
    """The one-page memo as text. `downtime` is an optional dict of operational facts (printed, never used to adjust a return)."""
    L = []
    L.append("A/B SCOREBOARD for the %s decision (read-only; never a verdict)" % DECIDE_NO_EARLIER_THAN)
    L.append("window %s to %s, %d trading days with data" % (s["first"], s["last"], s["rows"]))
    L.append("")
    L.append("1. THE RULE'S NUMBERS (dollars net of in-window deposits, experiment.json rule)")
    L.append("   Arm A: %s (%s of its $%.2f start)" % (_usd(s["a_pl"]), _pct(s["a_pct"]), s.get("a_base") or 0))
    L.append("   Arm B: %s (%s of its $%.2f basis: start plus deposits)" % (_usd(s["b_pl"]), _pct(s["b_pct"]), s["b_basis"] or 0))
    if s.get("spy_twr") is not None:
        L.append("   SPY over the same days: %s" % _pct(s["spy_twr"]))
    else:
        L.append("   SPY: not read (offline or --no-spy); the rule asks for it, so do not decide without it")
    L.append("")
    for arm, dropped in s["glitches"].items():
        if dropped:
            L.append("   DATA GLITCH dropped for %s (far from its neighbours, a stale or partial mark): %s" % (
                arm, ", ".join("%s %.2f" % (d, v) for d, v in dropped)))
    L.append("")
    L.append("2. TIME-WEIGHTED (a deposit is not a gain; the hour of a deposit is unknown, so both bounds)")
    L.append("   Arm A %s | Arm B %s if deposits land at the start of their day, %s if at the end" % (_pct(s["twr_a"]), _pct(s["twr_b_start"]), _pct(s["twr_b_end"])))
    spread = abs(s["twr_b_start"] - s["twr_b_end"])
    L.append("   the deposit-timing uncertainty on Arm B alone is %.2f points" % spread)
    if s.get("spy_twr") is not None:
        L.append("   against SPY (time-weighted points): Arm A %+.2f, Arm B %+.2f (B at the start-of-day convention)" % (
            s["twr_a"] - s["spy_twr"], s["twr_b_start"] - s["spy_twr"]))
    L.append("")
    L.append("3. RISK TAKEN")
    for name, k in (("Arm A", "risk_a"), ("Arm B", "risk_b")):
        r = s[k]
        L.append("   %s: worst day %s, best day %s, deepest fall %s, up on %s of %s days" % (
            name, _frac(r["worst_day"]), _frac(r["best_day"]), _frac(r["max_drawdown"]),
            "n/a" if r["up_days"] is None else "%d%%" % round(100 * r["up_days"]), r["days"]))
    L.append("")
    L.append("4. HOW SURE IS THE GAP (paired daily differences, A minus B)")
    g = s["gap"]
    if g["mean"] is None:
        L.append("   too few overlapping days to say anything")
    else:
        L.append("   mean gap %s per day, standard error %s, t = %.2f over %d days" % (_frac(g["mean"], 3), _frac(g["se"], 3), g["t"] or 0.0, g["days"]))
        if g["days_needed"]:
            L.append("   at this size of gap and this much day-to-day noise it takes about %d trading days to be 95%% sure it is not luck; you have %d" % (g["days_needed"], g["days"]))
        verdict = "NOT distinguishable from luck" if g["t"] is None or abs(g["t"]) < Z95 else "larger than luck at 95%, in this window only"
        L.append("   so, on the data in hand: %s" % verdict)
    if downtime:
        L.append("")
        L.append("5. OPERATIONAL FACTS (shown, NOT adjusted for: the BUNDLE reading, confirmed by Devon 2026-10-13)")
        for k, v in downtime.items():
            L.append("   %s: %s" % (k, v))
    L.append("")
    L.append("WHAT THIS CANNOT SHOW")
    L.append("   - one market regime, a few weeks, about $250 to $320 per arm: one bad day moves either figure by a point")
    L.append("   - the arms differ in deployment history (experiment.json known_confound), in the platform, and in what each did on the same days")
    L.append("   - a gap that is real now can reverse in a different regime; nothing here forecasts")
    L.append("   - not tax advice; the broker's 1099-B is authoritative")
    return "\n".join(L)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    d = load()
    spy = None if "--no-spy" in argv else fetch_spy(d["rows"][0]["date"] if d["rows"] else WINDOW_OPEN)
    s = score(d["rows"], d["a_base"], d["b_start"], spy)
    print(memo(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
