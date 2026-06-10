"""
Alpaca Day Trading Bot — GitHub Actions
Full buy+sell meme/screener strategy. Same signal engine as the Robinhood bot,
but account + orders go through Alpaca's OFFICIAL REST API. Fractional market
buys use `notional` (dollar amount) — exactly what Robinhood's API blocked.

Defaults to the PAPER endpoint. To go live, set the ALPACA_BASE_URL secret to
https://api.alpaca.markets (and use live API keys).
"""

import os, math, json, requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
# Daily loss halt = max(10% of equity, $20). Loose on purpose: it only stops the
# day on a genuine crash, so it never kneecaps a small ($250-500) live account on
# normal noise. The real risk controls are the sleeve caps + per-name caps below.
#
# Capital is split into two sleeves (max 70% deployed, 30% cash floor):
#   TRADING sleeve (40%): signal-driven buys the day-trade sell logic manages.
#   HOLD sleeve (30%):    strong-signal names (4+ buy votes AND uptrend) bought to
#                         KEEP — exempt from sell signals; only a -25% disaster stop
#                         (thesis broken) exits them. Compounding lives here, and on
#                         a small live account it also burns zero PDT day trades.
LOSS_CAP_PCT     = 0.10
LOSS_CAP_FLOOR   = 20.00
PDT_LIMIT        = 3
MAX_INVESTED_PCT = 0.40   # TRADING sleeve: cap on signal-traded capital
HOLD_PCT         = 0.30   # HOLD sleeve: buy-and-hold allocation on strong signals
HOLD_STOP        = 0.75   # disaster stop — sell a hold at 75% of basis (-25%)
MAX_POS_PCT      = 0.10   # max 10% of equity in any single name (≥4 names = diversified)
SMALLCAP_POS_PCT = 0.05   # small-caps get HALF size (5% of equity) — higher growth, higher blowup risk
SPEND_CAP_PCT    = 0.25   # deploy at most 25% of cash per run (gradual, not all at once)

