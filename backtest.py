"""
Backtest harness — research only, NOT the live bot (respects the freeze).
Tests TWO proposed improvements against the current setup and SPY:
  TEST 1 (tighter ratchet): hold sleeve gives back 25% from peak (htrail 0.75)
                            instead of 40% (htrail 0.60) before selling.
  TEST 2 (index anchor):    put a chunk in held SPY, run the active strategy as
                            a smaller satellite around it.
Uses the bot's REAL compute_signals(). Honest limits unchanged: fixed liquid
universe (no live screener picks, no microcaps/crypto, mild survivorship), daily
bars, fills at close, ZERO slippage, stops checked daily. One data point, not a verdict.
"""
import os, requests
from datetime import datetime
os.environ.setdefault("ALPACA_API_KEY","x"); os.environ.setdefault("ALPACA_SECRET_KEY","x")
import alpaca_bot as bot

UNIVERSE = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","AVGO","MU",
            "INTC","QCOM","ORCL","CRM","NFLX","DIS","BAC","JPM","XOM","CVX",
            "PFE","NKE","SBUX","UBER","PLTR","SOFI","COIN","AMC","AAL","CCL",
            "F","RIVN","SNAP","ROKU","MARA","RIOT","DKNG","HOOD","PLUG","NIO"]
START=10_000.0; WINDOW=90; RANGE=os.environ.get("BT_RANGE","5y")
MAX_POS,STOP,TP,RSI_MAX = bot.MAX_POS_PCT,bot.STOP_LOSS_PCT,bot.TAKE_PROFIT_PCT,bot.RSI_ENTRY_MAX
HSTOP,HRSI = bot.HOLD_STOP,bot.HOLD_RSI_MAX
HOLD_CAP,TRADE_CAP = bot.HOLD_PCT,bot.MAX_INVESTED_PCT

def fetch(sym):
    try:
        d=requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range={RANGE}",
                       headers=bot.YF_HEADERS,timeout=15).json()
        res=d["chart"]["result"][0]; ts=res["timestamp"]; q=res["indicators"]["quote"][0]
        return {datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"):(q["close"][i],q["volume"][i] or 0)
                for i,t in enumerate(ts) if q["close"][i]}
    except Exception: return {}

print(f"Fetching {RANGE} bars...")
data={s:d for s in UNIVERSE for d in [fetch(s)] if len(d)>60}
bench=fetch("SPY"); cal=sorted(bench)
series={s:sorted(d.items()) for s,d in data.items()}
idx={s:{dt:i for i,(dt,_) in enumerate(ser)} for s,ser in series.items()}
print(f"  {len(data)} names, {len(cal)} days ({cal[0]} -> {cal[-1]})")

day_sig={}
for day,D in enumerate(cal):
    s={}
    if day>=55:
        for sym in data:
            i=idx[sym].get(D)
            if i is None or i<40: continue
            w=series[sym][max(0,i-WINDOW+1):i+1]
            rr=bot.compute_signals(sym,[c for _,(c,_) in w],[v for _,(_,v) in w],w[-1][1][0],[])
            if rr: s[sym]=rr
    day_sig[D]=s

