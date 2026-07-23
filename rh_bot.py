"""
rh_bot.py — Robinhood laptop bot: the DECISION ENGINE.

Same signal engine as the cloud Alpaca bot (imports compute_signals directly —
one strategy, not a fork), sized for a small speculative CASH account.

THE LLM NEVER DECIDES A TRADE. The scheduled agent on the laptop only moves data:
  IN  : account state JSON  (cash / positions / today's sale proceeds) — the agent
        reads this from the Robinhood MCP and pipes it in
  OUT : an explicit order list JSON — the agent places exactly these orders
        through the Robinhood MCP, then commits the log

Market data comes straight from Yahoo (keyless — and a home IP doesn't catch the
429s that block cloud runners), so the laptop needs NO API keys at all.

Usage:  python rh_bot.py < state.json > orders.json
        python rh_bot.py --selftest

State schema (what the agent pipes in):
  {"cash": 59.92,
   "unsettled": 0.0,                       # today's stock-sale proceeds (T+1)
   "positions": [{"symbol": "F", "qty": 3, "avg_cost": 12.10}, ...]}

Order schema (what the agent places, verbatim):
  [{"action": "sell", "symbol": "F", "qty": 3,        "reason": "STOP-LOSS -7.2%"},
   {"action": "buy",  "symbol": "SOFI", "notional": 13.0, "reason": "signal +1 RSI 54"}]
"""

import os, sys, json

# Dummy creds so the import never demands Alpaca keys on the laptop — we only use
# this module's math and its keyless Yahoo helpers. (Same pattern as backtest.py.)
os.environ.setdefault("ALPACA_API_KEY", "unused-on-laptop")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-on-laptop")
import alpaca_bot as bot   # compute_signals, yf_ohlcv, yf_live, screeners

# ── Risk rails, sized for a ~$60 speculative cash account ─────────────────────
# This is the SLOT MACHINE sleeve: no index core (that's Alpaca's job), pure
# active. Same signal engine and same bracket discipline as the cloud bot.
MAX_POSITIONS   = 4       # 4 names ≈ $13 each at $60 — diversified enough to not be one bet
POS_PCT         = 0.22    # max 22% of equity per name
MIN_ORDER       = 5.00    # Robinhood fractional minimum is $1; $5 keeps orders meaningful
CASH_FLOOR_PCT  = 0.05    # never deploy the last 5%
STOP_LOSS_PCT   = 0.93    # -7% hard stop (identical to the cloud bot's trade sleeve)
TAKE_PROFIT_PCT = 1.15    # +15% take-profit unless the signal still says buy
RSI_ENTRY_MAX   = 78.0    # no blow-off-top chasing
MAX_CANDIDATES  = 25      # cap the Yahoo pulls per run (laptop runs a few times/day)
CORR_MAX        = 0.60    # above this two names are ONE trade (cloud bot's calibration)
MAX_PER_THEME   = 1       # ...and only ONE per theme. The cloud bot allows 2 of ~6 holds;
                          # with just 4 slots that would be half the book on one bet. The
                          # first dry run wanted WTI+KOS (both oil) — exactly the pile-up
                          # that sank cloud week 1, so every slot here is its own bet.


def _price_and_signal(sym, meme):
    """(live, signal, closes) for one symbol from Yahoo. Nones if data is unusable."""
    closes, vols = bot.yf_ohlcv(sym)
    if not closes or len(closes) < 35:
        return None, None, None
    live = bot.yf_live(sym) or closes[-1]
    return live, bot.compute_signals(sym, closes, vols, live, meme), closes


