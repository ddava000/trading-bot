"""
rh_bot.py — Robinhood laptop bot: the DECISION ENGINE.

IDENTICAL strategy to the cloud bot. Every threshold, rail and helper is
IMPORTED from alpaca_bot, never copied, so the two cannot drift: change a stop
in the cloud bot, `git pull` on the laptop, and this inherits it.

THE LLM NEVER DECIDES A TRADE. This module is pure: account state in, an
explicit order list out. The laptop daemon (rh_daemon.py) runs it on the cloud
bot's exact cadence and only spends tokens when there is an order to place.

  IN  : state JSON  (cash / unsettled / positions / holds ledger / last buys)
  OUT : {"orders":[...], "notes":[...], "snapshot":{...}}

Market data comes from Yahoo (keyless; a home IP dodges the 429s that block
cloud runners). If real Alpaca keys are present the news tripwire also runs;
without them it fails open and is simply skipped.

The ONE forced difference from the cloud bot: no crypto sleeve. Robinhood's
agentic API is equities + options only today ("more assets soon"), so the 5%
crypto slice sits in cash instead. Options are never used.

Usage:  python rh_bot.py < state.json > orders.json
        python rh_bot.py --fast < state.json      (exits only, for 60s passes)
        python rh_bot.py --selftest
"""

import os, sys, json
from datetime import datetime

os.environ.setdefault("ALPACA_API_KEY", "unused-on-laptop")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-on-laptop")
import alpaca_bot as bot   # THE strategy: signals, rails, screeners, tripwire

MIN_ORDER = 1.00   # Robinhood fractional minimum (the cloud bot's $5 floor exists
                   # to dodge Alpaca whole-share rejections; RH fractions everything)


def _quote(sym, meme):
    """(live, signal, closes) from Yahoo. Nones when data is unusable."""
    closes, vols = bot.yf_ohlcv(sym)
    if not closes or len(closes) < 35:
        return None, None, None
    live = bot.yf_live(sym) or closes[-1]
    return live, bot.compute_signals(sym, closes, vols, live, meme), closes