ALPACA_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET = os.environ["ALPACA_SECRET_KEY"]
ALPACA_BASE   = os.environ.get("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets"
ALPACA_HDRS   = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
MODE          = "PAPER" if "paper-api" in ALPACA_BASE else "LIVE"

# Email alerts — sent from GitHub's cloud so they arrive even with the PC asleep.
ALERT_TO     = os.environ.get("ALERT_TO",   "devondavasher@gmail.com")  # you receive
GMAIL_USER   = os.environ.get("GMAIL_USER", "devonsdummy@gmail.com")    # bot sends as
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_email(subject, body):
    """Best-effort Gmail alert. Never raises. No-op if GMAIL_APP_PASSWORD unset."""
    if not GMAIL_APP_PW:
        print("  [email skipped — GMAIL_APP_PASSWORD not set]")
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_TO
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls()
            s.login(GMAIL_USER, GMAIL_APP_PW)
            s.sendmail(GMAIL_USER, [ALERT_TO], msg.as_string())
        print(f"  [email sent → {ALERT_TO}: {subject}]")
    except Exception as e:
        print(f"  [email failed: {e}]")


# ── Step 1: Market hours ──────────────────────────────────────────────────────
def check_market():
    utc = datetime.now(timezone.utc)
    et  = utc - timedelta(hours=4)   # EDT Mar–Nov; change to 5 in winter
    if et.weekday() >= 5:
        return False, et
    open_  = et.replace(hour=9,  minute=45, second=0, microsecond=0)
    close_ = et.replace(hour=15, minute=55, second=0, microsecond=0)
    return open_ <= et <= close_, et


# ── Indicators (identical to the Robinhood bot) ───────────────────────────────
def calc_rsi(closes, n=14):
    gains  = [max(closes[i]-closes[i-1], 0) for i in range(-n, 0)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(-n, 0)]
    mg, ml = sum(gains)/n, sum(losses)/n
    return 100 - 100/(1+mg/ml) if ml > 0 else 100

def calc_ema(prices, n):
    k, v = 2/(n+1), sum(prices[:n])/n
    for p in prices[n:]: v = p*k + v*(1-k)
    return v

def calc_ema_series(prices, n):
    k, out = 2/(n+1), [None]*(n-1)
    v = sum(prices[:n])/n; out.append(v)
    for p in prices[n:]: v = p*k + v*(1-k); out.append(v)
    return out

def compute_signals(sym, closes, vols, live, meme_tickers):
    if len(closes) < 20: return None
    closes, vols = list(closes), list(vols)
    closes[-1] = live

    r   = calc_rsi(closes)
    s20 = sum(closes[-20:])/20
    s50 = sum(closes[-50:])/50 if len(closes) >= 50 else None
    e9  = calc_ema(closes, 9)
    e21 = calc_ema(closes, 21)

    fast = calc_ema_series(closes, 12)
    slow = calc_ema_series(closes, 26)
    ml   = [f-s for f, s in zip(fast, slow) if f and s]
    hist = ml[-1] - calc_ema(ml, 9)

    mid  = s20
    std  = math.sqrt(sum((p-mid)**2 for p in closes[-20:])/20)
    pctb = (live-mid+2*std)/(4*std) if std > 0 else 0.5

    trend = "neutral"
    if s50:
        if   live > s50*1.002: trend = "up"
        elif live < s50*0.998: trend = "down"

    v1 = (1 if live > s20*1.005 and r > 52 else -1 if live < s20*0.995 and r < 48 else 0)
    v2 = 1 if r < 32 else (-1 if r > 68 else 0)
    v3 = 1 if e9 > e21*1.001 else (-1 if e9 < e21*0.999 else 0)
    v4 = 1 if hist > 0 else -1

    avg_vol = sum(vols[-20:-1])/len(vols[-20:-1]) if len(vols) >= 20 else 0
    delta   = (closes[-1]-closes[-2])/closes[-2] if closes[-2] else 0
    v5 = 0
    if vols and vols[-1] > avg_vol*1.8:
        v5 = 1 if delta > 0.003 else (-1 if delta < -0.003 else 0)

    bb     = 1 if pctb < 0.10 else (-1 if pctb > 0.90 else 0)
    meme_b = 2 if sym in meme_tickers and r < 75 and delta > 0 else 0

    buys  = sum(1 for v in [v1,v2,v3,v4,v5] if v==1)  + meme_b + (bb if bb==1  else 0)
    sells = sum(1 for v in [v1,v2,v3,v4,v5] if v==-1) + (abs(bb) if bb==-1 else 0)

    raw = 1 if buys >= 3 else (-1 if sells >= 3 else 0)
    con = raw
    if raw ==  1 and trend == "down": con = 0
    if raw == -1 and trend == "up":   con = 0
    if sym in meme_tickers and meme_b == 2 and buys >= 2 and trend != "down": con = 1

    return {"sym": sym, "rsi": r, "trend": trend, "consensus": con, "delta": delta,
            "buys": buys, "sells": sells, "macd_up": hist > 0, "bb": bb, "meme": meme_b > 0}


# ── Yahoo Finance helpers (broker-agnostic, identical) ────────────────────────
YF_HEADERS = {"User-Agent": "Mozilla/5.0"}

def yf_ohlcv(sym):
    try:
        d = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=90d",
                         headers=YF_HEADERS, timeout=10).json()
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        c = [x for x in q["close"]  if x is not None]
        v = [x for x in q["volume"] if x is not None]
        return (c, v) if len(c) >= 20 else (None, None)
    except Exception: return None, None

def yf_live(sym):
    try:
        d = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d",
                         headers=YF_HEADERS, timeout=10).json()
        c = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return next(x for x in reversed(c) if x)
    except Exception: return None

def yf_vix():
    try:
        d = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=5d",
                         headers=YF_HEADERS, timeout=10).json()
        c = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return next(x for x in reversed(c) if x)
    except Exception: return 20.0

def fetch_wsb():
    try:
        d = requests.get("https://apewisdom.io/api/v1.0/filter/wallstreetbets", timeout=10).json()
        return [x["ticker"] for x in d["results"][:10]]
    except Exception: return []

def fetch_screener():
    try:
        url = ("https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
               "?formatted=false&scrIds=most_actives&count=15")
        qs = requests.get(url, headers=YF_HEADERS, timeout=10).json()["finance"]["result"][0]["quotes"]
        qs = [q for q in qs if q.get("regularMarketPrice",0) >= 0.01
              and q.get("averageDailyVolume3Month",0) >= 100000
              and "." not in q["symbol"] and "-" not in q["symbol"]]
        qs.sort(key=lambda q: q.get("averageDailyVolume3Month",0), reverse=True)
        return [q["symbol"] for q in qs[:10]]
    except Exception: return ["AAPL","MSFT","NVDA","AMD","TSLA","META","GOOGL","AMZN"]

