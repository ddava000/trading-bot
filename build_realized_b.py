#!/usr/bin/env python3
"""Build realized_b.json (Arm B, Robinhood) from the broker's own tool output.

WHY A SCRIPT: Arm B's realized figures come from the BROKER, and nothing forces anyone to
re-pull them after a sale. Arm B is index-only and rarely sells, so its ledger can sit
unchanged for weeks and still be right, which is exactly how it would go silently wrong
after the next sale. This makes the refresh deterministic and cheap: save two broker tool
outputs to files, run this, commit the result.

INPUTS (the tools' native JSON, saved as returned; nothing needs reshaping):
  --orders  get_equity_orders   {"data": {"orders": [...]}}   state=filled, ALL history
                                (created_at_gte 2026-05-01 returned every order in one page)
  --pnl     get_pnl_trade_history {"data": {"trades": [...]}} span=all
Both for the Agentic account only. Read-only: this script places nothing.

HOW IT STAYS HONEST: the summary uses the BROKER's realized_gain per sale (authoritative).
FIFO is run independently on the same executions as a cross-check, sale by sale, and the
ledger says UNVERIFIED unless every sale matches to within 2 cents, the total to within 10
cents, and the FIFO holdings equal the positions the daemon publishes. On 2026-09-29 all 50
sales matched Robinhood's rows (48 to the cent, all within 1 cent, total off by 2 cents).

NOT A TAX DOCUMENT. The broker 1099-B is authoritative.
"""
import argparse
import json
import os
import sys
from datetime import datetime

import realized as R

SALE_TOL, TOTAL_TOL, MATCH_SECS = 0.02, 0.10, 120


