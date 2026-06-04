"""
Robinhood Day Trading Bot — GitHub Actions
Runs every 5 min during market hours via cron schedule.

ACCOUNT SAFETY: Every single Robinhood API call is locked to ACCOUNT = 950942706.
The bot will hard-abort at login if it detects any other account. No trades ever
fire on any other account under any circumstances.
"""

import os, math, uuid, base64, requests, pyotp
from datetime import datetime, timezone, timedelta
import robin_stocks.robinhood as rh

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT     = "950942706"
ACCOUNT_URL = f"https://api.robinhood.com/accounts/{ACCOUNT}/"

DAILY_LOSS_CAP = 20.00
PDT_LIMIT      = 3
MAX_POS_PCT    = 0.30
SPEND_CAP_PCT  = 0.75

RH_USERNAME    = os.environ["RH_USERNAME"]
RH_PASSWORD    = os.environ["RH_PASSWORD"]
RH_TOTP_SECRET = os.environ.get("RH_TOTP_SECRET", "")
RH_SESSION_B64 = os.environ.get("RH_SESSION_B64", "")


# ── Step 1: Market hours ──────────────────────────────────────────────────────
def check_market():
    utc = datetime.now(timezone.utc)
    et  = utc - timedelta(hours=4)   # EDT Mar–Nov; change to 5 in winter
    if et.weekday() >= 5:
        return False, et
    open_  = et.replace(hour=9,  minute=45, second=0, microsecond=0)
    close_ = et.replace(hour=15, minute=55, second=0, microsecond=0)
    return open_ <= et <= close_, et


# ── Indicators ────────────────────────────────────────────────────────────────
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

    return {"sym": sym, "rsi": r, "trend": trend, "consensus": con, "delta": delta}


# ── Yahoo Finance helpers ─────────────────────────────────────────────────────
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


# ── Robinhood — account-locked helpers ───────────────────────────────────────
#
# EVERY function below explicitly targets ACCOUNT / ACCOUNT_URL.
# None of them use robin_stocks defaults that could resolve to a different account.

def rh_api_get(url, params=None):
    """Authenticated GET via robin_stocks session — returns parsed JSON."""
    return rh.helper.request_get(url, dataType="regular", payload=params)


def rh_login():
    """Login and HARD-ABORT if we are not on account 950942706."""
    if RH_SESSION_B64:
        home      = os.path.expanduser("~")
        token_dir = os.path.join(home, ".tokens")
        os.makedirs(token_dir, exist_ok=True)
        with open(os.path.join(token_dir, "robinhood.pickle"), "wb") as f:
            f.write(base64.b64decode(RH_SESSION_B64.strip()))
        rh.login(RH_USERNAME, RH_PASSWORD, store_session=True)
    else:
        mfa = pyotp.TOTP(RH_TOTP_SECRET).now() if RH_TOTP_SECRET else None
        rh.login(RH_USERNAME, RH_PASSWORD, mfa_code=mfa, store_session=False)

    # ── HARD ACCOUNT CHECK — call the specific account URL directly ───────────
    # This bypasses robin_stocks defaults and hits our account exclusively.
    profile     = rh_api_get(ACCOUNT_URL)
    actual_url  = (profile or {}).get("url", "").rstrip("/")
    actual_acct = actual_url.split("/")[-1]
    if actual_acct != ACCOUNT:
        rh.logout()
        raise SystemExit(
            f"SAFETY ABORT: wrong account '{actual_acct}' — expected '{ACCOUNT}'. "
            "No trades placed. Exiting."
        )
    print(f"  Account verified: {ACCOUNT} ✓")


def get_portfolio():
    """Load buying power and equity directly from ACCOUNT_URL."""
    acct = rh_api_get(ACCOUNT_URL)
    port = rh_api_get(f"https://api.robinhood.com/portfolios/{ACCOUNT}/")
    return float(acct["buying_power"]), float(port["equity"])


