"""
Backtest harness — research only, NOT part of the live bot (respects the config freeze).

Tests the bot's REAL signal engine (imports compute_signals from alpaca_bot) and now
models BOTH sleeves separately so allocation changes (trading vs hold split) actually
show up:
  • TRADING sleeve: buy on +1 consensus, exit on -7% stop / +15% TP / -1 signal.
  • HOLD sleeve:    buy on STRONG signal (>=4 buy votes, uptrend, RSI<=70), KEEP it —
                    exit only at -25% from basis or 40%-from-peak ratchet.
Run with BT_HOLD / BT_TRADE env vars to compare allocations; BT_RANGE for the window.

HONEST SCOPE / LIMITATIONS (unchanged — read before trusting any number):
  • Fixed liquid universe — NOT the bot's live dynamic screener picks. Tests the
    SIGNAL APPROACH on the kind of names the sleeves trade live, minus microcaps
    (survivorship bias) and minus crypto.
  • Daily bars, decide-and-fill at the same close. No commissions, slippage IGNORED
    (optimistic for the meme-y names). Stops checked daily (live bot ~every 15 min),
    so realized drawdowns could differ.
A good backtest here is ONE more data point, not a green light.
"""
import os, statistics, requests
from datetime import datetime
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")
import alpaca_bot as bot                            # tests the REAL signal code

UNIVERSE = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","AVGO","MU",
            "INTC","QCOM","ORCL","CRM","NFLX","DIS","BAC","JPM","XOM","CVX",
            "PFE","NKE","SBUX","UBER","PLTR","SOFI","COIN","AMC","AAL","CCL",
            "F","RIVN","SNAP","ROKU","MARA","RIOT","DKNG","HOOD","PLUG","NIO"]
BENCH = "SPY"
START_CASH = 10_000.0
WINDOW     = 90                                       # trailing bars fed to signals (matches live ~90d fetch)
RANGE      = os.environ.get("BT_RANGE", "5y")
HOLD_CAP   = float(os.environ.get("BT_HOLD",  bot.HOLD_PCT))          # hold-sleeve cap
TRADE_CAP  = float(os.environ.get("BT_TRADE", bot.MAX_INVESTED_PCT))  # trading-sleeve cap
MAX_POS, STOP, TP, RSI_MAX = bot.MAX_POS_PCT, bot.STOP_LOSS_PCT, bot.TAKE_PROFIT_PCT, bot.RSI_ENTRY_MAX
HSTOP, HTRAIL, HRSI = bot.HOLD_STOP, bot.HOLD_TRAIL, bot.HOLD_RSI_MAX

