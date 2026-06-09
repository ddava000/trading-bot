"""
Weekly performance review for the Alpaca bot.
Pulls account performance from Alpaca, scores it against SPY (the honest
benchmark), summarizes the week's trades + their entry context, and emails it.

Reuses config/helpers from alpaca_bot.py. Run weekly via alpaca-review.yml.
"""

import os, json
from datetime import datetime, timezone, timedelta
import alpaca_bot as bot   # config, send_email, alpaca_get, yf_ohlcv, MODE


def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d


def main():
    et = datetime.now(timezone.utc) - timedelta(hours=4)

    # ── Account + weekly equity change ────────────────────────────────────────
    acct   = bot.alpaca_get("/v2/account")
    equity = _f(acct.get("equity"))
    cash   = _f(acct.get("cash"))

    try:
        hist = bot.alpaca_get("/v2/account/portfolio/history?period=1W&timeframe=1D")
        eqs  = [e for e in (hist.get("equity") or []) if e]
    except Exception:
        eqs = []
    wk_start = eqs[0] if eqs else equity
    wk_chg   = equity - wk_start
    wk_pct   = (wk_chg / wk_start * 100) if wk_start else 0.0

    # ── SPY benchmark (≈1 week = 5 trading days) ──────────────────────────────
    spy_c, _ = bot.yf_ohlcv("SPY")
    spy_pct  = ((spy_c[-1] - spy_c[-6]) / spy_c[-6] * 100) if spy_c and len(spy_c) >= 6 else None

    # ── This week's filled orders ─────────────────────────────────────────────
    after = (et - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    try:
        orders = bot.alpaca_get(f"/v2/orders?status=closed&after={after}&limit=500&direction=asc")
        orders = orders if isinstance(orders, list) else []
    except Exception:
        orders = []
    filled = [o for o in orders if o.get("filled_at")]
    nbuy   = sum(1 for o in filled if o.get("side") == "buy")
    nsell  = sum(1 for o in filled if o.get("side") == "sell")

    # ── Open positions ────────────────────────────────────────────────────────
    try:
        positions = bot.alpaca_get("/v2/positions")
        positions = positions if isinstance(positions, list) else []
    except Exception:
        positions = []

    # ── Logged entry context (the "why") ──────────────────────────────────────
    log = []
    if os.path.exists("trade_log.jsonl"):
        for line in open("trade_log.jsonl"):
            try: log.append(json.loads(line))
            except Exception: pass
    wk_log    = [t for t in log if t.get("ts", "") >= (et - timedelta(days=7)).strftime("%Y-%m-%d")]
    log_buys  = [t for t in wk_log if t.get("side") == "buy"]
    meme_buys = [t for t in log_buys if t.get("meme")]
    buy_rsis  = [t["rsi"] for t in log_buys if "rsi" in t]

    # ── Build report ──────────────────────────────────────────────────────────
    L = [f"📊 WEEKLY REVIEW — Alpaca bot ({bot.MODE})", f"Week ending {et.strftime('%Y-%m-%d')}", ""]
    L.append(f"Equity: ${equity:,.2f}   (cash ${cash:,.2f})")
    L.append(f"This week: {wk_chg:+,.2f}  ({wk_pct:+.2f}%)")
    if spy_pct is not None:
        diff = wk_pct - spy_pct
        L.append(f"SPY this week: {spy_pct:+.2f}%")
        L.append(f"→ Bot {'BEAT' if diff >= 0 else 'TRAILED'} SPY by {abs(diff):.2f} points.")
    L.append("")
    L.append(f"Trades this week: {len(filled)} filled ({nbuy} buys, {nsell} sells)")

    if positions:
        L.append(""); L.append("Open positions (unrealized):")
        for p in sorted(positions, key=lambda x: _f(x.get("unrealized_pl")), reverse=True):
            L.append(f"  {p.get('symbol'):6} ${_f(p.get('market_value')):>9,.2f}   "
                     f"{_f(p.get('unrealized_pl')):+7.2f}  ({_f(p.get('unrealized_plpc'))*100:+.1f}%)")

    if wk_log:
        L.append(""); L.append("Entry context (logged trades):")
        L.append(f"  logged buys: {len(log_buys)}  ·  meme-driven: {len(meme_buys)}")
        if buy_rsis:
            L.append(f"  avg buy RSI: {sum(buy_rsis)/len(buy_rsis):.1f}  "
                     f"(min {min(buy_rsis):.0f} / max {max(buy_rsis):.0f})")
        L.append("  → As more trades accumulate, this section will show which setups")
        L.append("    (RSI band, trend, meme vs not) actually produced winners.")

    if bot.MODE == "PAPER":
        L.append(""); L.append("(Paper account — Alpaca starts at $100k, so sizes look large; "
                                "scales down when live.)")

    report = "\n".join(L)
    print(report)
    bot.send_email(f"📊 Weekly Review — Alpaca bot ({bot.MODE}) — {et.strftime('%b %d')}", report)


if __name__ == "__main__":
    main()