def get_positions():
    """Load open positions filtered to ACCOUNT only."""
    pos = {}
    # Filter positions by account URL to guarantee we only see ACCOUNT's positions
    raw = rh_api_get("https://api.robinhood.com/positions/",
                     params={"account_number": ACCOUNT, "nonzero": "true"})
    items = raw if isinstance(raw, list) else (raw or {}).get("results", [])
    for p in items:
        if ACCOUNT_URL not in p.get("account", ""):
            continue
        sym = rh.stocks.get_symbol_by_url(p["instrument"])
        qty = float(p["quantity"])
        if qty > 0:
            pos[sym] = {"qty": qty, "avg_cost": float(p["average_buy_price"])}
    return pos


def get_daily_pnl_and_pdt(et):
    """Compute daily P&L and PDT count, filtered to ACCOUNT only."""
    today   = et.strftime("%Y-%m-%d")
    cutoff  = (et - timedelta(days=7)).date()
    # get_all_stock_orders returns all accounts; filter by our account URL
    orders  = rh.orders.get_all_stock_orders()
    pnl     = 0.0
    day_trades = {}

    for o in orders:
        # Skip orders not belonging to ACCOUNT
        if ACCOUNT_URL not in o.get("account", ""):
            continue
        if o["state"] != "filled":
            continue
        ts = o.get("last_transaction_at") or o.get("created_at") or ""
        if not ts: continue
        dt  = datetime.fromisoformat(ts.replace("Z", "+00:00")) - timedelta(hours=4)
        sym = rh.stocks.get_symbol_by_url(o["instrument"])

        if dt.strftime("%Y-%m-%d") == today and o["side"] == "sell":
            avg_p = float(o.get("average_price") or 0)
            qty_f = float(o.get("cumulative_quantity") or 0)
            avg_c = float(o.get("average_buy_price") or avg_p)
            pnl  += (avg_p - avg_c) * qty_f

        if dt.date() >= cutoff:
            key = (dt.date(), sym)
            day_trades.setdefault(key, {"buy": 0, "sell": 0})
            day_trades[key][o["side"]] += 1

    pdt = sum(1 for v in day_trades.values() if v["buy"] > 0 and v["sell"] > 0)
    return pnl, pdt




def _order_post(payload):
    """
    POST an order to Robinhood using the session's existing auth token but
    with a bumped API version (1.440.0) that supports order placement.
    The instrument lookup must be done BEFORE calling this (at default version).
    """
    _orig_ver = rh.helper.SESSION.headers.get("X-Robinhood-API-Version", "")
    rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": "1.440.0"})
    try:
        resp = rh.helper.SESSION.post("https://api.robinhood.com/orders/", json=payload)
        return resp.json()
    finally:
        rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": _orig_ver})


def place_buy(sym, dollar_amount):
    """
    Fractional market buy locked to ACCOUNT.
    Step 1: resolve instrument at default API version (works fine).
    Step 2: POST order at 1.440.0 (required for order placement).
    """
    instruments = rh.stocks.get_instruments_by_symbols(sym, info="url")
    if not instruments:
        raise ValueError(f"No instrument found for {sym}")
    return _order_post({
        "account":             ACCOUNT_URL,
        "instrument":          instruments[0],
        "symbol":              sym,
        "type":                "market",
        "time_in_force":       "gfd",
        "trigger":             "immediate",
        "side":                "buy",
        "dollar_based_amount": {"amount": str(round(dollar_amount, 2)),
                                "currency_code": "USD"},
        "ref_id":              str(uuid.uuid4()),
    })


def place_sell(sym, qty):
    """
    Market sell locked to ACCOUNT.
    Same two-step approach as place_buy.
    """
    instruments = rh.stocks.get_instruments_by_symbols(sym, info="url")
    if not instruments:
        raise ValueError(f"No instrument found for {sym}")
    return _order_post({
        "account":       ACCOUNT_URL,
        "instrument":    instruments[0],
        "symbol":        sym,
        "type":          "market",
        "time_in_force": "gfd",
        "trigger":       "immediate",
        "side":          "sell",
        "quantity":      str(round(qty, 6)),
        "ref_id":        str(uuid.uuid4()),
    })


