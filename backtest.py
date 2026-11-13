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
# MICRO/MEME sleeve universe — the "slot machine" the live bot actually trades and
# the blue-chip UNIVERSE above cannot represent. Deliberately includes names that
# collapsed or went to zero (EV/SPAC/meme busts), because the whole question is
# whether the occasional 10-bagger pays for the wipeouts.
#
# SURVIVORSHIP WARNING, and the reason this list is written out in full: Yahoo
# returns NOTHING for fully delisted/bankrupt tickers, so every name that died
# hardest silently drops out of the test. The run prints how many vanished, which
# is a live measurement of the bias — the surviving-only result is an OPTIMISTIC
# upper bound on the micro sleeve, never a fair estimate.
MICRO_UNIVERSE = ["MULN","BBIG","ATER","PROG","GNUS","SNDL","EXPR","WISH","CLOV",
                  "SPCE","RIDE","WKHS","GOEV","ARVL","FFIE","NKLA","HYZN","XELA",
                  "BBBY","AMC","GME","KOSS","NAKD","CENN","IDEX","SOS","ZOM",
                  "OCGN","SENS","CTRM","TOPS","SHIP","MARK","JAGX","ENZC"]

START=10_000.0; WINDOW=90; RANGE=os.environ.get("BT_RANGE","5y")
MAX_POS,STOP,TP,RSI_MAX = bot.MAX_POS_PCT,bot.STOP_LOSS_PCT,bot.TAKE_PROFIT_PCT,bot.RSI_ENTRY_MAX
HSTOP,HRSI = bot.HOLD_STOP,bot.HOLD_RSI_MAX
HOLD_CAP,TRADE_CAP = bot.HOLD_PCT,bot.MAX_INVESTED_PCT
MICRO_POS,SMALL_POS = bot.MICRO_POS_PCT,bot.SMALLCAP_POS_PCT
MICRO_PX,SMALL_PX   = bot.MICRO_PX,bot.SMALL_PX

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

# Micro sleeve data, kept in the SAME structures but tagged so sizing/routing can
# tell them apart. Names returning no history are almost all delisted/bankrupt —
# counted and reported, because their absence is the survivorship bias itself.
MICRO=set(); micro_dead=[]
for s in MICRO_UNIVERSE:
    d=fetch(s)
    if len(d)>60: data[s]=d; MICRO.add(s)
    else:         micro_dead.append(s)

series={s:sorted(d.items()) for s,d in data.items()}
idx={s:{dt:i for i,(dt,_) in enumerate(ser)} for s,ser in series.items()}
print(f"  {len(data)-len(MICRO)} blue-chip + {len(MICRO)} micro names, {len(cal)} days ({cal[0]} -> {cal[-1]})")
print(f"  MICRO SURVIVORSHIP: {len(micro_dead)}/{len(MICRO_UNIVERSE)} micro tickers returned NO data "
      f"(delisted/bankrupt) and are therefore ABSENT from the sim: {', '.join(micro_dead) or 'none'}")
print( "  => micro results below EXCLUDE the worst outcomes and are an OPTIMISTIC upper bound.")

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

def simulate(mode, htrail, exit_rule="ratchet", hstop=HSTOP, tstop=STOP):
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
                pg=p["peak"]/cost-1                                      # peak gain so far
                if exit_rule=="scaleout":
                    osh=p.get("osh",p["sh"]); g=live/cost-1
                    if p.get("tier",0)<1 and g>=0.50 and p["sh"]>1e-9:   # +50%: trim 1/3 of original
                        ss=min(osh/3,p["sh"]); cash+=ss*live; p["sh"]-=ss; p["tier"]=1
                    if p.get("tier",0)<2 and g>=1.00 and p["sh"]>1e-9:   # +100%: trim another 1/3
                        ss=min(osh/3,p["sh"]); cash+=ss*live; p["sh"]-=ss; p["tier"]=2
                    if p["sh"]<=1e-9: del pos[s]; continue
                    if live<=max(cost*hstop,p["peak"]*htrail): reason=1  # rest rides the ratchet
                elif exit_rule=="accel":                                 # tighten trail only for big winners
                    ht=htrail if pg<0.5 else (0.70 if pg<1.0 else 0.78)
                    if live<=max(cost*hstop,p["peak"]*ht): reason=1
                else:
                    if live<=max(cost*hstop,p["peak"]*htrail): reason=1
            else:
                if live<=cost*tstop or (live>=cost*TP and con<=0) or con==-1: reason=1
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
            sh=room/price[s]; cash-=sh*price[s]; pos[s]={"sh":sh,"cost":price[s],"sleeve":sleeve,"peak":price[s],"osh":sh,"tier":0}
            inv+=room
            if sleeve=="hold": inv_h+=room
    return curve

