#!/usr/bin/env python3
"""Hypothetical, read-only: how far past the documented -7% stop did Arm A's exits actually fill?

WHY (2026-10-28). Arm A evaluates its stops inside each 15-minute run; there are no stop orders at the broker. The cost of that design has
a visible part: a position that is already below -7% when a run notices it fills below -7%. Devon deferred the broker-side-stop question until
after the 11-24 decision; this measures the part of it that the history can answer, so that decision starts from a number.

HOW. realized_a.json lists every closed lot with its basis, proceeds and the id of the sale; trade_log.jsonl lists each sell with the flag that
caused it (stop_loss, hold_stop, time_stop, take_profit, news_exit) and the price the bot saw when it decided. Joining on the order id gives,
for each exit, the realized return and the reason. The comparison is against the stop level ONLY for the exits whose reason is stop_loss
(-7%); hold_stop is the hold sleeve's own, much wider stop and is reported separately.

WHAT IT CAN SHOW: the overshoot (realized return minus -7%) and the dollars it cost against a perfect fill at -7%; the fill-versus-decision
slippage (tiny: the problem is not the order, it is the price when the run looked).
WHAT IT CANNOT SHOW, and it says so: what a broker-side stop would have cost in whipsaws (a stop that fires on a wick and sees the price recover)
and gap-throughs (a stop-market fills at the open, below the stop, exactly as this does), because that needs minute prices after each exit
that this repository does not keep. Nothing here is a recommendation and nothing changes a bot.

Run:  python stop_study.py
"""
import collections
import json
import os
import statistics
import sys

STOP_LEVEL = -0.07
FLAGS = ("stop_loss", "hold_stop", "time_stop", "take_profit", "news_exit")


def load(root="."):
    with open(os.path.join(root, "realized_a.json"), encoding="utf-8") as f:
        real = json.load(f)
    with open(os.path.join(root, "trade_log.jsonl"), encoding="utf-8") as f:
        log = [json.loads(line) for line in f if line.strip()]
    return real, log


def join(real, log):
    """One row per closed lot: reason (the first set flag on its sell, 'other', or 'unmatched'), realized return, decision and fill prices."""
    sells = {r.get("order_id"): r for r in log if str(r.get("side")).lower() == "sell" and r.get("order_id")}
    rows = []
    for lot in real.get("closed", []):
        s = sells.get(lot.get("sale"))
        reason = "unmatched" if s is None else next((f for f in FLAGS if s.get(f)), "other")
        basis, qty = lot.get("basis") or 0.0, lot.get("qty") or 0.0
        rows.append({"symbol": lot.get("symbol"), "open": lot.get("open_date"), "close": lot.get("close_date"), "reason": reason,
                     "ret": (lot["gain"] / basis) if basis else None, "gain": lot.get("gain") or 0.0, "basis": basis,
                     "decision": (s or {}).get("live"), "fill": (lot["proceeds"] / qty) if qty else None})
    return rows


def study(rows, level=STOP_LEVEL):
    """Numbers for the stop_loss exits against `level`, plus a per-reason table."""
    by = collections.defaultdict(list)
    for r in rows:
        by[r["reason"]].append(r)
    table = {}
    for k, v in by.items():
        rets = [r["ret"] for r in v if r["ret"] is not None]
        table[k] = {"lots": len(v), "median": statistics.median(rets) if rets else None, "worst": min(rets) if rets else None,
                    "gain": sum(r["gain"] for r in v), "basis": sum(r["basis"] for r in v)}
    stops = [r for r in by.get("stop_loss", []) if r["ret"] is not None]
    out = {"table": table, "n": len(stops)}
    if stops:
        worse = [r for r in stops if r["ret"] < level]
        perfect = sum(level * r["basis"] for r in stops)
        actual = sum(r["gain"] for r in stops)
        slips = [r["fill"] / r["decision"] - 1.0 for r in stops if r.get("decision") and r.get("fill")]
        out.update({"worse": len(worse), "worst": min(r["ret"] for r in stops), "mean_overshoot_pts": (statistics.mean(level - r["ret"] for r in worse) * 100.0) if worse else 0.0,
                    "actual": actual, "perfect": perfect, "cost": actual - perfect, "basis": sum(r["basis"] for r in stops),
                    "median_fill_vs_decision_pct": (statistics.median(slips) * 100.0) if slips else None,
                    "biggest": sorted(stops, key=lambda r: r["ret"])[:5]})
    return out


def report(st):
    L = ["ARM A STOP-LOSS FILLS vs the documented %.0f%% level (hypothetical, read-only)" % (STOP_LEVEL * 100), ""]
    L.append("%-12s %5s %9s %9s %9s %9s" % ("exit reason", "lots", "median %", "worst %", "total $", "basis $"))
    for k, t in sorted(st["table"].items(), key=lambda kv: -kv[1]["lots"]):
        L.append("%-12s %5d %9s %9s %+9.2f %9.2f" % (k, t["lots"], "n/a" if t["median"] is None else "%+.2f" % (100 * t["median"]),
                                                    "n/a" if t["worst"] is None else "%+.2f" % (100 * t["worst"]), t["gain"], t["basis"]))
    L.append("")
    if not st["n"]:
        L.append("no stop_loss exits in the ledger")
    else:
        L.append("%d stop_loss exits: %d filled below the level (mean overshoot %.2f points), worst %.2f%%" % (
            st["n"], st["worse"], st["mean_overshoot_pts"], 100 * st["worst"]))
        L.append("dollars: actual %+.2f, if every one had filled exactly at the level %+.2f, so the overshoot cost %+.2f on $%.2f of positions" % (
            st["actual"], st["perfect"], st["cost"], st["basis"]))
        if st["median_fill_vs_decision_pct"] is not None:
            L.append("median fill vs the price at decision: %+.2f%% (the ORDER is not the problem; the price when the run looked is)" % st["median_fill_vs_decision_pct"])
        L.append("largest overshoots: " + "; ".join("%s %s->%s %+.2f%%" % (r["symbol"], r["open"], r["close"], 100 * r["ret"]) for r in st["biggest"]))
    L.append("")
    L.append("NOT SHOWN: the cost of broker-side stops (whipsaws on a wick that recovers; gap-throughs fill below the stop there too). That needs minute")
    L.append("prices after each exit, which this repository does not keep. This is a measurement for Devon's post-11-24 decision, not a recommendation.")
    return "\n".join(L)


def main(argv=None):
    real, log = load(".")
    print(report(study(join(real, log))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