def decide(state):
    """Pure function: account state -> order list. No network writes, no side effects."""
    cash      = float(state.get("cash") or 0)
    unsettled = float(state.get("unsettled") or 0)
    settled   = max(0.0, cash - unsettled)        # T+1: can't spend today's sale proceeds
    positions = {p["symbol"]: p for p in state.get("positions") or []}
    orders, notes = [], []

    # Price every held name first — needed for equity, exits, and sizing.
    meme = bot.fetch_wsb() or []
    held = {}
    for sym, p in positions.items():
        live, sig, closes = _price_and_signal(sym, meme)
        if live:
            held[sym] = {"live": live, "sig": sig, "closes": closes,
                         "qty": float(p.get("qty") or 0),
                         "cost": float(p.get("avg_cost") or 0)}
        else:
            notes.append(f"{sym}: no market data — left alone this run")

    equity = settled + unsettled + sum(h["live"] * h["qty"] for h in held.values())

    # ── EXITS (always run, even when buying is blocked) ───────────────────────
    sold = set()
    for sym, h in held.items():
        live, cost, con = h["live"], h["cost"], (h["sig"] or {}).get("consensus", 0)
        if h["qty"] <= 0 or cost <= 0:
            continue
        pct, why = (live / cost - 1) * 100, None
        if live <= cost * STOP_LOSS_PCT:
            why = f"STOP-LOSS {pct:+.1f}%"
        elif live >= cost * TAKE_PROFIT_PCT and con <= 0:
            why = f"TAKE-PROFIT {pct:+.1f}%"
        elif con == -1:
            why = f"SELL signal (RSI {h['sig']['rsi']:.0f}, {pct:+.1f}%)"
        if why:
            orders.append({"action": "sell", "symbol": sym, "qty": h["qty"], "reason": why})
            sold.add(sym)

    # ── ENTRIES ───────────────────────────────────────────────────────────────
    room = MAX_POSITIONS - len([s for s in held if s not in sold])
    budget = min(settled * 0.95, max(0.0, settled - equity * CASH_FLOOR_PCT))
    if room > 0 and budget >= MIN_ORDER:
        universe, seen = [], set(held) | sold
        for s in (bot.fetch_day_gainers() or []) + (bot.fetch_smallcaps() or []) + meme:
            if s and s not in seen and s.isalpha():
                seen.add(s); universe.append(s)
            if len(universe) >= MAX_CANDIDATES:
                break

        scored = []
        for sym in universe:
            live, sig, closes = _price_and_signal(sym, meme)
            if not live or not sig or sig["consensus"] != 1:
                continue
            if sig["rsi"] > RSI_ENTRY_MAX:
                continue
            if live > equity * POS_PCT:      # a single share would blow the per-name cap
                continue
            scored.append((sig["buys"], sym, live, sig, closes))
        scored.sort(key=lambda r: -r[0])     # strongest consensus first

        # Theme cap: a candidate that moves with names we already own (or already
        # picked this run) counts toward that theme; max MAX_PER_THEME per theme.
        book = {s: h["closes"] for s, h in held.items() if s not in sold and h.get("closes")}
        picked = 0
        for _buys, sym, live, sig, closes in scored:
            if picked >= room or budget < MIN_ORDER:
                break
            peers = [s for s, cl in book.items()
                     if cl and bot._pair_corr(closes, cl) > CORR_MAX]
            if len(peers) >= MAX_PER_THEME:
                notes.append(f"{sym}: skipped — moves with {sorted(peers)} (theme at max)")
                continue
            amount = min(equity * POS_PCT, budget)
            orders.append({"action": "buy", "symbol": sym, "notional": round(amount, 2),
                           "reason": f"signal +1 RSI {sig['rsi']:.0f} {sig['trend']} ({sig['buys']} votes)"})
            budget -= amount
            book[sym] = closes            # counts toward its theme for the rest of this run
            picked += 1

    return {"orders": orders, "notes": notes,
            "snapshot": {"equity": round(equity, 2), "settled": round(settled, 2),
                         "unsettled": round(unsettled, 2), "held": sorted(held)}}


def _selftest():
    """Offline check of the decision rules — no network, synthetic prices."""
    import random
    random.seed(11)
    flat = [10.0 + random.gauss(0, 0.05) for _ in range(60)]          # uncorrelated filler
    theme = [random.gauss(0, 0.02) for _ in range(60)]                # a shared factor

    def series(load):
        px = [50.0]
        for i in range(59):
            px.append(px[-1] * (1 + load * theme[i] + random.gauss(0, 0.004)))
        return px
    oil_a, oil_b, other = series(1.0), series(1.0), series(0.0)

    calls = {"F": (10.00, {"consensus": -1, "rsi": 40, "trend": "down", "buys": 0}, flat),
             "T": (11.50, {"consensus":  0, "rsi": 55, "trend": "up",   "buys": 2}, flat),
             "X": ( 9.00, {"consensus":  0, "rsi": 50, "trend": "up",   "buys": 2}, flat)}
    bot.fetch_wsb = lambda: []
    bot.fetch_day_gainers = lambda: []
    bot.fetch_smallcaps = lambda: []
    globals()["_price_and_signal"] = lambda sym, meme: calls.get(sym, (None, None, None))

    out = decide({"cash": 20.0, "unsettled": 0.0, "positions": [
        {"symbol": "F", "qty": 2, "avg_cost": 12.00},   # -16.7% -> stop
        {"symbol": "T", "qty": 1, "avg_cost": 10.00},   # +15%   -> take-profit
        {"symbol": "X", "qty": 1, "avg_cost":  9.00}]}) # flat   -> hold
    kinds = {o["symbol"]: o["reason"].split()[0] for o in out["orders"]}
    assert kinds.get("F") == "STOP-LOSS",   kinds
    assert kinds.get("T") == "TAKE-PROFIT", kinds
    assert "X" not in kinds,                kinds
    print("exits: F stop-loss, T take-profit, X untouched  OK")

    # T+1 guard: all cash unsettled -> zero buys
    out = decide({"cash": 50.0, "unsettled": 50.0, "positions": []})
    assert not [o for o in out["orders"] if o["action"] == "buy"], out
    print("settlement guard: unsettled cash buys nothing  OK")

    # Theme cap: two correlated candidates -> only the first gets bought
    buy_sig = {"consensus": 1, "rsi": 55, "trend": "up", "buys": 4}
    calls.clear()
    calls.update({"OILA": (10.0, buy_sig, oil_a), "OILB": (10.0, buy_sig, oil_b),
                  "INDY": (10.0, buy_sig, other)})
    bot.fetch_day_gainers = lambda: ["OILA", "OILB", "INDY"]
    out = decide({"cash": 60.0, "unsettled": 0.0, "positions": []})
    bought = [o["symbol"] for o in out["orders"] if o["action"] == "buy"]
    assert "OILA" in bought and "INDY" in bought, bought
    assert "OILB" not in bought, f"theme cap failed, bought both oil names: {bought}"
    print(f"theme cap: bought {bought}, blocked the second correlated name  OK")
    print("SELFTEST PASS")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        json.dump(decide(json.load(sys.stdin)), sys.stdout, indent=1)
        sys.stdout.write("\n")