def _ts(x):
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def build(orders, trades, status=None, rh_log="rh_trade_log.jsonl"):
    filled = [o for o in orders if o.get("state") == "filled" and o.get("executions")]
    fills = [{"t": e["timestamp"], "symbol": o["symbol"], "side": o["side"], "qty": e["quantity"],
              "price": e["price"], "order": o["id"], "agent": o.get("placed_agent")}
             for o in filled for e in o["executions"]]
    res = R.fifo(fills)
    by_order = {o["id"]: o for o in filled}

    sales = {}
    for l in res["closed"]:
        sales.setdefault(l["sale"], []).append(l)

    # Pair each FIFO sale (an order) with the broker's row: same symbol, closest in time.
    used, pairs, problems, sale_terms = set(), [], [], {}
    for sid, lots in sorted(sales.items(), key=lambda kv: by_order[kv[0]]["executions"][-1]["timestamp"]):
        o = by_order[sid]
        t = o["executions"][-1]["timestamp"]
        cand = sorted((abs((_ts(t) - _ts(b["timestamp"])).total_seconds()), i)
                      for i, b in enumerate(trades) if b["symbol"] == o["symbol"] and i not in used)
        mine = round(sum(l["gain"] for l in lots), 2)
        if not cand or cand[0][0] > MATCH_SECS:
            problems.append("no broker row for %s sold %s" % (o["symbol"], t[:16]))
            continue
        i = cand[0][1]
        used.add(i)
        pairs.append({"symbol": o["symbol"], "when": t, "fifo": mine, "broker": float(trades[i]["realized_gain"]),
                      "row": i})
        sale_terms[i] = "LT" if any(l["term"] == "LT" for l in lots) else "ST"
    for i, b in enumerate(trades):
        if i not in used:
            problems.append("broker row with no FIFO sale: %s %s" % (b["symbol"], b["timestamp"][:16]))

    max_diff = max((abs(p["fifo"] - p["broker"]) for p in pairs), default=0.0)
    total_diff = round(sum(p["fifo"] for p in pairs) - sum(p["broker"] for p in pairs), 2)
    if max_diff > SALE_TOL:
        problems.append("a sale differs from the broker by %.2f" % max_diff)
    if abs(total_diff) > TOTAL_TOL:
        problems.append("total differs from the broker by %.2f" % total_diff)
    if res["unmatched"]:
        problems.append("%d sell(s) with no buy behind them" % len(res["unmatched"]))
    if status is not None:
        bad = R.verify_positions(res["open"], status.get("positions") or {}, tol=1e-6)
        if bad:
            problems.append("FIFO holdings differ from the daemon's positions: " + ", ".join(
                "%s %.6f vs %.6f" % (b["symbol"], b["from_fills"], b["broker"]) for b in bad[:3]))

    # THE BROKER's figure is what we publish, classified per sale like its own rows.
    rows = [(sale_terms.get(i, "ST"), b["realized_gain"]) for i, b in enumerate(trades)]
    summ = R.summarize_sales(rows)
    by_year = {}
    for i, b in enumerate(trades):
        y = str(R.trade_date(b["timestamp"]).year)
        by_year.setdefault(y, []).append((sale_terms.get(i, "ST"), b["realized_gain"]))
    by_year = {y: R.summarize_sales(v) for y, v in sorted(by_year.items())}

    watch = R.wash_watch(res["closed"], fills)
    dates = sorted(R.trade_date(b["timestamp"]).isoformat() for b in trades)
    last_exec = max((e["timestamp"] for o in filled for e in o["executions"]), default=None)
    summary = dict(summ)
    summary.update({
        "state": "unverified" if problems else "ok",
        "since": dates[0] if dates else None, "last_sale": dates[-1] if dates else None,
        "as_of": last_exec, "wash_watch": R.wash_totals(watch), "by_year": by_year,
        "rh_log_sells_counted": R.rh_sells_logged(rh_log),
        "verification": {"sales_matched": len(pairs), "broker_rows": len(trades),
                         "max_sale_difference": round(max_diff, 2), "total_difference_fifo_minus_broker": total_diff,
                         "positions_checked_against_daemon": status is not None},
        "method": "Broker per-sale realized gain (authoritative), cross-checked by independent FIFO",
    })
    if problems:
        summary["reason"] = "; ".join(problems[:4])
    ledger = {
        "arm": "B", "source": "Robinhood Agentic account: get_pnl_trade_history + get_equity_orders",
        "method": summary["method"],
        "note": "NOT a tax document. The broker 1099-B is authoritative. Wash-sale figures are an "
                "UPPER BOUND for a tax preparer to judge, not an adjustment. Dividend income (two "
                "dividend-reinvestment purchases exist) is separate and is not in these totals. "
                "REFRESH after any sale: see build_realized_b.py.",
        "summary": summary,
        "closed": res["closed"], "wash_watch": watch, "open": res["open"],
        "broker_rows": [{"date": R.trade_date(b["timestamp"]).isoformat(), "symbol": b["symbol"],
                         "qty": float(b["quantity"]), "realized_gain": float(b["realized_gain"])} for b in trades],
        "buys": [{"t": R.trade_date(f["t"]).isoformat(), "symbol": f["symbol"], "qty": float(f["qty"])}
                 for f in fills if f["side"] == "buy"],
    }
    return ledger


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--orders", required=True)
    ap.add_argument("--pnl", required=True)
    ap.add_argument("--status", default="rh_status.json")
    ap.add_argument("--rh-log", default="rh_trade_log.jsonl")
    ap.add_argument("--out", default="realized_b.json")
    a = ap.parse_args()
    orders = json.load(open(a.orders, encoding="utf-8"))["data"]["orders"]
    trades = json.load(open(a.pnl, encoding="utf-8"))["data"]["trades"]
    status = json.load(open(a.status, encoding="utf-8")) if os.path.exists(a.status) else None
    led = build(orders, trades, status, a.rh_log)
    new = json.dumps(led, indent=1, sort_keys=True)
    old = open(a.out, encoding="utf-8").read() if os.path.exists(a.out) else ""
    if new != old:                       # deterministic: only changes when the data does
        open(a.out, "w", encoding="utf-8").write(new)
    s = led["summary"]
    print(R.report_line("Arm B", s))
    print("state=%s  %s" % (s["state"], s.get("reason", "")))
    print("verification:", json.dumps(s["verification"]))
    return 0 if s["state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