# ── Main bot ──────────────────────────────────────────────────────────────────
def run_bot(request=None):
    # Step 1: market hours
    open_, et = check_market()
    if not open_:
        print(f"Market closed ({et.strftime('%H:%M ET')}). Done.")
        return ("market_closed", 200)

    print(f"=== Bot run {et.strftime('%Y-%m-%d %H:%M ET')} | account={ACCOUNT} ===")

    # Step 2: login (aborts on wrong account)
    rh_login()

    buying_power, equity = get_portfolio()
    max_pos   = equity * MAX_POS_PCT
    spend_cap = buying_power * SPEND_CAP_PCT
    spent     = 0.0
    low_cash  = buying_power < 5.00

    positions          = get_positions()
    daily_pnl, pdt     = get_daily_pnl_and_pdt(et)

    print(f"  BP=${buying_power:.2f}  EQ=${equity:.2f}  P&L=${daily_pnl:.2f}  PDT={pdt}")

    # Step 3: circuit breakers
    if daily_pnl <= -DAILY_LOSS_CAP:
        print("Loss cap hit. Stopping.")
        rh.logout(); return ("loss_cap", 200)
    pdt_exhausted = pdt >= PDT_LIMIT

    # Step 4: universe
    universe     = set(positions.keys()) | {"SPY"}
    meme_tickers = []
    if low_cash:
        print("LOW_CASH — positions only.")
    else:
        wsb    = fetch_wsb()
        screen = fetch_screener()
        meme_tickers = wsb
        for s in wsb + screen:
            if len(universe) < 20: universe.add(s)
    universe = list(universe)
    print(f"UNIVERSE ({len(universe)}): {universe}")

    # Step 5: market data
    vix = yf_vix()
    print(f"VIX={vix:.1f}")
    if vix > 35:
        print("VIX>35. Halt."); rh.logout(); return ("vix_halt", 200)
    vix_scale = 0.50 if vix > 25 else (0.75 if vix > 20 else 1.00)

    market, consec_fails = {}, 0
    for sym in universe:
        c, v = yf_ohlcv(sym)
        if c is None:
            consec_fails += 1
            if consec_fails >= 3: break
            continue
        consec_fails = 0
        live = yf_live(sym)
        if live: market[sym] = {"closes": c, "volumes": v, "live": live}

    # Step 6: signals
    sigs = {}
    for sym, d in market.items():
        r = compute_signals(sym, d["closes"], d["volumes"], d["live"], meme_tickers)
        if r: sigs[sym] = r

    # Step 7: sell first
    for sym, sig in sigs.items():
        if sig["consensus"] != -1 or sym not in positions: continue
        qty = positions[sym]["qty"]
        if qty <= 0: continue
        print(f"SELL {sym} qty={qty:.6f} RSI={sig['rsi']:.1f}")
        try:
            result = place_sell(sym, qty)
            if result and result.get("id"):
                print(f"  → Order placed: {result['id']}")
                buying_power += qty * market[sym]["live"]
                low_cash = False
            else:
                print(f"  → SELL rejected: {result}")
        except Exception as e:
            print(f"  → SELL error: {e}")

    # Step 7: buy
    if not low_cash and not pdt_exhausted:
        for sym, sig in sigs.items():
            if sig["consensus"] != 1 or sym in positions: continue
            if spent >= spend_cap: break
            amount = min(max_pos * vix_scale, buying_power * 0.95, spend_cap - spent)
            if amount < 1.00: continue
            print(f"BUY {sym} ${amount:.2f} RSI={sig['rsi']:.1f}")
            try:
                result = place_buy(sym, amount)
                if result and result.get("id"):
                    print(f"  → Order placed: {result['id']}")
                    buying_power -= amount; spent += amount
                    positions[sym] = {"qty": 0, "avg_cost": 0}
                else:
                    print(f"  → BUY rejected: {result}")
            except Exception as e:
                print(f"  → BUY error: {e}")

    # Step 8: summary
    print(f"\n--- Summary | {et.strftime('%H:%M ET')} | acct={ACCOUNT} | "
          f"VIX={vix:.1f} | PDT={pdt} | P&L=${daily_pnl:.2f} | BP=${buying_power:.2f} ---")
    for sym, sig in sigs.items():
        if sig["consensus"] != 0:
            print(f"  {sym}: {sig['consensus']:+d}  RSI={sig['rsi']:.1f}  {sig['trend']}")
    print(f"  Zero-consensus: {sum(1 for s in sigs.values() if s['consensus']==0)}")

    rh.logout()
    return ("ok", 200)


if __name__ == "__main__":
    run_bot()