yrs=(datetime.strptime(cal[-1],"%Y-%m-%d")-datetime.strptime(cal[55],"%Y-%m-%d")).days/365.25
def stats(curve):
    r=curve[-1]/START-1; cg=(1+r)**(1/yrs)-1
    pk=-1e9; dd=0
    for e in curve: pk=max(pk,e); dd=min(dd,e/pk-1)
    return r,cg,dd
def line(nm,cv):
    r,cg,dd=stats(cv); print(f"  {nm:<36} total {r*100:>+7.1f}%  CAGR {cg*100:>+6.1f}%  maxDD {dd*100:>+7.1f}%  ret/risk {cg/abs(dd):.2f}")
print("\n"+"="*90)
print(f"ACTIVE-STOP TIGHTNESS TEST: does cutting active-sleeve losers FASTER help?  |  {cal[55]} -> {cal[-1]} ({yrs:.1f}y)")
print("  Live: trade sleeve hard-stops at -7% (0.93); hold sleeve at -25% basis (0.75).")
print("  Motivation (2026-07-30): on up-days the active picks sometimes bleed while the market rises.")
print("  Sweep the TRADE hard stop tighter; last row also tightens the HOLD basis stop.")
print("  HONEST LIMIT: daily bars + ZERO slippage FLATTER tight stops badly — a real -3% stop gets")
print("  whipsawed out by intraday noise this can't see, then pays slippage to re-enter. Read tight-stop")
print("  rows as an OPTIMISTIC upper bound; reality is worse. No microcaps/crypto.")
print("="*90)
# (name, tstop=trade hard stop, hstop=hold basis stop)
STOPS=[("current      (-7% trade / -25% hold)", 0.93, 0.75),
       ("trade -5%    (-5% trade / -25% hold)", 0.95, 0.75),
       ("trade -4%    (-4% trade / -25% hold)", 0.96, 0.75),
       ("trade -3%    (-3% trade / -25% hold)", 0.97, 0.75),
       ("both tight   (-5% trade / -15% hold)", 0.95, 0.85)]
print("-- ACTIVE strategy (mirrors the live bot: trade + hold sleeves) --")
for nm,ts,hs in STOPS: line(nm, simulate("active",0.60,"ratchet",hs,ts))
line("SPY buy & hold", [START*bench[D][0]/bench[cal[55]][0] for D in cal])

print("\n"+"="*90)
print(f"SLEEVE-SPLIT TEST: shift weight from ACTIVE (trade+hold) into the INDEX core  |  {cal[55]} -> {cal[-1]} ({yrs:.1f}y)")
print("  Live split: INDEX 50% / TRADE 15% / HOLD 25% / CRYPTO 5% / cash ~5%.")
print("  This harness excludes crypto (see file docstring); crypto's 5% + the cash buffer are folded into")
print("  a fixed ~10% idle residual in every row below, so only INDEX vs ACTIVE moves. Trade:hold keeps")
print("  today's 15:25 ratio as ACTIVE shrinks. Stops/brackets unchanged from live at every split.")
print("  Index sleeve = equal-weight SPY/QQQ/IWM bought once on day 55 and held flat (no >25%-over-target")
print("  trim modeled) — optimistic, so index-heavy rows here are a slight upper bound on the live design.")
print("="*90)
IDX_ETFS = ["SPY","QQQ","IWM"]
idx_series = {"SPY": bench, "QQQ": fetch("QQQ"), "IWM": fetch("IWM")}

