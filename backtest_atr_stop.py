"""
Backtest harness -- research only, NOT the live bot (respects the freeze).
Explores whether an ATR (volatility-scaled) stop beats the TRADING sleeve's
current static -7% stop (STOP_LOSS_PCT). Motivated by the 2026-07-13 weekly
audit: practitioner sources call static tight stops in headline-driven
choppy markets a common whipsaw source, and ATR-scaled stops "the
professional standard." This tests that claim on the bot's own signal
engine before it's ever considered for the live bot.

THREE stop variants on the TRADING sleeve only (hold sleeve untouched --
that's backtest.py's territory):
  static     current live behavior: fixed -7% from cost (STOP_LOSS_PCT)
  atr_fixed  cost - k*ATR(14) measured ONCE at entry, never moves
  atr_trail  peak - k*ATR(14) recomputed daily (a "Chandelier" trailing
             stop) -- widens in high vol, tightens in low vol, and locks
             in gains as a position runs, unlike atr_fixed
Take-profit (+15%, signal-gated) is unchanged in every variant -- this is
a stop-only test.

Same honest limits as backtest.py: 40 liquid names (no live screener picks,
no microcaps/crypto -- the actual whipsaw incidents were in microcaps/news
events this harness can't reach), daily bars, fills at close, ZERO
slippage, checked once/day (the live bot checks every 15 min intraday).
One data point, not a verdict.
"""
import os, requests
from datetime import datetime
os.environ.setdefault("ALPACA_API_KEY", "x"); os.environ.setdefault("ALPACA_SECRET_KEY", "x")
import alpaca_bot as bot

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "MU",
            "INTC", "QCOM", "ORCL", "CRM", "NFLX", "DIS", "BAC", "JPM", "XOM", "CVX",
            "PFE", "NKE", "SBUX", "UBER", "PLTR", "SOFI", "COIN", "AMC", "AAL", "CCL",
            "F", "RIVN", "SNAP", "ROKU", "MARA", "RIOT", "DKNG", "HOOD", "PLUG", "NIO"]
START = 10_000.0; WINDOW = 90; RANGE = os.environ.get("BT_RANGE", "5y")
MAX_POS, STOP, TP, RSI_MAX = bot.MAX_POS_PCT, bot.STOP_LOSS_PCT, bot.TAKE_PROFIT_PCT, bot.RSI_ENTRY_MAX
HSTOP, HRSI, HOLD_TRAIL = bot.HOLD_STOP, bot.HOLD_RSI_MAX, bot.HOLD_TRAIL
HOLD_CAP, TRADE_CAP = bot.HOLD_PCT, bot.MAX_INVESTED_PCT
ATR_N = 14   # standard ATR lookback

def fetch(sym):
    try:
        d = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={RANGE}",
                          headers=bot.YF_HEADERS, timeout=15).json()
        res = d["chart"]["result"][0]; ts = res["timestamp"]; q = res["indicators"]["quote"][0]
        return {datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"): (q["close"][i], q["volume"][i] or 0,
                                                                     q["high"][i], q["low"][i])
                for i, t in enumerate(ts) if q["close"][i] and q["high"][i] and q["low"][i]}
    except Exception:
        return {}

print(f"Fetching {RANGE} bars (with high/low for ATR)...")
data = {s: d for s in UNIVERSE for d in [fetch(s)] if len(d) > 60}
bench = fetch("SPY"); cal = sorted(bench)
series = {s: sorted(d.items()) for s, d in data.items()}
idx = {s: {dt: i for i, (dt, _) in enumerate(ser)} for s, ser in series.items()}
print(f"  {len(data)} names, {len(cal)} days ({cal[0]} -> {cal[-1]})")

# Precompute signals (identical to backtest.py -- same signal engine, same window)
day_sig = {}
for day, D in enumerate(cal):
    s = {}
    if day >= 55:
        for sym in data:
            i = idx[sym].get(D)
            if i is None or i < 40: continue
            w = series[sym][max(0, i - WINDOW + 1):i + 1]
            rr = bot.compute_signals(sym, [c for _, (c, v, h, l) in w], [v for _, (c, v, h, l) in w],
                                      w[-1][1][0], [])
            if rr: s[sym] = rr
    day_sig[D] = s

