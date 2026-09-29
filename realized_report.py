#!/usr/bin/env python3
"""On-demand Arm A realized gain/loss report. READ-ONLY: places no orders, writes no files.

Runs on a GitHub runner because only GitHub holds the Alpaca keys. It uses the SAME code
the bot runs every cycle (alpaca_bot.alpaca_realized_block), so what this prints is what
status.json and the reports carry, and a disagreement between them would be a bug.

Before it prints a total it tries to falsify the machinery, because this repo has been
fooled repeatedly by checks that could only ever say "fine":
  1. PAGINATION: the full history is fetched twice with different page sizes. The two id
     sets must be identical, or the pager is dropping or repeating activities.
  2. POSITIVE CONTROL: the event-type inventory must contain the account's original cash
     deposit. A check never seen to report "present" proves nothing when it says "absent".
  3. POSITIONS: quantities implied by the fills must equal the broker's positions.
  4. ACCOUNTING IDENTITY: cash + market value - contributions - income must equal FIFO
     realized + (market value - open-lot cost). This is the one that would catch a wrong
     price or a wrong closed lot, which share counts cannot.
"""
import os
import sys

os.environ.setdefault("ALPACA_API_KEY", "missing")
os.environ.setdefault("ALPACA_SECRET_KEY", "missing")
import alpaca_bot as bot          # noqa: E402
import realized as R              # noqa: E402


def main():
    print("=== REALIZED REPORT, Arm A (%s) ===" % bot.MODE)
    failures = []

    a = bot.alpaca_activity_history(page_size=100)
    b = bot.alpaca_activity_history(page_size=7)
    if a is None or b is None:
        print("FAIL: could not fetch the full activity history")
        return 1
    ida, idb = [x["id"] for x in a], [x["id"] for x in b]
    ok = sorted(ida) == sorted(idb) and len(set(ida)) == len(ida)
    print("pagination check     : %d activities (page 100) vs %d (page 7), identical: %s"
          % (len(a), len(b), ok))
    if not ok:
        failures.append("pagination")

    types = {}
    for x in a:
        t = str(x.get("activity_type"))
        types[t] = types.get(t, 0) + 1
    print("activity types found : %s" % ", ".join("%s x%d" % kv for kv in sorted(types.items())))
    print("positive control     : deposit type CSD present %s (must be >= 1)" % types.get("CSD", 0))
    if not types.get("CSD"):
        failures.append("cannot see the known cash deposit")
    hits = {t: n for t, n in types.items() if t in bot.BREAKS_FIFO_SET}
    print("split/transfer check : %s" % (
        "NONE of %s present" % ",".join(sorted(bot.BREAKS_FIFO_SET)) if not hits else
        "FOUND %s" % hits))

    block, ledger = bot.alpaca_realized_block()
    if ledger is None:
        print("FAIL: %s" % block.get("reason"))
        return 1

    res = R.fifo(bot.fills_from(a))
    live = bot.alpaca_positions_total() or {}
    bad = R.verify_positions(res["open"], live)
    print("positions            : %d symbol(s) from fills vs %d at the broker, mismatches: %d"
          % (len(res["open"]), len(live), len(bad)))
    for m in bad:
        print("   MISMATCH %(symbol)s fills=%(from_fills)s broker=%(broker)s" % m)

    resid, det = bot.realized_identity(a, res)
    if resid is None:
        print("accounting identity  : COULD NOT COMPUTE (%s)" % det)
        failures.append("identity")
    else:
        print("accounting identity  : residual %.4f dollars (tolerance %.2f)" % (resid, bot.IDENTITY_TOL))
        print("   cash %(cash).2f + market value %(market_value).2f - contributions %(contributions).2f"
              " - income/fees %(other_income_fees).2f" % det)
        print("   = realized %(realized_raw).4f + (market value %(market_value).2f"
              " - open cost %(open_cost).2f)" % det)
        if abs(resid) > bot.IDENTITY_TOL:
            failures.append("identity residual too large")

    print()
    print("CLOSED LOTS (FIFO), oldest first")
    print("  %-10s %-7s %10s %10s %10s %9s %s" % ("sold", "symbol", "qty", "basis", "proceeds", "gain", "term/opened"))
    for l in sorted(ledger["closed"], key=lambda x: (x["close_date"], x["symbol"], x["open_date"])):
        print("  %-10s %-7s %10.4f %10.2f %10.2f %9.2f %s %s"
              % (l["close_date"], l["symbol"], l["qty"], l["basis"], l["proceeds"],
                 l["gain"], l["term"], l["open_date"]))

    print()
    print("STILL HELD (open lots by FIFO): %s" % (
        ", ".join("%s %.4f" % (s, q) for s, q in sorted(res["open"].items())) or "none"))
    print()
    print("SUMMARY   state=%s" % block["state"])
    if block.get("reason"):
        print("  reason: %s" % block["reason"])
    print("  " + R.report_line("Arm A", block))
    print("  history: %s to %s, %d fills, last sale %s"
          % (block.get("since"), (block.get("last_fill") or "")[:10], block.get("fills", 0),
             block.get("last_sale")))

    w = ledger["wash_watch"]
    print()
    print("WASH-SALE WATCH (upper bound, same account only here): %d loss sale(s) flagged" % len(w))
    for x in w:
        print("  %s sold %s at %.2f loss, %.4f sh replaced within 30d (%d buys): up to %.2f"
              % (x["symbol"], x["sale_date"], x["loss"], x["replacement_qty"],
                 x["replacement_buys"], x["upper_bound"]))
    print()
    print("Not a tax document. The broker 1099-B is authoritative.")

    if failures:
        print()
        print("SELF-CHECK FAILURES: %s. Do not trust the totals above." % ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