def simulate_split(index_pct, active_pct, micro_pct=0.0, hold_pct=None, trade_pct=None):
    """index_pct = held SPY/QQQ/IWM core. active_pct = trade+hold, split on the live
    15:25 ratio unless hold_pct/trade_pct are given explicitly. micro_pct = the
    slot-machine sleeve, drawn ONLY from MICRO names and sized like the live bot
    (2.5% per name under $2, 5% under $15), exiting on the same -7%/+15% brackets."""
    hold_cap  = hold_pct  if hold_pct  is not None else active_pct * (HOLD_CAP/(HOLD_CAP+TRADE_CAP))
    trade_cap = trade_pct if trade_pct is not None else active_pct * (TRADE_CAP/(HOLD_CAP+TRADE_CAP))
    cash=START; pos={}; curve=[]; idx_sh={}
    for day,D in enumerate(cal):
        if day<55: curve.append(START); continue
        if day==55 and index_pct>0:
            budget=START*index_pct/len(IDX_ETFS)
            for s in IDX_ETFS:
                px=idx_series[s].get(D)
                if px: idx_sh[s]=budget/px[0]; cash-=budget
        price={s:data[s][D][0] for s in data if D in data[s]}
        idx_val=sum(idx_sh.get(s,0)*idx_series[s][D][0] for s in IDX_ETFS if D in idx_series[s])
        equity=cash+sum(pos[s]["sh"]*price[s] for s in pos if s in price)+idx_val
        curve.append(equity)
        sig=day_sig[D]
        for s in list(pos):
            if s not in price: continue
            live=price[s]; p=pos[s]; cost=p["cost"]; con=sig.get(s,{}).get("consensus",0); reason=None
            if p["sleeve"]=="hold":
                p["peak"]=max(p["peak"],live)
                if live<=max(cost*HSTOP,p["peak"]*0.60): reason=1
            else:
                if live<=cost*STOP or (live>=cost*TP and con<=0) or con==-1: reason=1
            if reason: cash+=p["sh"]*live; del pos[s]
        inv=sum(pos[s]["sh"]*price[s] for s in pos if s in price)
        inv_h=sum(pos[s]["sh"]*price[s] for s in pos if pos[s]["sleeve"]=="hold" and s in price)
        inv_m=sum(pos[s]["sh"]*price[s] for s in pos if pos[s]["sleeve"]=="micro" and s in price)
        for s,r in sig.items():
            if s in pos or r["consensus"]!=1 or r["rsi"]>RSI_MAX: continue
            if s in MICRO:
                # Slot machine: micro names never enter hold/trade, only their own
                # sleeve, at the live bot's reduced per-name size.
                if micro_pct<=0 or inv_m>=equity*micro_pct: continue
                px=price[s]
                cap = MICRO_POS if px<MICRO_PX else (SMALL_POS if px<SMALL_PX else MAX_POS)
                sleeve,room="micro",min(equity*cap,equity*micro_pct-inv_m,cash*0.98)
            else:
                strong=r["buys"]>=4 and r["trend"]=="up" and r["rsi"]<=HRSI
                if strong and inv_h<equity*hold_cap: sleeve,room="hold",min(equity*MAX_POS,equity*hold_cap-inv_h,cash*0.98)
                elif inv-inv_h-inv_m<equity*trade_cap: sleeve,room="trade",min(equity*MAX_POS,equity*trade_cap-(inv-inv_h-inv_m),cash*0.98)
                else: continue
            if room<equity*0.005: continue      # micro bets are small by design
            sh=room/price[s]; cash-=sh*price[s]; pos[s]={"sh":sh,"cost":price[s],"sleeve":sleeve,"peak":price[s]}
            inv+=room
            if   sleeve=="hold":  inv_h+=room
            elif sleeve=="micro": inv_m+=room
    return curve

# Every row deploys the SAME ~90% (the live 5% crypto sleeve is outside this
# harness), so differences are pure allocation, not more/less money at work.
# idx, hold, trade, micro
SPLITS=[("all-active   ( 0 idx / 35 tr / 55 hold)", 0.00, 0.55, 0.35, 0.00),
        ("current live (50 idx / 15 tr / 25 hold)", 0.50, 0.25, 0.15, 0.00),
        ("current+slots(50 idx / 13 tr / 22 hold / 5 mic)", 0.50, 0.22, 0.13, 0.05),
        ("PROPOSED     (75 idx /  5 tr /  5 hold / 5 mic)", 0.75, 0.05, 0.05, 0.05),
        ("index+slots  (85 idx /  0 tr /  0 hold / 5 mic)", 0.85, 0.00, 0.00, 0.05),
        ("all-index    (90 idx / no active at all)", 0.90, 0.00, 0.00, 0.00)]
for nm,ip,hp,tp,mp in SPLITS:
    line(nm, simulate_split(ip, hp+tp, micro_pct=mp, hold_pct=hp, trade_pct=tp))