# Precompute ATR(14) per symbol per day (Wilder-style simple rolling average of True Range)
def atr_series(ser):
    trs, out, prev_close = [], {}, None
    for date, (c, v, h, l) in ser:
        tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        if len(trs) >= ATR_N:
            out[date] = sum(trs[-ATR_N:]) / ATR_N
        prev_close = c
    return out

atr_data = {s: atr_series(ser) for s, ser in series.items()}

def simulate(trade_exit, atr_mult=2.0):
    """trade_exit: 'static' | 'atr_fixed' | 'atr_trail'. HOLD sleeve logic
    (ratchet, unaffected by this test) matches the live bot exactly."""
    cash = START; pos = {}; curve = []
    for day, D in enumerate(cal):
        if day < 55: curve.append(START); continue
        price = {s: data[s][D][0] for s in data if D in data[s]}
        equity = cash + sum(pos[s]["sh"] * price[s] for s in pos if s in price); curve.append(equity)
        sig = day_sig[D]
        for s in list(pos):
            if s not in price: continue
            live = price[s]; p = pos[s]; cost = p["cost"]; con = sig.get(s, {}).get("consensus", 0); reason = None
            if p["sleeve"] == "hold":
                p["peak"] = max(p["peak"], live)
                if live <= max(cost * HSTOP, p["peak"] * HOLD_TRAIL): reason = 1
            else:
                stop_hit = False
                if trade_exit == "static":
                    stop_hit = live <= cost * STOP
                elif trade_exit == "atr_fixed":
                    a0 = p.get("atr_entry")
                    stop_hit = (live <= cost - atr_mult * a0) if a0 else live <= cost * STOP
                elif trade_exit == "atr_trail":
                    p["peak"] = max(p.get("peak", cost), live)
                    a_now = atr_data.get(s, {}).get(D)
                    stop_hit = (live <= p["peak"] - atr_mult * a_now) if a_now else live <= cost * STOP
                if stop_hit or (live >= cost * TP and con <= 0) or con == -1: reason = 1
            if reason: cash += p["sh"] * live; del pos[s]
        inv = sum(pos[s]["sh"] * price[s] for s in pos if s in price)
        inv_h = sum(pos[s]["sh"] * price[s] for s in pos if pos[s]["sleeve"] == "hold" and s in price)
        for s, r in sig.items():
            if s in pos or r["consensus"] != 1 or r["rsi"] > RSI_MAX: continue
            strong = r["buys"] >= 4 and r["trend"] == "up" and r["rsi"] <= HRSI
            if strong and inv_h < equity * HOLD_CAP:
                sleeve, room = "hold", min(equity * MAX_POS, equity * HOLD_CAP - inv_h, cash * 0.98)
            elif inv - inv_h < equity * TRADE_CAP:
                sleeve, room = "trade", min(equity * MAX_POS, equity * TRADE_CAP - (inv - inv_h), cash * 0.98)
            else:
                continue
            if room < equity * 0.01: continue
            sh = room / price[s]; cash -= sh * price[s]
            pos[s] = {"sh": sh, "cost": price[s], "sleeve": sleeve, "peak": price[s],
                      "atr_entry": atr_data.get(s, {}).get(D)}
            inv += room
            if sleeve == "hold": inv_h += room
    return curve

yrs = (datetime.strptime(cal[-1], "%Y-%m-%d") - datetime.strptime(cal[55], "%Y-%m-%d")).days / 365.25
def stats(curve):
    r = curve[-1] / START - 1; cg = (1 + r) ** (1 / yrs) - 1
    pk = -1e9; dd = 0
    for e in curve: pk = max(pk, e); dd = min(dd, e / pk - 1)
    return r, cg, dd

