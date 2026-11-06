#!/usr/bin/env python3
"""Build realized_b.json (Arm B, Robinhood) from the broker's own tool output.

WHY A SCRIPT: Arm B's realized figures come from the BROKER, and nothing forces anyone to
re-pull them after a sale. Arm B is index-only and rarely sells, so its ledger can sit
unchanged for weeks and still be right, which is exactly how it would go silently wrong
after the next sale. This makes the refresh deterministic and cheap: save two broker tool
outputs to files, run this, commit the result.

REFRESH PROCEDURE (run by the LAPTOP session, which has the broker bridge; the cloud has none).
Proven end to end 2026-09-30: 139 orders and 50 P&L rows, 50 of 50 sales matched, and the rebuilt
ledger equalled the committed one. It took four attempts and four faults; each is a rule below.
 WHEN: after ANY sale by rh_daemon, or whenever realized.arm_b_block() reports the ledger stale.
 1. TOOLS, all read-only, Agentic account only (never read another account):
      get_accounts (find the account that has agentic_allowed; the number never needs to be typed),
      get_equity_orders (state=filled, everything since 2026-05-01),
      get_pnl_trade_history (span=all),
      get_equity_positions (the completeness check: a symbol bought after the P&L history began
      and still held never appears in that history, so held positions are the only way to confirm
      nothing is missing).
    Do NOT assume one page. On 2026-09-29 one page held all 139 orders; on 2026-09-30 it did not,
    and the agent walked date windows and then each symbol. Page or window until the count stops
    growing, and say in the final line how many it got.
 2. THERE IS NO HUMAN in a headless `claude -p` run. A question goes nowhere and the run still
    exits 0 with no files, which is indistinguishable from success. The prompt MUST say that nobody
    can answer, that producing nothing is the worst outcome, and that the agent should write what
    it has and name the limitation in its last line. A tool outside the allowlist is therefore a
    limitation to report, not a question to ask: grant the four tools above up front.
 3. WRITE INCREMENTALLY. Write orders.json the moment the orders are collected, before starting the
    P&L call, so a run cut short still leaves something usable.
 4. WHERE THE RAW FILES GO: OUTSIDE ANY REPO (the laptop uses C:/Users/devon/rh_export/). They carry
    the account number and this repo is PUBLIC. Run the bridge with a working directory outside the
    repo too, so it does not inherit this repo's CLAUDE.md and rightly refuse. Only the sanitised
    realized_b.json is ever committed. .gitignore also blocks _rh_*.json and rh_export/.
 5. FILE SHAPE: save the tool output as returned. This script accepts the full envelope
    {"data": {"orders": [...]}} / {"data": {"trades": [...]}}, the inner object, or a bare list.
 6. POST-CONDITION before trusting a run: both files exist, parse, and the builder prints a
    plausible "export check" line. It REFUSES, writing nothing and exiting 2, if the orders list is
    empty or there are more broker sale rows than orders. Exit 0 from the export step proves
    nothing. NEVER read an exit code through a pipe: `claude ... | tail` reports tail's status,
    which is always 0, so a killed run looks clean. Capture claude's own status directly.
 Then:  python build_realized_b.py --orders <orders.json> --pnl <pnl.json>
        (exit 0 = state ok; exit 1 = built but UNVERIFIED, read the reason; exit 2 = refused)
        git add realized_b.json, commit, push (explicit path), then read the remote.

 COPY-READY PROMPT for the headless export (the cloud cannot run it; the laptop owns running it):
   You are exporting read-only data for a tax ledger. NOBODY CAN ANSWER QUESTIONS in this run, so
   never ask for permission or clarification; if something is unavailable, write what you have and
   state the limitation in your last line. Producing nothing is the worst outcome.
   Use only these read-only tools: get_accounts, get_equity_orders, get_pnl_trade_history,
   get_equity_positions. Use ONLY the account with agentic_allowed. Do not read any other account.
   1. get_equity_orders, state=filled, created_at_gte 2026-05-01. Do not assume one page: page or
      use date windows, then confirm per held symbol, until the count stops growing. As soon as you
      have them, write {"data": {"orders": [...]}} to <OUT>/orders.json, full order objects with
      their executions, unmodified.
   2. get_pnl_trade_history, span=all. Write {"data": {"trades": [...]}} to <OUT>/pnl.json.
   3. get_equity_positions and compare the held symbols against the orders; name any held symbol
      with no buy in the orders file.
   Last line: ORDERS=<n> PNL=<n> HELD_WITHOUT_BUY=<list or none> LIMITATIONS=<none or what>.

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


def unwrap(obj, key):
    """The list under `key`, from whichever shape the export came back in.

    The tool returns {"data": {key: [...]}}, but an agent told to "save the output" tends to
    write the inner object {key: [...]} or just the bare list. All three are the same data, so
    accept all three rather than die with KeyError: 'data' on the first run that differs."""
    if isinstance(obj, dict):
        if isinstance(obj.get("data"), dict) and key in obj["data"]:
            obj = obj["data"][key]
        elif key in obj:
            obj = obj[key]
        else:
            raise ValueError("no %r list in the file (top-level keys: %s)" % (key, sorted(obj)[:6]))
    if not isinstance(obj, list):
        raise ValueError("%r is not a list" % key)
    return obj


def export_problem(orders, trades):
    """Why this export cannot be trusted, or None. Checked BEFORE building, and a refusal writes
    nothing: an empty export would otherwise produce state ok with zero sales, a false all-clear
    that is indistinguishable from an account that has never sold."""
    if not orders:
        return "the orders export is empty"
    if len(trades) > len(orders):
        return ("%d broker sale rows but only %d orders: the orders export is truncated"
                % (len(trades), len(orders)))
    return None


def build(orders, trades, status=None, rh_log="rh_trade_log.jsonl"):
    # Any order that carries executions, WHATEVER its final state. A sell cancelled after a
    # partial fill still sold shares and is still taxable; filtering on state == "filled" would
    # drop it and silently understate the lots sold. An order with no executions never traded.
    filled = [o for o in orders if o.get("executions")]
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
    def _load(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    try:
        orders = unwrap(_load(a.orders), "orders")
        trades = unwrap(_load(a.pnl), "trades")
        status = _load(a.status) if os.path.exists(a.status) else None
    except (OSError, ValueError) as e:        # JSONDecodeError is a ValueError
        print("REFUSING to build, nothing written: %s" % e)
        return 2
    print("export check: %d orders (%d carry executions), %d broker sale rows"
          % (len(orders), sum(1 for o in orders if o.get("executions")), len(trades)))
    why = export_problem(orders, trades)
    if why:
        print("REFUSING to build, nothing written: %s" % why)
        return 2
    led = build(orders, trades, status, a.rh_log)
    new = json.dumps(led, indent=1, sort_keys=True)
    old = ""
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            old = fh.read()
    if new != old:                       # deterministic: only changes when the data does
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(new)
    s = led["summary"]
    print(R.report_line("Arm B", s, watch=led["wash_watch"]))
    print("state=%s  %s" % (s["state"], s.get("reason", "")))
    print("verification:", json.dumps(s["verification"]))
    return 0 if s["state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