def decide(state, fast=False):
    """Pure: account state -> order list. `fast` = exits only (the 60s pass)."""
    cash      = float(state.get("cash") or 0)
    unsettled = float(state.get("unsettled") or 0)
    settled   = max(0.0, cash - unsettled)          # T+1: today's proceeds are locked
    positions = {p["symbol"]: p for p in state.get("positions") or []}
    holds     = dict(state.get("holds") or {})       # {sym: {basis, peak}} — the hold ledger
    last_buy  = dict(state.get("last_buy") or {})
    today     = datetime.now().strftime("%Y-%m-%d")
    orders, notes = [], []

    meme = [] if fast else (bot.fetch_wsb() or [])
    held = {}
    for sym, p in positions.items():
        live, sig, closes = _quote(sym, meme)
        if live:
            held[sym] = {"live": live, "sig": sig, "closes": closes,
                         "qty": float(p.get("qty") or 0),
                         "cost": float(p.get("avg_cost") or 0)}
        else:
            notes.append(f"{sym}: no market data — untouched this pass")

    invested = sum(h["live"] * h["qty"] for h in held.values())
    equity   = cash + invested
    index_val = sum(held[s]["live"] * held[s]["qty"] for s in bot.INDEX_ETFS if s in held)
    hold_val  = sum(held[s]["live"] * held[s]["qty"] for s in holds if s in held)
    trade_val = max(0.0, invested - hold_val - index_val)

    # News tripwire (needs real Alpaca keys; fails open to {} without them).
    watch   = set(held) - set(bot.INDEX_ETFS)
    bad_now = bot.news_flags(watch, bot.NEWS_ALERT_MIN) if watch else {}

    def _sell(sym, qty, why):
        orders.append({"action": "sell", "symbol": sym, "qty": qty, "reason": why})

    # ── EXITS (identical rails; always run, even on a halt/fast pass) ─────────
    sold = set()
    for sym, h in held.items():
        if sym in bot.INDEX_ETFS or h["qty"] <= 0 or h["cost"] <= 0:
            continue                                  # index core is buy-and-hold
        live, cost = h["live"], h["cost"]
        con = (h["sig"] or {}).get("consensus", 0)
        pct, why = (live / cost - 1) * 100, None
        if sym in holds:                              # HOLD sleeve: basis stop + peak ratchet
            basis = float(holds[sym].get("basis") or cost) or cost
            peak  = max(float(holds[sym].get("peak") or 0), live)
            floor_ = max(basis * bot.HOLD_STOP, peak * bot.HOLD_TRAIL)
            if live <= floor_:
                why = (f"HOLD-STOP {pct:+.1f}% (floor ${floor_:.2f}, "
                       f"{'basis -25%' if floor_ == basis * bot.HOLD_STOP else f'gave back from peak ${peak:.2f}'})")
        else:                                         # TRADE sleeve: brackets + signal + time stop
            age = None
            if last_buy.get(sym):
                try:
                    age = (datetime.now() - datetime.strptime(last_buy[sym][:10], "%Y-%m-%d")).days
                except Exception:
                    age = None
            if live <= cost * bot.STOP_LOSS_PCT:
                why = f"STOP-LOSS {pct:+.1f}%"
            elif live >= cost * bot.TAKE_PROFIT_PCT and con <= 0:
                why = f"TAKE-PROFIT {pct:+.1f}%"
            elif con == -1:
                why = f"SELL signal RSI {h['sig']['rsi']:.0f} ({pct:+.1f}%)"
            elif age is not None and age >= bot.TIME_STOP_DAYS and con <= 0 and live < cost * 1.02:
                why = f"TIME-STOP {pct:+.1f}% after {age}d"
        if not why and sym in bad_now and sym not in holds:
            why = f"NEWS-EXIT ({bad_now[sym][:70]})"   # holds are alerted, never auto-sold
        if why:
            _sell(sym, h["qty"], why); sold.add(sym)
    for sym in (s for s in bad_now if s in holds and s not in sold):
        notes.append(f"ALERT {sym} (HOLD): danger headline — {bad_now[sym][:90]}")

    if fast:
        return {"orders": orders, "notes": notes, "fast": True,
                "snapshot": {"equity": round(equity, 2), "held": sorted(held)}}

    budget = min(settled * 0.95, settled)             # settled cash only

    # ── INDEX CORE: equal-weight SPY/QQQ/IWM toward 50%, funded FIRST ─────────
    per_tgt = equity * bot.INDEX_CORE_PCT / len(bot.INDEX_ETFS)
    for etf in bot.INDEX_ETFS:
        h = held.get(etf)
        val = (h["live"] * h["qty"]) if h else 0.0
        if val > per_tgt * 1.25 and h:                # overweight -> trim back to target
            _sell(etf, round((val - per_tgt) / h["live"], 6), f"INDEX-TRIM to ${per_tgt:.2f}")
        elif val < per_tgt - equity * 0.01:           # underweight -> buy toward target
            amt = min(per_tgt - val, budget)
            if amt >= MIN_ORDER:
                orders.append({"action": "buy", "symbol": etf, "notional": round(amt, 2),
                               "reason": f"INDEX-CORE toward ${per_tgt:.2f}"})
                budget -= amt

    # ── ACTIVE ENTRIES: same sleeve routing, theme cap, guards ───────────────
    if budget >= MIN_ORDER:
        universe, seen = [], set(held) | sold | set(bot.INDEX_ETFS)
        for s in ((bot.fetch_day_gainers() or []) + (bot.fetch_smallcaps() or [])
                  + meme + (bot.fetch_screener() or [])):
            if s and s.isalpha() and s not in seen:
                seen.add(s); universe.append(s)
            if len(universe) >= 25:
                break

        block = bot.news_flags(set(universe), bot.NEWS_BLOCK_MIN) if universe else {}
        cands = []
        for sym in universe:
            live, sig, closes = _quote(sym, meme)
            if not live or not sig or sig["consensus"] != 1:      continue
            if sig["rsi"] > bot.RSI_ENTRY_MAX:                    continue
            if sym in block:
                notes.append(f"{sym}: blocked — danger news"); continue
            cands.append((sig["buys"], sym, live, sig, closes))
        cands.sort(key=lambda r: -r[0])

        # Theme cap: identical rule to the cloud bot — correlation is measured
        # against the HOLD book (multi-day risk), not short-lived trade positions.
        # Each name is already capped at MAX_POS_PCT, so 2 per theme = the same
        # ~20%-of-equity theme exposure the cloud bot allows.
        book = {s: held[s]["closes"] for s in holds
                if s in held and s not in sold and held[s].get("closes")}
        for _b, sym, live, sig, closes in cands:
            if budget < MIN_ORDER:
                break
            peers = [s for s, cl in book.items() if cl and bot._pair_corr(closes, cl) > bot.CORR_MAX]
            if len(peers) >= bot.HOLD_CLUSTER_MAX:
                notes.append(f"{sym}: skipped — moves with {sorted(peers)} (theme at max)"); continue
            if bot.earnings_within(sym):
                notes.append(f"{sym}: skipped — earnings within {bot.EARNINGS_BLOCK_D}d"); continue
            strong  = (sig["buys"] >= 4 and sig["trend"] == "up"
                       and sig["rsi"] <= bot.HOLD_RSI_MAX)
            sleeve  = "HOLD" if strong else "TRADE"
            room    = (equity * bot.HOLD_PCT - hold_val) if strong else \
                      (equity * bot.MAX_INVESTED_PCT - trade_val)
            amount  = min(equity * bot.MAX_POS_PCT, room, budget)
            if amount < MIN_ORDER:
                continue
            orders.append({"action": "buy", "symbol": sym, "notional": round(amount, 2),
                           "sleeve": sleeve,
                           "reason": f"{sleeve} +1 RSI {sig['rsi']:.0f} {sig['trend']} ({sig['buys']} votes)"})
            budget -= amount
            if strong:
                book[sym] = closes        # joins the hold book -> counts toward its theme
                hold_val  += amount
            else:
                trade_val += amount

    return {"orders": orders, "notes": notes, "fast": False,
            "snapshot": {"equity": round(equity, 2), "settled": round(settled, 2),
                         "index": round(index_val, 2), "hold": round(hold_val, 2),
                         "trade": round(trade_val, 2), "held": sorted(held)}}