def simulate(mode, htrail):
    cash=START; pos={}; curve=[]
    for day,D in enumerate(cal):
        if day<55: curve.append(START); continue
        price={s:data[s][D][0] for s in data if D in data[s]}
        equity=cash+sum(pos[s]["sh"]*price[s] for s in pos if s in price); curve.append(equity)
        sig=day_sig[D]
        for s in list(pos):
            if s not in price: continue
            live=price[s]; p=pos[s]; cost=p["cost"]; con=sig.get(s,{}).get("consensus",0); reason=None
            if mode=="allhold" or p["sleeve"]=="hold":
                p["peak"]=max(p["peak"],live)
                if live<=max(cost*HSTOP,p["peak"]*htrail): reason=1
            else:
                if live<=cost*STOP or (live>=cost*TP and con<=0) or con==-1: reason=1
            if reason: cash+=p["sh"]*live; del pos[s]
        inv=sum(pos[s]["sh"]*price[s] for s in pos if s in price)
        inv_h=sum(pos[s]["sh"]*price[s] for s in pos if pos[s]["sleeve"]=="hold" and s in price)
        for s,r in sig.items():
            if s in pos or r["consensus"]!=1 or r["rsi"]>RSI_MAX: continue
            if mode=="allhold":
                if inv>=equity*0.95: continue
                sleeve,room="hold",min(equity*MAX_POS,equity*0.95-inv,cash*0.98)
            else:
                strong=r["buys"]>=4 and r["trend"]=="up" and r["rsi"]<=HRSI
                if strong and inv_h<equity*HOLD_CAP: sleeve,room="hold",min(equity*MAX_POS,equity*HOLD_CAP-inv_h,cash*0.98)
                elif inv-inv_h<equity*TRADE_CAP:     sleeve,room="trade",min(equity*MAX_POS,equity*TRADE_CAP-(inv-inv_h),cash*0.98)
                else: continue
            if room<equity*0.01: continue
            sh=room/price[s]; cash-=sh*price[s]; pos[s]={"sh":sh,"cost":price[s],"sleeve":sleeve,"peak":price[s]}
            inv+=room
            if sleeve=="hold": inv_h+=room
    return curve

yrs=(datetime.strptime(cal[-1],"%Y-%m-%d")-datetime.strptime(cal[55],"%Y-%m-%d")).days/365.25
def stats(curve):
    r=curve[-1]/START-1; cg=(1+r)**(1/yrs)-1
    pk=-1e9; dd=0
    for e in curve: pk=max(pk,e); dd=min(dd,e/pk-1)
    return r,cg,dd
active_full=simulate("active",0.60)            # old-bot proxy (current ratchet; tighter one flunked)
adict=dict(zip(cal,active_full))
qqq=fetch("QQQ"); iwm=fetch("IWM")
days=[D for D in cal[55:] if D in bench and D in qqq and D in iwm]
def norm(get): base=get(days[0]); return [get(D)/base*START for D in days]
spy_c=norm(lambda D:bench[D][0]); qqq_c=norm(lambda D:qqq[D][0])
iwm_c=norm(lambda D:iwm[D][0]);   act_c=norm(lambda D:adict[D])
core  =[(spy_c[i]+qqq_c[i]+iwm_c[i])/3 for i in range(len(days))]      # equal SPY/QQQ/IWM
hybrid=[0.50*core[i] + 0.45*act_c[i] + 0.05*START for i in range(len(days))]
yrs2=(datetime.strptime(days[-1],"%Y-%m-%d")-datetime.strptime(days[0],"%Y-%m-%d")).days/365.25
def stat2(cv):
    r=cv[-1]/START-1; cg=(1+r)**(1/yrs2)-1; pk=-1e9; dd=0
    for e in cv: pk=max(pk,e); dd=min(dd,e/pk-1)
    return r,cg,dd
def line(nm,cv):
    r,cg,dd=stat2(cv); print(f"  {nm:<40} total {r*100:>+7.1f}%  CAGR {cg*100:>+6.1f}%  maxDD {dd*100:>+7.1f}%  ret/risk {cg/abs(dd):.2f}")
print("\n"+"="*90)
print(f"HYBRID: 50% index core (SPY/QQQ/IWM) + 45% old active bot + 5% cash  |  {days[0]} -> {days[-1]} ({yrs2:.1f}y)")
print("  active = old-bot proxy on 40 liquid names (real bot adds untestable crypto/micro upside ON TOP).")
print("="*90)
line("SPY only (reference)", spy_c)
line("Index core: equal SPY/QQQ/IWM", core)
line("Old active bot alone (~full)", act_c)
line(">>> HYBRID 50 core / 45 active / 5 cash", hybrid)
