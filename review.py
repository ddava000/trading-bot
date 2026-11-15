"""
Weekly performance review for the Alpaca bot.
Pulls account performance from Alpaca, scores it against SPY (the honest
benchmark), summarizes the week's trades + their entry context, and emails it.

Reuses config/helpers from alpaca_bot.py. Run weekly via alpaca-review.yml.
"""

import os, json
from datetime import datetime, timedelta
import alpaca_bot as bot   # config, send_email, alpaca_get, yf_ohlcv, MODE


def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d


# Margin (percentage points) below which the bot-vs-SPY gap is treated as a tie rather than a
# decisive BEAT/TRAILED. LAPTOP BOT (2026-11-09) measured a 0.43-point spread between two
# defensible readings of the SAME week's Arm A return (the two legs are not anchored to identical
# windows: the Arm A leg is Alpaca's trailing 1W daily history, the SPY leg a fixed 5 closes). A
# margin inside that spread is decided by the measurement choice, not by performance, so we must
# not print it as a decisive result. Widening the window alignment itself is deferred to Devon
# (needs the live portfolio-history reply, which the audit runner has no keys to read).
PERF_TIE_BAND_PTS = 0.50


def _perf_lines(equity, eqs, spy_pct):
    """Build the weekly equity-vs-SPY block. Returns (lines, wk_pct_or_None).

    FAILS CLOSED. With no usable equity history we do NOT fabricate wk_start=equity (which made
    wk_pct exactly 0.00% and still printed "Bot BEAT SPY by <spy>%": a failed Alpaca fetch shipped
    a flattering fake BEAT with no caveat - LAPTOP BOT 2026-11-09, review.py fail-open). Instead the
    comparison is suppressed and the report says the history was unavailable.

    The Arm A leg runs eqs[0] -> eqs[-1] (close-to-close, like the SPY leg), NOT eqs[0] -> live
    intraday equity; review runs after Friday's close so eqs[-1] is that close. The session span of
    each leg is printed so a window mismatch is visible rather than silent.
    """
    lines = []
    if eqs:
        wk_start = eqs[0]
        wk_end   = eqs[-1]
        wk_chg   = wk_end - wk_start
        wk_pct   = (wk_chg / wk_start * 100) if wk_start else None
    else:
        wk_pct = None

    if wk_pct is None:
        lines.append("This week: equity history unavailable "
                     "(Alpaca portfolio-history returned no usable points); "
                     "week-vs-SPY comparison suppressed.")
    else:
        lines.append(f"This week: {wk_chg:+,.2f}  ({wk_pct:+.2f}%)   "
                     f"[Arm A: {max(len(eqs) - 1, 0)} sessions, close-to-close]")

    if spy_pct is not None:
        lines.append(f"SPY this week: {spy_pct:+.2f}%   [5 sessions, close-to-close]")
        if wk_pct is not None:
            diff = wk_pct - spy_pct
            if abs(diff) < PERF_TIE_BAND_PTS:
                lines.append(f"-> Bot roughly matched SPY (gap {diff:+.2f} pts, within the "
                             f"{PERF_TIE_BAND_PTS:.2f}-pt measurement spread; not a decisive result).")
            else:
                lines.append(f"-> Bot {'BEAT' if diff >= 0 else 'TRAILED'} SPY by {abs(diff):.2f} points.")
        else:
            lines.append("-> No bot-vs-SPY result: equity history was unavailable this run.")

    return lines, wk_pct


def main():
    et = datetime.now(bot.ET_TZ)   # DST-correct ET (hardcoded UTC-4 breaks every November)

    # ── Account + weekly equity change ────────────────────────────────────────
    acct   = bot.alpaca_get("/v2/account")
    equity = _f(acct.get("equity"))
    cash   = _f(acct.get("cash"))

    try:
        hist = bot.alpaca_get("/v2/account/portfolio/history?period=1W&timeframe=1D")
        eqs  = [e for e in (hist.get("equity") or []) if e]
    except Exception:
        eqs = []

    # ── SPY benchmark (≈1 week = 5 trading days) ──────────────────────────────
    spy_c, _ = bot.yf_ohlcv("SPY")
    spy_pct  = ((spy_c[-1] - spy_c[-6]) / spy_c[-6] * 100) if spy_c and len(spy_c) >= 6 else None

    perf_lines, _wk_pct = _perf_lines(equity, eqs, spy_pct)

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
        with open("trade_log.jsonl", encoding="utf-8") as _lf:
            for line in _lf:
                try: log.append(json.loads(line))
                except Exception: pass
    wk_log    = [t for t in log if t.get("ts", "") >= (et - timedelta(days=7)).strftime("%Y-%m-%d")]
    log_buys  = [t for t in wk_log if t.get("side") == "buy"]
    meme_buys = [t for t in log_buys if t.get("meme")]
    buy_rsis  = [t["rsi"] for t in log_buys if "rsi" in t]

    # ── Build report ──────────────────────────────────────────────────────────
    L = [f"WEEKLY REVIEW - Alpaca bot ({bot.MODE})", f"Week ending {et.strftime('%Y-%m-%d')}", ""]
    L.append(f"Equity: ${equity:,.2f}   (cash ${cash:,.2f})")
    L += perf_lines
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

    # REALIZED gain/loss for tax tracking (Devon, 2026-09-29). The equity change above
    # includes unrealized paper P&L; only positions actually SOLD are taxable. Both arms,
    # one shared reader so every report prints the same numbers.
    try:
        import realized as _R
        L.append("")
        L.append("REALIZED GAIN/LOSS (sold positions only, unlike the equity change above):")
        L += ["  " + ln for ln in _R.repo_report_lines(a_block=bot.realized_for_run())]
    except Exception as _e:
        L.append("")
        L.append("Realized gain/loss totals UNAVAILABLE (%s)" % _e)

    if bot.MODE == "PAPER":
        L.append(""); L.append("(Paper account — validation run, not real money.)")

    report = "\n".join(L)
    print(report)
    bot.send_email(f"Weekly Review - Alpaca bot ({bot.MODE}) - {et.strftime('%b %d')}", report)


if __name__ == "__main__":
    main()