def fetch_smallcaps():
    """Low-cap growth discovery via Yahoo's small-cap screeners. Quality rails:
    price >= $0.25 (allows liquid sub-$1 movers — bought as WHOLE shares since
    Alpaca blocks notional orders on non-fractionable names; true OTC penny stocks
    aren't tradeable on Alpaca at all), >=500k shares/day AND >=$5M/day traded so
    spreads don't eat the edge. These are only CANDIDATES — the signal engine
    still has to vote them in like any name."""
    out, seen = [], set()
    for scr in ("small_cap_gainers", "aggressive_small_caps"):
        try:
            url = ("https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
                   f"?formatted=false&scrIds={scr}&count=15")
            qs = requests.get(url, headers=YF_HEADERS, timeout=10).json()["finance"]["result"][0]["quotes"]
            for q in qs:
                px  = q.get("regularMarketPrice", 0) or 0
                vol = q.get("averageDailyVolume3Month", 0) or 0
                sym = q.get("symbol", "")
                if (px >= 0.25 and vol >= 500_000 and px*vol >= 5_000_000
                        and sym and "." not in sym and "-" not in sym and sym not in seen):
                    seen.add(sym); out.append(sym)
        except Exception:
            continue
    return out[:8]

# ── Alpaca broker layer (official REST API) ───────────────────────────────────
def alpaca_get(path):
    r = requests.get(ALPACA_BASE + path, headers=ALPACA_HDRS, timeout=15)
    return r.json()

def alpaca_account():
    """Returns (cash, equity, daily_pnl, daytrade_count).
    We size off CASH — NOT Alpaca's margin cash — so the bot never trades on
    leverage. This keeps paper swings realistic and matches a small live cash account.
    daily_pnl = equity − last_equity (today's total change; for the safety loss-cap).
    PDT count comes straight from Alpaca's daytrade_count."""
    a = alpaca_get("/v2/account")
    cash = float(a.get("cash") or 0)
    eq   = float(a["equity"])
    leq  = float(a.get("last_equity") or eq)
    pdt  = int(a.get("daytrade_count") or 0)
    return cash, eq, eq - leq, pdt

def alpaca_positions():
    pos = {}
    for p in alpaca_get("/v2/positions"):
        q = float(p.get("qty_available") or p.get("qty") or 0)
        if q > 0:
            pos[p["symbol"]] = {"qty": q, "avg_cost": float(p.get("avg_entry_price") or 0),
                                "mkt_val": float(p.get("market_value") or 0)}
    return pos


def load_holds():
    """The buy-and-hold ledger (holds.json, committed back to the repo like the
    trade log). Maps symbol -> {ts, basis, notional}. Symbols here are EXEMPT from
    sell signals — only the HOLD_STOP disaster stop exits them."""
    try:
        return json.load(open("holds.json"))
    except Exception:
        return {}

def save_holds(holds):
    with open("holds.json", "w") as f:
        json.dump(holds, f, indent=1, sort_keys=True)


def load_plan(et):
    """Read today's research plan (daily_plan.json, written by brief.py).
    risk is clamped to [0,1] — the plan can only scale buys DOWN, never past the
    bot's hard rails. A missing or stale (not-today) plan → neutral defaults, so the
    bot is unaffected when the brief hasn't run."""
    neutral = {"regime": "neutral", "risk": 1.0, "avoid": set(), "favor": [], "notes": "no plan"}
    try:
        if not os.path.exists("daily_plan.json"):
            return neutral
        p = json.load(open("daily_plan.json"))
        if p.get("date") != et.strftime("%Y-%m-%d"):
            return neutral   # stale plan from a previous day — ignore
        rs = max(0.0, min(1.0, float(p.get("risk_scale", 1.0))))
        return {"regime": p.get("regime", "neutral"), "risk": rs,
                "avoid": set(p.get("avoid_symbols", [])),
                "favor": list(p.get("favor_symbols", [])),
                "notes": p.get("notes", "")}
    except Exception:
        return neutral