def _selftest():
    import random
    random.seed(11)
    flat  = [10.0 + random.gauss(0, 0.05) for _ in range(60)]
    theme = [random.gauss(0, 0.02) for _ in range(60)]

    def series(load):
        px = [50.0]
        for i in range(59):
            px.append(px[-1] * (1 + load * theme[i] + random.gauss(0, 0.004)))
        return px
    oil_a, oil_b, other = series(1.0), series(1.0), series(0.0)

    q = {}
    globals()["_quote"] = lambda sym, meme: q.get(sym, (None, None, None))
    bot.fetch_wsb = bot.fetch_day_gainers = bot.fetch_smallcaps = bot.fetch_screener = lambda: []
    bot.news_flags = lambda syms, mins: {}
    bot.earnings_within = lambda sym, days=2: False

    # exits: trade stop / take-profit / hold ratchet / untouched
    q.update({"F": (10.0, {"consensus": -1, "rsi": 40, "trend": "down", "buys": 0}, flat),
              "T": (11.5, {"consensus":  0, "rsi": 55, "trend": "up",   "buys": 2}, flat),
              "H": ( 6.0, {"consensus":  1, "rsi": 55, "trend": "up",   "buys": 4}, flat),
              "X": ( 9.0, {"consensus":  0, "rsi": 50, "trend": "up",   "buys": 2}, flat)})
    out = decide({"cash": 0.0, "positions": [
        {"symbol": "F", "qty": 2, "avg_cost": 12.0},    # -16.7% -> stop
        {"symbol": "T", "qty": 1, "avg_cost": 10.0},    # +15%   -> take-profit
        {"symbol": "H", "qty": 1, "avg_cost": 10.0},    # hold, -40% -> basis stop
        {"symbol": "X", "qty": 1, "avg_cost":  9.0}],
        "holds": {"H": {"basis": 10.0, "peak": 12.0}}}, fast=True)
    kinds = {o["symbol"]: o["reason"].split()[0] for o in out["orders"]}
    assert kinds.get("F") == "STOP-LOSS" and kinds.get("T") == "TAKE-PROFIT", kinds
    assert kinds.get("H") == "HOLD-STOP" and "X" not in kinds, kinds
    print("exits: trade stop, take-profit, hold ratchet, untouched  OK")

    # fast pass never buys
    assert not [o for o in out["orders"] if o["action"] == "buy"]
    print("fast pass: exits only, zero buys  OK")

    # T+1: unsettled cash buys nothing
    out = decide({"cash": 50.0, "unsettled": 50.0, "positions": []})
    assert not [o for o in out["orders"] if o["action"] == "buy"], out["orders"]
    print("settlement guard: unsettled cash buys nothing  OK")

    # index core funded first, equal weight
    out = decide({"cash": 60.0, "unsettled": 0.0, "positions": []})
    idx = {o["symbol"]: o["notional"] for o in out["orders"] if o.get("reason", "").startswith("INDEX")}
    assert set(idx) == set(bot.INDEX_ETFS), idx
    assert all(abs(v - 10.0) < 0.5 for v in idx.values()), idx   # 50% of $60 / 3
    print(f"index core: {idx} funded first  OK")

    # theme cap: 3 correlated hold-grade names -> the THIRD is blocked (cloud rule),
    # while an uncorrelated name still gets in.
    strong = {"consensus": 1, "rsi": 55, "trend": "up", "buys": 4}   # 4 votes -> HOLD sleeve
    oil_c = series(1.0)
    q.update({"OILA": (10.0, strong, oil_a), "OILB": (10.0, strong, oil_b),
              "OILC": (10.0, strong, oil_c), "INDY": (10.0, strong, other)})
    bot.fetch_day_gainers = lambda: ["OILA", "OILB", "OILC", "INDY"]
    out = decide({"cash": 60.0, "unsettled": 0.0, "positions": []})
    bought = [o["symbol"] for o in out["orders"]
              if o["action"] == "buy" and o["symbol"] not in bot.INDEX_ETFS]
    assert "OILA" in bought and "OILB" in bought, bought
    assert "OILC" not in bought, f"theme cap failed, 3rd correlated name got in: {bought}"
    assert any("OILC" in n and "theme" in n for n in out["notes"]), out["notes"]
    assert "INDY" in bought, f"uncorrelated name wrongly blocked: {bought}"
    print(f"theme cap: bought {bought} — 3rd correlated name blocked, independent allowed  OK")
    print("SELFTEST PASS")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        json.dump(decide(json.load(sys.stdin), fast="--fast" in sys.argv),
                  sys.stdout, indent=1)
        sys.stdout.write("\n")