def count_trade_stops(trade_exit, atr_mult=2.0):
    """How often the TRADING-sleeve stop actually fires under each rule (whipsaw proxy)."""
    cash = START; pos = {}; stops = 0; recovered_10d = 0
    for day, D in enumerate(cal):
        if day < 55: continue
        price = {s: data[s][D][0] for s in data if D in data[s]}
        sig = day_sig[D]
        for s in list(pos):
            if s not in price: continue
            live = price[s]; p = pos[s]; cost = p["cost"]; con = sig.get(s, {}).get("consensus", 0)
            if p["sleeve"] == "hold":
                p["peak"] = max(p["peak"], live)
                if live <= max(cost * HSTOP, p["peak"] * HOLD_TRAIL): del pos[s]
                continue
            stop_hit = False
            if trade_exit == "static": stop_hit = live <= cost * STOP
            elif trade_exit == "atr_fixed":
                a0 = p.get("atr_entry"); stop_hit = (live <= cost - atr_mult * a0) if a0 else live <= cost * STOP
            elif trade_exit == "atr_trail":
                p["peak"] = max(p.get("peak", cost), live)
                a_now = atr_data.get(s, {}).get(D)
                stop_hit = (live <= p["peak"] - atr_mult * a_now) if a_now else live <= cost * STOP
            if stop_hit or (live >= cost * TP and con <= 0) or con == -1:
                if stop_hit:
                    stops += 1
                    i = idx[s].get(D)
                    if i is not None and i + 10 < len(series[s]):
                        exit_px = live; later_px = series[s][i + 10][1][0]
                        if later_px > exit_px * 1.03: recovered_10d += 1
                del pos[s]
        inv = sum(pos[s]["sh"] * price[s] for s in pos if s in price) if pos else 0
        equity = cash + inv
        inv_h = sum(pos[s]["sh"] * price[s] for s in pos if pos[s]["sleeve"] == "hold" and s in price)
        for s, r in sig.items():
            if s in pos or r["consensus"] != 1 or r["rsi"] > RSI_MAX: continue
            strong = r["buys"] >= 4 and r["trend"] == "up" and r["rsi"] <= HRSI
            if strong and inv_h < equity * HOLD_CAP:
                sleeve, room = "hold", min(equity * MAX_POS, equity * HOLD_CAP - inv_h, cash * 0.98)
            elif inv - inv_h < equity * TRADE_CAP:
                sleeve, room = "trade", min(equity * MAX_POS, equity * TRADE_CAP - (inv - inv_h), cash * 0.98)
            else:
                continue
            if room < equity * 0.01: continue
            sh = room / price[s]; cash -= sh * price[s]
            pos[s] = {"sh": sh, "cost": price[s], "sleeve": sleeve, "peak": price[s],
                      "atr_entry": atr_data.get(s, {}).get(D)}
            inv += room
            if sleeve == "hold": inv_h += room
    return stops, recovered_10d

def line(nm, cv):
    r, cg, dd = stats(cv)
    print(f"  {nm:<40} total {r*100:>+7.1f}%  CAGR {cg*100:>+6.1f}%  maxDD {dd*100:>+7.1f}%  ret/risk {cg/abs(dd):.2f}")

print("\n" + "=" * 92)
print(f"TRADING-SLEEVE STOP TEST: static -7% vs ATR-scaled  |  {cal[55]} -> {cal[-1]} ({yrs:.1f}y)")
print("  NOTE: 40 liquid large/mid-caps, daily bars, no slippage. Real whipsaw incidents")
print("  (microcaps, single-day headline shocks) live outside what this harness can see.")
print("=" * 92)
VARIANTS = [("current static -7% (live bot)", "static", 2.0),
            ("ATR(14) x2.0 fixed-at-entry",   "atr_fixed", 2.0),
            ("ATR(14) x3.0 fixed-at-entry",   "atr_fixed", 3.0),
            ("ATR(14) x2.0 trailing (chandelier)", "atr_trail", 2.0),
            ("ATR(14) x3.0 trailing (chandelier)", "atr_trail", 3.0)]
for nm, rule, mult in VARIANTS:
    line(nm, simulate(rule, mult))

print("\nStop-fire frequency + whipsaw proxy (stop fired, then price >3% above exit within 10 trading days):")
for nm, rule, mult in VARIANTS:
    stops, recovered = count_trade_stops(rule, mult)
    pct = (recovered / stops * 100) if stops else 0.0
    print(f"  {nm:<40} stops fired: {stops:>4}   recovered-within-10d: {recovered:>4} ({pct:.0f}%)")