def alpaca_order(payload):
    """POST an order. Returns Alpaca's JSON — has 'id' on success, 'message' on error."""
    try:
        r = requests.post(ALPACA_BASE + "/v2/orders", headers=ALPACA_HDRS, json=payload, timeout=15)
        try:
            return r.json()
        except Exception:
            return {"message": f"non-JSON {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"message": f"request failed: {e}"}

def _ok(result):
    """Alpaca accepted the order iff it echoes an id and carries no error message."""
    return bool(result) and result.get("id") and not result.get("message")

_ASSET_CACHE = {}
def alpaca_asset(sym):
    """Asset metadata (tradable/fractionable), cached per run."""
    if sym not in _ASSET_CACHE:
        try:    _ASSET_CACHE[sym] = alpaca_get(f"/v2/assets/{sym}") or {}
        except Exception: _ASSET_CACHE[sym] = {}
    return _ASSET_CACHE[sym]

def place_buy(sym, dollar_amount, live=None):
    """MARKET buy. Fractionable names buy by dollar amount (notional). Names Alpaca
    won't fraction (most sub-$1 / low-cap tickers) buy WHOLE shares instead —
    qty = floor(amount / live) — so cheap small-caps are still reachable."""
    a = alpaca_asset(sym)
    if a and a.get("tradable") is False:
        r = {"message": f"{sym} not tradable on Alpaca"}
    elif a.get("fractionable", True):
        r = alpaca_order({"symbol": sym, "notional": str(round(dollar_amount, 2)),
                          "side": "buy", "type": "market", "time_in_force": "day"})
    elif live and live > 0 and int(dollar_amount // live) >= 1:
        r = alpaca_order({"symbol": sym, "qty": str(int(dollar_amount // live)),
                          "side": "buy", "type": "market", "time_in_force": "day"})
    else:
        r = {"message": f"{sym} not fractionable; ${dollar_amount:.2f} buys <1 share"}
    if not _ok(r):
        print(f"    [buy rejected: {(r or {}).get('message', r)}]")
    return r

def place_sell(sym, qty):
    """Fractional MARKET sell of a held quantity."""
    r = alpaca_order({"symbol": sym, "qty": str(qty),
                      "side": "sell", "type": "market", "time_in_force": "day"})
    if not _ok(r):
        print(f"    [sell rejected: {(r or {}).get('message', r)}]")
    return r


# ── Main bot ──────────────────────────────────────────────────────────────────
def run_bot():
    open_, et = check_market()
    if not open_:
        print(f"Market closed ({et.strftime('%H:%M ET')}). Done.")
        return

    print(f"=== Alpaca bot run {et.strftime('%Y-%m-%d %H:%M ET')} | {MODE} ===")
    events     = []   # human-readable order outcomes (drives the email)
    trades_log = []   # structured per-trade context (appended to trade_log.jsonl)

    cash, equity, daily_pnl, pdt = alpaca_account()
    acct_tag    = str((alpaca_get("/v2/account") or {}).get("account_number", "????"))[-4:]  # tags each trade so an account swap can't silently corrupt the log/review
    max_pos     = equity * MAX_POS_PCT
    spend_cap   = max(0.0, cash) * SPEND_CAP_PCT        # ≤25% of CASH per run (no margin)
    low_cash    = cash < 5.00
    positions   = alpaca_positions()
    plan        = load_plan(et)

    # Sleeve accounting: holds (buy-and-keep ledger) vs trading (everything else).
    holds       = load_holds()
    holds_dirty = False
    stale = [s for s in holds if s not in positions]    # sold manually / stopped out
    for s in stale: holds.pop(s); holds_dirty = True
    hold_val    = sum(positions[s]["mkt_val"] for s in holds)
    invested    = max(0.0, equity - cash)               # total $ held in positions
    trade_val   = max(0.0, invested - hold_val)         # signal-traded portion
    invest_room = max(0.0, equity * MAX_INVESTED_PCT - trade_val)  # trading-sleeve headroom
    hold_room   = max(0.0, equity * HOLD_PCT - hold_val)           # hold-sleeve headroom
    spent = trade_spent = hold_spent = 0.0
    print(f"  Cash=${cash:.2f}  EQ=${equity:.2f}  dayP&L=${daily_pnl:.2f}  PDT={pdt}  acct=…{acct_tag}")
    print(f"  Sleeves: trade ${trade_val:,.0f}/{equity*MAX_INVESTED_PCT:,.0f} (room ${invest_room:,.0f}) | "
          f"hold ${hold_val:,.0f}/{equity*HOLD_PCT:,.0f} (room ${hold_room:,.0f}) | holds: {sorted(holds) or '—'}")
    print(f"  PLAN: {plan['regime']} | risk={plan['risk']} | avoid={sorted(plan['avoid'])} | {plan['notes'][:80]}")

    # Circuit breakers
    loss_cap = max(LOSS_CAP_FLOOR, equity * LOSS_CAP_PCT)
    if daily_pnl <= -loss_cap:
        print(f"Daily loss cap hit (dayP&L ${daily_pnl:.2f} <= -${loss_cap:.2f}). Stopping.")
        return
    pdt_exhausted = pdt >= PDT_LIMIT

    # Universe
    universe     = set(positions.keys()) | {"SPY"}
    meme_tickers = []
    small_caps   = set()
    if low_cash:
        print("LOW_CASH — positions only.")
    else:
        wsb    = fetch_wsb()
        screen = fetch_screener()
        smalls = fetch_smallcaps()
        meme_tickers = wsb
        small_caps   = set(smalls)
        for s in wsb + screen + smalls + plan["favor"]:   # plan's favored names get considered too
            if len(universe) < 28: universe.add(s)
    universe = list(universe)
    print(f"UNIVERSE ({len(universe)}): {universe}")
    if small_caps & set(universe):
        print(f"  smallcap candidates (half-size): {sorted(small_caps & set(universe))}")

    # Market data
    vix = yf_vix()
    print(f"VIX={vix:.1f}")
    if vix > 35:
        print("VIX>35. Halt."); return
    vix_scale = 0.50 if vix > 25 else (0.75 if vix > 20 else 1.00)

    market, consec = {}, 0
    for sym in universe:
        c, v = yf_ohlcv(sym)
        if c is None:
            consec += 1
            if consec >= 3: break
            continue
        consec = 0
        live = yf_live(sym)
        if live: market[sym] = {"closes": c, "volumes": v, "live": live}

    # Signals
    sigs = {}
    for sym, d in market.items():
        rr = compute_signals(sym, d["closes"], d["volumes"], d["live"], meme_tickers)
        if rr: sigs[sym] = rr

    # SELL first (frees buying power). Hold-sleeve names are exempt — they only
    # exit via the disaster stop below.
    for sym, sig in sigs.items():
        if sig["consensus"] != -1 or sym not in positions: continue
        if sym in holds:
            print(f"  KEEP {sym} (hold sleeve — sell signal ignored)"); continue
        qty = positions[sym]["qty"]
        if qty <= 0: continue
        print(f"SELL {sym} qty={qty} RSI={sig['rsi']:.1f}")
        try:
            r = place_sell(sym, qty)
            if _ok(r):
                print(f"  → placed {r['id']}")
                cash += qty * market[sym]["live"]; low_cash = False
                events.append(f"SELL {sym} qty={qty} → PLACED ({r['id']})")
                trades_log.append({
                    "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag, "symbol": sym,
                    "side": "sell", "qty": qty, "order_id": r["id"],
                    "live": round(market[sym]["live"], 2), "rsi": round(sig["rsi"], 1),
                    "trend": sig["trend"], "consensus": sig["consensus"],
                    "delta": round(sig["delta"], 4), "sells": sig["sells"],
                    "macd_up": sig["macd_up"], "vix": round(vix, 1)})
            else:
                events.append(f"SELL {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"SELL {sym} → ERROR: {e}")

    # HOLD disaster stop: a hold trading at <=75% of its basis is a broken thesis —
    # sell it and free the sleeve. (This is the ONLY way the bot exits a hold.)
    for sym in list(holds):
        if sym not in positions: continue
        live  = market.get(sym, {}).get("live")
        basis = float(holds[sym].get("basis") or 0)
        if not live or basis <= 0 or live > basis * HOLD_STOP: continue
        qty = positions[sym]["qty"]
        print(f"HOLD-STOP {sym} qty={qty} (live ${live:.2f} <= {HOLD_STOP:.0%} of basis ${basis:.2f})")
        try:
            r = place_sell(sym, qty)
            if _ok(r):
                print(f"  → placed {r['id']}")
                cash += qty * live; low_cash = False
                holds.pop(sym); holds_dirty = True
                events.append(f"HOLD-STOP {sym} qty={qty} (-25% from basis) → PLACED ({r['id']})")
                trades_log.append({
                    "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag, "symbol": sym,
                    "side": "sell", "hold_stop": True, "qty": qty, "order_id": r["id"],
                    "live": round(live, 2), "basis": round(basis, 2), "vix": round(vix, 1)})
            else:
                events.append(f"HOLD-STOP {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"HOLD-STOP {sym} → ERROR: {e}")

    # BUY — new entries AND adds to held winners, up to the per-name cap.
    # Small-caps size at SMALLCAP_POS_PCT (half) — growthier but blowup-prone.
    # Sleeve routing: STRONG signals (4+ buy votes AND uptrend) buy into the HOLD
    # sleeve (kept until the disaster stop); everything else is a trading-sleeve buy.
    if not low_cash and not pdt_exhausted:
        for sym, sig in sigs.items():
            if sig["consensus"] != 1: continue
            if sym in plan["avoid"]:
                print(f"  SKIP {sym} (plan avoid-list)"); continue
            if spent >= spend_cap: break                      # per-run pacing cap reached
            if (invest_room - trade_spent) < 1.00 and (hold_room - hold_spent) < 1.00:
                break                                         # both sleeves full
            # A strong signal can top up a held name, but never past its per-name cap.
            held_value = 0.0
            if sym in positions and positions[sym]["qty"] > 0 and sym in market:
                held_value = positions[sym]["qty"] * market[sym]["live"]
            strong   = sig["buys"] >= 4 and sig["trend"] == "up"
            use_hold = (sym in holds) or (strong and sym not in positions
                                          and (hold_room - hold_spent) >= 1.00)
            sleeve_room  = (hold_room - hold_spent) if use_hold else (invest_room - trade_spent)
            name_cap     = equity * (SMALLCAP_POS_PCT if sym in small_caps else MAX_POS_PCT)
            room_in_name = name_cap - held_value
            if room_in_name < 1.00: continue
            amount = min(name_cap * vix_scale * plan["risk"], room_in_name, cash * 0.95,
                         spend_cap - spent, sleeve_room)
            if amount < 1.00: continue
            if held_value > 0 and amount < name_cap * 0.20:
                continue   # near its cap — skip dribble top-ups every run
            live = market[sym]["live"]
            verb = (("HOLD-ADD" if held_value > 0 else "HOLD-BUY") if use_hold
                    else ("ADD" if held_value > 0 else "BUY"))
            print(f"{verb} {sym} ${amount:.2f} RSI={sig['rsi']:.1f}"
                  + (" [smallcap]" if sym in small_caps else ""))
            try:
                r = place_buy(sym, amount, live)
                if _ok(r):
                    # Whole-share orders may spend slightly less than requested.
                    actual = amount
                    if r.get("qty") and not r.get("notional"):
                        actual = round(float(r["qty"]) * live, 2)
                    print(f"  → placed {r['id']} (${actual:.2f})")
                    cash -= actual; spent += actual
                    if use_hold: hold_spent  += actual
                    else:        trade_spent += actual
                    if sym not in positions:
                        positions[sym] = {"qty": 0, "avg_cost": 0, "mkt_val": 0.0}
                    if use_hold:
                        h = holds.get(sym)
                        if h:   # weighted-average basis across adds
                            prev = float(h.get("notional") or 0)
                            tot  = prev + actual
                            h["basis"]    = round((float(h["basis"])*prev + live*actual)/tot, 4) if tot else live
                            h["notional"] = round(tot, 2)
                        else:
                            holds[sym] = {"ts": et.strftime("%Y-%m-%dT%H:%M"),
                                          "basis": round(live, 4), "notional": round(actual, 2)}
                        holds_dirty = True
                    events.append(f"{verb} {sym} ${actual:.2f} → PLACED ({r['id']})")
                    trades_log.append({
                        "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag, "symbol": sym,
                        "side": "buy", "add": held_value > 0, "smallcap": sym in small_caps,
                        "hold": use_hold, "notional": round(actual, 2), "order_id": r["id"],
                        "live": round(live, 2), "rsi": round(sig["rsi"], 1),
                        "trend": sig["trend"], "consensus": sig["consensus"],
                        "delta": round(sig["delta"], 4), "buys": sig["buys"],
                        "macd_up": sig["macd_up"], "meme": sig["meme"], "vix": round(vix, 1)})
                else:
                    events.append(f"{verb} {sym} ${amount:.2f} → REJECTED: {(r or {}).get('message', r)}")
            except Exception as e:
                events.append(f"{verb} {sym} ${amount:.2f} → ERROR: {e}")

    # Summary
    print(f"\n--- {MODE} | {et.strftime('%H:%M ET')} | VIX={vix:.1f} | PDT={pdt} | "
          f"dayP&L=${daily_pnl:.2f} | Cash=${cash:.2f} ---")
    if holds:
        hl = []
        for s, h in sorted(holds.items()):
            lv = market.get(s, {}).get("live")
            hl.append(f"{s} {((lv/float(h['basis'])-1)*100):+.1f}%" if lv and float(h.get("basis") or 0) > 0 else s)
        print(f"  HOLDS vs basis: {', '.join(hl)}")
    for sym, sig in sigs.items():
        if sig["consensus"] != 0:
            print(f"  {sym}: {sig['consensus']:+d}  RSI={sig['rsi']:.1f}  {sig['trend']}")
    print(f"  Zero-consensus: {sum(1 for s in sigs.values() if s['consensus']==0)}")

    # Email: on any order activity, plus once at the 9:45 ET morning run
    morning = (et.hour == 9 and et.minute >= 45)
    if events or morning:
        nonzero = [f"  {s}: {sig['consensus']:+d}  RSI={sig['rsi']:.1f}  {sig['trend']}"
                   for s, sig in sigs.items() if sig["consensus"] != 0]
        body = [f"Alpaca bot ({MODE})  {et.strftime('%Y-%m-%d %H:%M ET')}",
                f"Plan: {plan['regime']} | risk {plan['risk']} | avoid {sorted(plan['avoid'])}",
                f"Equity ${equity:.2f} | Cash ${cash:.2f} | "
                f"dayP&L ${daily_pnl:.2f} | PDT {pdt}/3", ""]
        body.append("ORDERS THIS RUN:" if events else "No orders this run.")
        body += [f"  • {e}" for e in events]
        body += ["", "Signals (non-neutral):"] + (nonzero or ["  (all neutral)"])
        if any("PLACED" in e for e in events):
            subject = f"✅ Alpaca bot ({MODE}) — ORDER PLACED"
        elif any(("REJECTED" in e or "ERROR" in e) for e in events):
            subject = f"⚠️ Alpaca bot ({MODE}) — order rejected"
        else:
            subject = f"Alpaca bot ({MODE}) — morning status"
        send_email(subject, "\n".join(body))

    # Persist structured trade context for the weekly review / pattern analysis.
    # The workflow commits trade_log.jsonl (and holds.json) back to the repo.
    if trades_log:
        with open("trade_log.jsonl", "a") as f:
            for t in trades_log:
                f.write(json.dumps(t) + "\n")
        print(f"  [logged {len(trades_log)} trade(s) to trade_log.jsonl]")
    if holds_dirty:
        save_holds(holds)
        print(f"  [holds.json updated — {len(holds)} hold(s)]")


if __name__ == "__main__":
    # Manual test: `gh workflow run alpaca-bot.yml -f email_test=true`
    if os.environ.get("EMAIL_TEST", "").lower() == "true":
        send_email(f"📧 Alpaca bot ({MODE}) — email test OK",
                   "Cloud email alerts are working for the Alpaca bot.")
        raise SystemExit(0)

    # Order-path test: `gh workflow run alpaca-bot.yml -f order_test=true`
    # Validates auth + the order endpoint by placing a tiny $1 PAPER buy, any time
    # of day. Refuses to run against a LIVE account so it can't spend real money.
    if os.environ.get("ORDER_TEST", "").lower() == "true":
        print(f"=== ORDER_TEST ({MODE}) ===")
        if MODE == "LIVE":
            print("Refusing ORDER_TEST on a LIVE account."); raise SystemExit(1)
        bp, eq, dpnl, pdt = alpaca_account()
        print(f"Account OK: BP=${bp:.2f}  EQ=${eq:.2f}  PDT={pdt}")
        print("Placing $1 notional AAPL market buy (paper)...")
        r = place_buy("AAPL", 1.00)
        print("Order result:", json.dumps(r)[:400])
        print("✅ ORDER PLACED — Alpaca order path works." if _ok(r)
              else "⚠️ Not accepted (see message). Auth/account confirmed above.")
        raise SystemExit(0)

    try:
        run_bot()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            _, _et = check_market()
            if _et.hour == 9 and _et.minute >= 45:
                send_email(f"❌ Alpaca bot ({MODE}) — CRASHED at open", tb[-3000:])
        except Exception:
            pass
        raise