def fetch(sym, rng=RANGE):
    try:
        d = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
                         f"?interval=1d&range={rng}", headers=bot.YF_HEADERS, timeout=15).json()
        res = d["chart"]["result"][0]; ts = res["timestamp"]; q = res["indicators"]["quote"][0]
        out = {}
        for i, t in enumerate(ts):
            c = q["close"][i]; v = q["volume"][i]
            if c: out[datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")] = (c, v or 0)
        return out
    except Exception:
        return {}

print(f"Fetching {RANGE} daily bars  (HOLD_CAP={HOLD_CAP:.0%}  TRADE_CAP={TRADE_CAP:.0%})...")
data = {s: d for s in UNIVERSE for d in [fetch(s)] if len(d) > 60}
bench = fetch(BENCH); cal = sorted(bench)
series = {s: sorted(d.items()) for s, d in data.items()}
idx_by_date = {s: {dt: i for i, (dt, _) in enumerate(ser)} for s, ser in series.items()}
print(f"  {len(data)} symbols, {len(cal)} days ({cal[0]} -> {cal[-1]})")

cash, pos, eq_curve, trades = START_CASH, {}, [], []   # pos[s]={sh,cost,sleeve,peak}
for day, D in enumerate(cal):
    if day < 55:
        eq_curve.append((D, START_CASH)); continue
    price = {s: data[s][D][0] for s in data if D in data[s]}
    equity = cash + sum(pos[s]["sh"] * price[s] for s in pos if s in price)
    eq_curve.append((D, equity))

    sig = {}
    for s in data:
        i = idx_by_date[s].get(D)
        if i is None or i < 40: continue
        w = series[s][max(0, i-WINDOW+1):i+1]
        closes = [c for _, (c, _) in w]; vols = [v for _, (_, v) in w]
        rr = bot.compute_signals(s, closes, vols, closes[-1], [])
        if rr: sig[s] = rr

    # EXITS — sleeve-dependent
    for s in list(pos):
        if s not in price: continue
        live = price[s]; p = pos[s]; cost = p["cost"]; con = sig.get(s, {}).get("consensus", 0)
        reason = None
        if p["sleeve"] == "trade":
            if   live <= cost*STOP:            reason = "stop"
            elif live >= cost*TP and con <= 0: reason = "tp"
            elif con == -1:                    reason = "signal"
        else:  # hold — keep until basis stop or peak ratchet
            p["peak"] = max(p["peak"], live)
            if live <= max(cost*HSTOP, p["peak"]*HTRAIL): reason = "hold_stop"
        if reason:
            cash += p["sh"]*live; trades.append((s, (live/cost-1)*100, reason, p["sleeve"])); del pos[s]

    # BUYS — route STRONG signals to hold, the rest to trading; each capped separately
    inv_hold  = sum(pos[s]["sh"]*price[s] for s in pos if pos[s]["sleeve"]=="hold"  and s in price)
    inv_trade = sum(pos[s]["sh"]*price[s] for s in pos if pos[s]["sleeve"]=="trade" and s in price)
    for s, r in sig.items():
        if s in pos or r["consensus"] != 1 or r["rsi"] > RSI_MAX: continue
        strong = r["buys"] >= 4 and r["trend"] == "up" and r["rsi"] <= HRSI
        if strong and inv_hold < equity*HOLD_CAP:
            sleeve, room = "hold", min(equity*MAX_POS, equity*HOLD_CAP - inv_hold, cash*0.98)
        elif inv_trade < equity*TRADE_CAP:
            sleeve, room = "trade", min(equity*MAX_POS, equity*TRADE_CAP - inv_trade, cash*0.98)
        else:
            continue
        if room < equity*0.01: continue
        sh = room/price[s]; cash -= sh*price[s]
        pos[s] = {"sh": sh, "cost": price[s], "sleeve": sleeve, "peak": price[s]}
        if sleeve == "hold": inv_hold += room
        else:                inv_trade += room

# ---- Results ----
final_eq = eq_curve[-1][1]; strat = final_eq/START_CASH - 1
spy = bench[cal[-1]][0]/bench[cal[55]][0] - 1
yrs = (datetime.strptime(cal[-1],"%Y-%m-%d") - datetime.strptime(cal[55],"%Y-%m-%d")).days/365.25
cagr = lambda r: (1+r)**(1/yrs)-1
def maxdd(curve):
    peak=-1e9; dd=0
    for _,e in curve: peak=max(peak,e); dd=min(dd,e/peak-1)
    return dd
spy_curve=[(D, START_CASH*bench[D][0]/bench[cal[55]][0]) for D in cal[55:] if D in bench]
wins=[t for t in trades if t[1]>0]; losses=[t for t in trades if t[1]<=0]
hold_tr=[t for t in trades if t[3]=="hold"]; trade_tr=[t for t in trades if t[3]=="trade"]

print("\n"+"="*66)
print(f"ALLOCATION: trading {TRADE_CAP:.0%} / hold {HOLD_CAP:.0%}   |   {cal[55]} -> {cal[-1]} ({yrs:.1f}y)")
print("="*66)
print(f"  STRATEGY  total {strat*100:+6.1f}%   CAGR {cagr(strat)*100:+5.1f}%   maxDD {maxdd(eq_curve)*100:6.1f}%")
print(f"  SPY hold  total {spy*100:+6.1f}%   CAGR {cagr(spy)*100:+5.1f}%   maxDD {maxdd(spy_curve)*100:6.1f}%")
print(f"  --> {'BEAT' if strat>spy else 'TRAILED'} SPY by {abs(strat-spy)*100:.1f} pts")
print(f"  trades {len(trades)} | win {len(wins)/max(1,len(trades))*100:.0f}% | "
      f"hold-sleeve {len(hold_tr)} / trade-sleeve {len(trade_tr)}")
if wins:   print(f"  avg win {statistics.mean(t[1] for t in wins):+.1f}%  avg loss {statistics.mean(t[1] for t in losses):+.1f}%")
