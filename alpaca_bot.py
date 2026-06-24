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
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
# Daily loss halt = max(10% of equity, $20). Loose on purpose: it only stops the
# day on a genuine crash — and even then it only blocks BUYS; exits keep running
# so the bot can always de-risk. The real risk controls are the sleeve caps,
# per-name caps, and per-position brackets below.
#
# Capital is split into three sleeves (max 90% deployed, 10% cash floor):
#   TRADING sleeve (30%): signal-driven buys managed by brackets + sell signals.
#   HOLD sleeve (50%):    strong-signal names (4+ buy votes AND uptrend) bought to
#                         KEEP — exempt from sell signals; exit only on the basis
#                         stop (-25%) or the peak ratchet (gives back 40% from high).
#   CRYPTO sleeve (10%):  DOGE-style moonshot exposure via Alpaca crypto (spot,
#                         long-only, 24/7 assets traded on the bot's schedule).
#                         Wider brackets (-15%/+30%) for crypto volatility.
#
# NOTE: FINRA retired the Pattern Day Trader rule on 2026-06-04 and Alpaca is
# removing daytrade_count from the API — the old 3-day-trade ration is gone.
# The bot sizes off CASH (never margin), so the new intraday-margin framework
# doesn't bind either.
LOSS_CAP_PCT     = 0.10
LOSS_CAP_FLOOR   = 20.00
MAX_INVESTED_PCT = 0.30   # TRADING sleeve: cap on signal-traded capital (trimmed 40->30: backtest showed the churn trails SPY)
HOLD_PCT         = 0.50   # HOLD sleeve: buy-and-keep core (raised 30->50: most index-like, lowest bleed)
HOLD_STOP        = 0.75   # hold exits at 75% of basis (-25% — thesis broken)
HOLD_TRAIL       = 0.60   # ...or at 60% of its peak once well in profit (locks 60% of best gain)
MAX_POS_PCT      = 0.10   # max 10% of equity in any single name (≥4 names = diversified)
SMALLCAP_POS_PCT = 0.05   # small/cheap names get HALF size (5%) — higher growth, higher blowup risk
MICRO_PX         = 2.00   # under this, gaps routinely blow past the -7% stop (audit wk1: realized
MICRO_POS_PCT    = 0.025  # stop-outs ran -14% to -25%), so QUARTER size — keeps access, caps the bleed
SPEND_CAP_PCT    = 0.25   # deploy at most 25% of cash per run (gradual, not all at once)
STOP_LOSS_PCT    = 0.93   # trading sleeve: hard stop at -7% from avg cost (overrides signals)
TAKE_PROFIT_PCT  = 1.15   # trading sleeve: bank +15% unless the signal still says buy (≈2:1 R:R)
RSI_ENTRY_MAX    = 78.0   # never open a NEW position into a blow-off top
HOLD_RSI_MAX     = 70.0   # hold-sleeve entries need a calmer entry than trades
MIN_ORDER_PCT    = 0.001  # skip dust orders under 0.1% of equity (min $1)
SMALL_PX         = 15.00  # live price under this sizes at the smallcap (half) cap
CHEAP_PX         = 5.00   # under this, use marketable LIMIT orders — thin names fill 5-20x worse at market
STOP_COOLDOWN_D  = 3      # days to sit out a name after its stop fired (no revenge re-entry)
TIME_STOP_DAYS   = 5      # trading position going nowhere for 5+ days with no signal = exit
CHEAP_HOLD_MAX   = 0.50   # sub-$5 names may fill at most half the HOLD sleeve (concentration cap)

# Crypto sleeve — spot, long-only, cash-only (no margin/futures). Brackets are
# wider than stocks because 5-10% daily swings are normal here.
CRYPTO_PCT       = 0.07   # crypto slice of the slot sleeve (trimmed 0.10->0.07 for the index-core restructure)
CRYPTO_POS_PCT   = 0.04   # max 4% of equity per coin (sleeve holds 2-3 coins max)
CRYPTO_STOP      = 0.85   # hard stop at -15% from avg cost
CRYPTO_TP        = 1.30   # bank +30% unless the signal still says buy (2:1 R:R)
CRYPTO_UNIVERSE  = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "SHIB/USD",
                    "LINK/USD", "AVAX/USD", "LTC/USD"]

# ── INDEX-CORE strategy (backtested 2026-06-24: best risk-adjusted mix tested) ───
# Replaces the old active liquid-momentum sleeves, which the backtest showed trail
# SPY with deeper drawdowns. Shape: ~80% held index + ~15% slot machines + ~5% cash.
INDEX_SYMBOL   = "SPY"    # buy-and-hold core: the index that beat the active strategy
INDEX_PCT      = 0.80     # target ~80% of equity in the core
SLOT_STOCK_PCT = 0.08     # speculative cheap/micro stock sleeve (the "slot machines")
SLOT_NAME_PCT  = 0.03     # max 3% of equity per slot name (sub-$2 micros get quartered)
SLOT_MAX_PX    = 15.00    # slots only buy names priced under this (cheap/speculative)

ET_TZ = ZoneInfo("America/New_York")   # DST-correct ET (the old UTC-4 broke every November)

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
# NYSE/NASDAQ full-closure holidays (observed dates). Hardcoded for verifiable
# correctness; refresh every couple of years (the weekly audit can top it up).
# Does NOT yet cover half-days (1pm early closes, e.g. day after Thanksgiving) —
# low-frequency, minor; a follow-up if it ever bites.
MARKET_HOLIDAYS = {
    "2026-01-01","2026-01-19","2026-02-16","2026-04-03","2026-05-25",
    "2026-06-19","2026-07-03","2026-09-07","2026-11-26","2026-12-25",
    "2027-01-01","2027-01-18","2027-02-15","2027-03-26","2027-05-31",
    "2027-06-18","2027-07-05","2027-09-06","2027-11-25","2027-12-24",
}

def check_market():
    et = datetime.now(ET_TZ)
    if et.weekday() >= 5:                            # weekend
        return False, et
    if et.strftime("%Y-%m-%d") in MARKET_HOLIDAYS:   # market holiday — skip like a weekend
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
    if len(closes) < 35: return None  # MACD slow EMA needs ≥26 bars + 9 for signal line
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
    price >= $0.10 — the practical floor for LISTED stocks (sub-penny names are OTC, untradeable on Alpaca; allows liquid sub-$1 movers — bought as WHOLE shares since
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
                if (px >= 0.10 and vol >= 500_000 and px*vol >= 5_000_000
                        and sym and "." not in sym and "-" not in sym and sym not in seen):
                    seen.add(sym); out.append(sym)
        except Exception:
            continue
    return out[:8]

def fetch_day_gainers():
    """Yahoo's whole-market day-gainers screener — momentum candidates from
    anywhere in the market, same quality rails as the smallcap screen."""
    try:
        url = ("https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
               "?formatted=false&scrIds=day_gainers&count=15")
        qs = requests.get(url, headers=YF_HEADERS, timeout=10).json()["finance"]["result"][0]["quotes"]
        qs = [q for q in qs if (q.get("regularMarketPrice",0) or 0) >= 0.25
              and (q.get("averageDailyVolume3Month",0) or 0) >= 500_000
              and "." not in q["symbol"] and "-" not in q["symbol"]]
        return [q["symbol"] for q in qs[:10]]
    except Exception: return []


# ── Alpaca broker layer (official REST API) ───────────────────────────────────
ALPACA_DATA = "https://data.alpaca.markets"

def alpaca_get(path):
    r = requests.get(ALPACA_BASE + path, headers=ALPACA_HDRS, timeout=15)
    return r.json()

def alpaca_data_get(path):
    r = requests.get(ALPACA_DATA + path, headers=ALPACA_HDRS, timeout=10)
    return r.json()

def fetch_market_movers():
    """ENTIRE-market momentum sweep: Alpaca's screener ranks every listed US
    equity by % change server-side (SIP data, resets at open). Top gainers join
    the candidate pool — the signal engine still has to vote each one in."""
    try:
        d = alpaca_data_get("/v1beta1/screener/stocks/movers?top=35")
        out = []
        for g in d.get("gainers", []):
            sym, px = g.get("symbol", ""), float(g.get("price") or 0)
            if px >= 0.10 and sym and all(c not in sym for c in "./-"):
                out.append(sym)
        return out[:25]
    except Exception: return []

def fetch_most_actives():
    """Highest share-volume names across the entire market (Alpaca screener)."""
    try:
        d = alpaca_data_get("/v1beta1/screener/stocks/most-actives?by=volume&top=15")
        return [a["symbol"] for a in d.get("most_actives", [])
                if a.get("symbol") and all(c not in a["symbol"] for c in "./-")][:10]
    except Exception: return []

def alpaca_bars_multi(symbols, days=90):
    """Daily OHLCV for the WHOLE universe in 1-2 batch calls via Alpaca's official
    data API (key-authed, real rate limits). This is the PRIMARY data source —
    Yahoo scraping gets 429-blocked from cloud IPs, and the stops/exits depend on
    this data, so the primary must be something we're entitled to. Returns
    {sym: (closes, volumes)}."""
    out = {}
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        base = ("/v2/stocks/bars?timeframe=1Day&adjustment=split&feed=iex&limit=10000"
                f"&start={start}&symbols=" + ",".join(symbols))
        token = None
        for _ in range(6):                      # paginate defensively
            d = alpaca_data_get(base + (f"&page_token={token}" if token else ""))
            for s, bars in (d.get("bars") or {}).items():
                c = [b["c"] for b in bars if b.get("c")]
                v = [b.get("v") or 0 for b in bars if b.get("c")]
                pc, pv = out.get(s, ([], []))
                out[s] = (pc + c, pv + v)
            token = d.get("next_page_token")
            if not token: break
    except Exception:
        pass
    return out

def alpaca_latest_multi(symbols):
    """Latest trade price for many symbols in one call. {sym: price}."""
    try:
        d = alpaca_data_get("/v2/stocks/trades/latest?feed=iex&symbols=" + ",".join(symbols))
        return {s: float(t.get("p") or 0) for s, t in (d.get("trades") or {}).items()
                if t.get("p")}
    except Exception:
        return {}

def crypto_data_get(path):
    """Crypto market data is PUBLIC — send no auth headers (invalid keys would
    401, and the data needs no entitlement)."""
    r = requests.get(ALPACA_DATA + path, timeout=10)
    return r.json()

def crypto_bars_multi(pairs, days=90):
    """Daily OHLCV for all crypto pairs in one call (v1beta3, free, no scraping).
    Returns {pair: (closes, volumes)} keyed by slash form, e.g. 'DOGE/USD'."""
    out = {}
    try:
        start = (datetime.now(timezone.utc) - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        base = ("/v1beta3/crypto/us/bars?timeframe=1Day&limit=10000"
                f"&start={start}&symbols=" + ",".join(pairs))
        token = None
        for _ in range(4):
            d = crypto_data_get(base + (f"&page_token={token}" if token else ""))
            for s, bars in (d.get("bars") or {}).items():
                c = [b["c"] for b in bars if b.get("c")]
                v = [b.get("v") or 0 for b in bars if b.get("c")]
                pc, pv = out.get(s, ([], []))
                out[s] = (pc + c, pv + v)
            token = d.get("next_page_token")
            if not token: break
    except Exception:
        pass
    return out

def crypto_latest_multi(pairs):
    """Latest crypto trade price per pair. {pair: price}."""
    try:
        d = crypto_data_get("/v1beta3/crypto/us/latest/trades?symbols=" + ",".join(pairs))
        return {s: float(t.get("p") or 0) for s, t in (d.get("trades") or {}).items()
                if t.get("p")}
    except Exception:
        return {}

def alpaca_account():
    """Returns (cash, equity, daily_pnl).
    We size off CASH — NOT Alpaca's margin buying power — so the bot never trades
    on leverage. daily_pnl = equity − last_equity (today's change, for the loss cap).
    daytrade_count is gone: FINRA retired the PDT rule 2026-06-04."""
    a = alpaca_get("/v2/account")
    cash = float(a.get("cash") or 0)
    eq   = float(a["equity"])
    leq  = float(a.get("last_equity") or eq)
    return cash, eq, eq - leq

def alpaca_open_orders():
    """Symbols with a pending (unfilled) order — skipped this run so a lingering
    limit order can't double-buy or double-sell."""
    try:
        return {o["symbol"] for o in alpaca_get("/v2/orders?status=open&limit=100")
                if o.get("symbol")}
    except Exception:
        return set()

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

def recent_stop_outs(days=STOP_COOLDOWN_D):
    """Symbols whose stop fired within the cooldown window — no revenge re-entry.
    Read from the tail of trade_log.jsonl (committed to the repo every run)."""
    latest = {}
    try:
        with open("trade_log.jsonl") as f:
            for line in f.readlines()[-400:]:
                try: d = json.loads(line)
                except Exception: continue
                if d.get("side") == "sell" and (d.get("stop_loss") or d.get("hold_stop")):
                    latest[d["symbol"]] = d.get("ts", "")
    except Exception:
        return set()
    cut = (datetime.now(ET_TZ) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    return {s for s, ts in latest.items() if ts >= cut}

def last_buy_dates(held_syms):
    """Most recent buy timestamp per held symbol, from the local trade log.
    Drives the time stop: an add resets the clock (renewed conviction)."""
    out = {}
    try:
        for line in open("trade_log.jsonl"):
            try: d = json.loads(line)
            except Exception: continue
            if d.get("side") == "buy" and d.get("symbol") in held_syms:
                out[d["symbol"]] = d.get("ts", "")
    except Exception:
        pass
    return out


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

def _px(p):
    """Limit-price rounding: sub-$1 names quote in 4 decimals, others in 2."""
    return round(p, 4 if p < 1 else 2)

def place_buy(sym, dollar_amount, live=None):
    """Buy with spread protection. Liquid fractionable names: MARKET notional
    (dollar amount). Cheap names (<$5, incl. all sub-$1): WHOLE-SHARE marketable
    LIMIT at live*1.02 — thin names fill 5-20x worse at market, so the limit caps
    slippage at ~2%. Non-fractionable names fall back to whole-share orders too."""
    a = alpaca_asset(sym)
    cheap = live is not None and live < CHEAP_PX
    if a and a.get("tradable") is False:
        r = {"message": f"{sym} not tradable on Alpaca"}
    elif a.get("fractionable", True) and not cheap:
        r = alpaca_order({"symbol": sym, "notional": str(round(dollar_amount, 2)),
                          "side": "buy", "type": "market", "time_in_force": "day"})
    elif live and live > 0 and int(dollar_amount // live) >= 1:
        payload = {"symbol": sym, "qty": str(int(dollar_amount // live)),
                   "side": "buy", "time_in_force": "day"}
        if cheap:
            payload.update({"type": "limit", "limit_price": str(_px(live * 1.02))})
        else:
            payload.update({"type": "market"})
        r = alpaca_order(payload)
    else:
        r = {"message": f"{sym} not fractionable; ${dollar_amount:.2f} buys <1 share"}
    if not _ok(r):
        print(f"    [buy rejected: {(r or {}).get('message', r)}]")
    return r

def place_crypto_buy(pair, dollar_amount):
    """Crypto MARKET buy by dollar amount. Crypto orders require tif=gtc/ioc."""
    r = alpaca_order({"symbol": pair, "notional": str(round(dollar_amount, 2)),
                      "side": "buy", "type": "market", "time_in_force": "gtc"})
    if not _ok(r):
        print(f"    [crypto buy rejected: {(r or {}).get('message', r)}]")
    return r

def place_crypto_sell(pair, qty):
    """Crypto MARKET sell of a held quantity (fractional fine)."""
    r = alpaca_order({"symbol": pair, "qty": str(qty),
                      "side": "sell", "type": "market", "time_in_force": "gtc"})
    if not _ok(r):
        print(f"    [crypto sell rejected: {(r or {}).get('message', r)}]")
    return r

def place_sell(sym, qty, live=None):
    """Sell a held quantity. Cheap names with whole-share qty sell via marketable
    LIMIT at live*0.98 (spread protection); fractional quantities must go MARKET
    (Alpaca restriction), as do liquid names where market fills are fine."""
    payload = {"symbol": sym, "qty": str(qty), "side": "sell", "time_in_force": "day"}
    whole = float(qty) == int(float(qty))
    if live is not None and live < CHEAP_PX and whole:
        payload.update({"type": "limit", "limit_price": str(_px(live * 0.98))})
    else:
        payload.update({"type": "market"})
    r = alpaca_order(payload)
    if not _ok(r):
        print(f"    [sell rejected: {(r or {}).get('message', r)}]")
    return r


# ── DEPRECATED: old active liquid-momentum strategy. Backtests showed it trails
# SPY with deeper drawdowns, so it was retired 2026-06-24 for run_bot() (index core
# + slot machines) further below. Kept for reference only; NOT called anywhere.
def _run_bot_legacy():
    open_, et = check_market()
    if not open_:
        print(f"Market closed ({et.strftime('%H:%M ET')}). Done.")
        return

    print(f"=== Alpaca bot run {et.strftime('%Y-%m-%d %H:%M ET')} | {MODE} ===")
    events     = []   # human-readable order outcomes (drives the email)
    trades_log = []   # structured per-trade context (appended to trade_log.jsonl)

    cash, equity, daily_pnl = alpaca_account()
    acct_tag    = str((alpaca_get("/v2/account") or {}).get("account_number", "????"))[-4:]  # tags each trade so an account swap can't silently corrupt the log/review
    max_pos     = equity * MAX_POS_PCT
    spend_cap   = max(0.0, cash) * SPEND_CAP_PCT        # ≤25% of CASH per run (no margin)
    low_cash    = cash < 5.00
    positions   = alpaca_positions()
    plan        = load_plan(et)
    pending     = alpaca_open_orders()                  # unfilled orders — skip those names
    cooldown    = recent_stop_outs()                    # stopped recently — no re-entry yet

    # Sleeve accounting: holds (buy-and-keep ledger) vs trading (everything else).
    holds       = load_holds()
    holds_dirty = False
    stale = [s for s in holds if s not in positions]    # sold manually / stopped out
    for s in stale: holds.pop(s); holds_dirty = True
    hold_val    = sum(positions[s]["mkt_val"] for s in holds)
    cheap_hold_val = sum(positions[s]["mkt_val"] for s in holds
                         if float(positions[s].get("avg_cost") or 99) < CHEAP_PX)
    crypto_flat = {p.replace("/", ""): p for p in CRYPTO_UNIVERSE}   # DOGEUSD -> DOGE/USD
    crypto_pos  = {s: p for s, p in positions.items() if s in crypto_flat}
    crypto_val  = sum(p["mkt_val"] for p in crypto_pos.values())
    invested    = max(0.0, equity - cash)               # total $ held in positions
    trade_val   = max(0.0, invested - hold_val - crypto_val)  # signal-traded stocks
    invest_room = max(0.0, equity * MAX_INVESTED_PCT - trade_val)  # trading-sleeve headroom
    hold_room   = max(0.0, equity * HOLD_PCT - hold_val)           # hold-sleeve headroom
    crypto_room = max(0.0, equity * CRYPTO_PCT - crypto_val)       # crypto-sleeve headroom
    spent = trade_spent = hold_spent = cheap_hold_spent = 0.0
    print(f"  Cash=${cash:.2f}  EQ=${equity:.2f}  dayP&L=${daily_pnl:.2f}  acct=…{acct_tag}")
    print(f"  Sleeves: trade ${trade_val:,.0f}/{equity*MAX_INVESTED_PCT:,.0f} (room ${invest_room:,.0f}) | "
          f"hold ${hold_val:,.0f}/{equity*HOLD_PCT:,.0f} (room ${hold_room:,.0f}) | "
          f"crypto ${crypto_val:,.0f}/{equity*CRYPTO_PCT:,.0f} | holds: {sorted(holds) or '—'}")
    if pending:  print(f"  PENDING orders (skipped this run): {sorted(pending)}")
    if cooldown: print(f"  STOP COOLDOWN ({STOP_COOLDOWN_D}d): {sorted(cooldown)}")
    print(f"  PLAN: {plan['regime']} | risk={plan['risk']} | avoid={sorted(plan['avoid'])} | {plan['notes'][:80]}")

    # Circuit breaker: a >10% down day blocks NEW buying — but exits (stops,
    # take-profits, signal sells) always run, so the bot can still de-risk.
    loss_cap = max(LOSS_CAP_FLOOR, equity * LOSS_CAP_PCT)
    halted   = daily_pnl <= -loss_cap
    if halted:
        print(f"⛔ Daily loss cap (dayP&L ${daily_pnl:.2f} <= -${loss_cap:.2f}) — buys OFF, exits still live.")

    # Universe — whole-market candidate sweep, then the signal engine votes.
    # (Crypto positions are excluded here; they have their own sleeve below.)
    universe     = {s for s in positions if s not in crypto_flat} | {"SPY"}
    meme_tickers = []
    small_caps   = set()
    movers_today = set()
    if low_cash:
        print("LOW_CASH — positions only.")
    else:
        wsb     = fetch_wsb()              # WallStreetBets chatter (meme bonus)
        smalls  = fetch_smallcaps()        # small-cap gainers/aggressive screens
        movers  = fetch_market_movers()    # Alpaca: top %-gainers across EVERY listed stock
        gainers = fetch_day_gainers()      # Yahoo: whole-market day gainers
        screen  = fetch_screener()         # Yahoo: most-active megacaps
        actives = fetch_most_actives()     # Alpaca: top volume across the whole market
        meme_tickers = wsb
        small_caps   = set(smalls)
        movers_today = set(movers)         # day-spike names: tradeable, but never HOLD entries
        # The screeners above each scan the ENTIRE market server-side (movers ranks
        # every listed US equity by % change, most-actives every stock by volume,
        # Yahoo screens sweep the whole market) — this cap is only how many top
        # candidates get the full 90-day indicator analysis per run. It rotates
        # every 15 minutes, so a full day deep-analyzes hundreds of distinct names.
        for s in plan["favor"] + wsb + smalls + movers + gainers + screen + actives:
            if len(universe) < 60: universe.add(s)
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

    # Market data — Alpaca official batch API first (2 calls for the whole
    # universe, no scraping-block roulette); Yahoo only as per-symbol fallback
    # for IEX coverage gaps. Exits depend on this data, so reliability is king.
    bars  = alpaca_bars_multi(universe)
    lasts = alpaca_latest_multi(universe)
    market, fails = {}, 0
    for sym in universe:
        c, v = bars.get(sym, (None, None))
        if not c or len(c) < 20:
            c, v = yf_ohlcv(sym)                 # fallback: Yahoo scrape
        live = lasts.get(sym) or yf_live(sym)
        if c and len(c) >= 20 and live:
            market[sym] = {"closes": c, "volumes": v, "live": live}
        else:
            fails += 1
    if fails:
        print(f"  [market data: {fails}/{len(universe)} symbols unavailable]")

    # Regime filter (practitioner staple): SPY under its 50-day SMA = weak tape —
    # no NEW hold-sleeve entries (multi-day risk needs a supportive market).
    spy_c   = market.get("SPY", {}).get("closes") or []
    risk_on = len(spy_c) >= 50 and spy_c[-1] > sum(spy_c[-50:]) / 50
    print(f"  REGIME: {'risk-on (SPY>SMA50)' if risk_on else 'risk-off (SPY<SMA50) — new holds disabled'}")

    # Signals
    sigs = {}
    for sym, d in market.items():
        rr = compute_signals(sym, d["closes"], d["volumes"], d["live"], meme_tickers)
        if rr: sigs[sym] = rr

    sold_now = set()   # exits this run — never re-buy the same name the same run

    def _exit(sym, qty, live, tag, extra, sig=None):
        """Shared exit path: place the sell, book cash, log with context."""
        nonlocal cash, low_cash
        print(f"{tag} {sym} qty={qty} {extra}")
        try:
            r = place_sell(sym, qty, live)
            if _ok(r):
                print(f"  → placed {r['id']}")
                cash += qty * live; low_cash = False; sold_now.add(sym)
                events.append(f"{tag} {sym} qty={qty} {extra} → PLACED ({r['id']})")
                entry = {"ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag,
                         "symbol": sym, "side": "sell", "qty": qty, "order_id": r["id"],
                         "live": round(live, 2), "vix": round(vix, 1)}
                if tag == "STOP-LOSS":   entry["stop_loss"]   = True
                if tag == "TAKE-PROFIT": entry["take_profit"] = True
                if tag == "HOLD-STOP":   entry["hold_stop"]   = True
                if tag == "TIME-STOP":   entry["time_stop"]   = True
                if sig: entry.update({"rsi": round(sig["rsi"], 1), "trend": sig["trend"],
                                      "consensus": sig["consensus"], "sells": sig.get("sells")})
                trades_log.append(entry)
                return True
            events.append(f"{tag} {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"{tag} {sym} → ERROR: {e}")
        return False

    # 1) BRACKET exits (trading sleeve): hard stop -7% — risk is cut no matter
    #    what the signals say; take-profit +15% banked unless the signal is still
    #    an active buy (let confirmed winners run). ~2:1 reward:risk. Plus a TIME
    #    stop: a position going nowhere for 5+ days with no signal is dead money.
    buy_dates = last_buy_dates(set(positions) - set(holds))
    for sym, p in list(positions.items()):
        if sym in holds or sym in pending or sym in crypto_flat: continue
        live = market.get(sym, {}).get("live"); cost = float(p.get("avg_cost") or 0)
        if not live or cost <= 0 or p["qty"] <= 0: continue
        con = sigs.get(sym, {}).get("consensus", 0)
        age_d = None
        if buy_dates.get(sym):
            try:
                age_d = (et - datetime.strptime(buy_dates[sym][:10], "%Y-%m-%d")
                         .replace(tzinfo=ET_TZ)).days
            except Exception:
                age_d = None
        if live <= cost * STOP_LOSS_PCT:
            _exit(sym, p["qty"], live, "STOP-LOSS",
                  f"({(live/cost-1)*100:+.1f}% from ${cost:.2f})", sigs.get(sym))
        elif live >= cost * TAKE_PROFIT_PCT and con <= 0:
            _exit(sym, p["qty"], live, "TAKE-PROFIT",
                  f"({(live/cost-1)*100:+.1f}% from ${cost:.2f})", sigs.get(sym))
        elif (age_d is not None and age_d >= TIME_STOP_DAYS
              and con <= 0 and live < cost * 1.02):
            _exit(sym, p["qty"], live, "TIME-STOP",
                  f"({(live/cost-1)*100:+.1f}% after {age_d}d, no signal — dead money)",
                  sigs.get(sym))

    # 2) SIGNAL sells (frees buying power). Hold-sleeve names are exempt.
    for sym, sig in sigs.items():
        if sig["consensus"] != -1 or sym not in positions: continue
        if sym in sold_now or sym in pending: continue
        if sym in holds:
            print(f"  KEEP {sym} (hold sleeve — sell signal ignored)"); continue
        qty = positions[sym]["qty"]
        if qty <= 0: continue
        _exit(sym, qty, market[sym]["live"], "SELL", f"RSI={sig['rsi']:.1f}", sig)

    # 3) HOLD stops: exit a hold at -25% from basis (thesis broken), OR once well
    #    in profit, if it gives back 40% from its peak (ratchet — a +200% winner
    #    can't round-trip to a loss). Peaks persist in holds.json.
    for sym in list(holds):
        if sym not in positions or sym in pending or sym in sold_now: continue
        live = market.get(sym, {}).get("live")
        h = holds[sym]
        # Broker's avg_entry_price is the true blended basis (our ledger's is an
        # estimate from order-time prices) — prefer it when available.
        basis = float(positions[sym].get("avg_cost") or 0) or float(h.get("basis") or 0)
        if not live or basis <= 0: continue
        peak = max(float(h.get("peak") or 0), live)
        if peak > float(h.get("peak") or 0):
            h["peak"] = round(peak, 4)
            if peak >= float(h.get("peak_saved") or basis) * 1.05:
                h["peak_saved"] = round(peak, 4); holds_dirty = True
        floor_ = max(basis * HOLD_STOP, peak * HOLD_TRAIL)
        if live > floor_: continue
        why = ("-25% from basis" if floor_ == basis * HOLD_STOP
               else f"gave back 40% from peak ${peak:.2f}")
        if _exit(sym, positions[sym]["qty"], live, "HOLD-STOP",
                 f"(live ${live:.2f} <= ${floor_:.2f}: {why})"):
            holds.pop(sym); holds_dirty = True

    # BUY — new entries AND adds to held winners, up to the per-name cap.
    # Cheap/small names size at SMALLCAP_POS_PCT (half) — growthier but blowup-prone.
    # Sleeve routing: STRONG signals (4+ buy votes AND uptrend) buy into the HOLD
    # sleeve (kept until a stop); everything else is a trading-sleeve buy.
    if not low_cash and not halted:
        for sym, sig in sigs.items():
            if sig["consensus"] != 1: continue
            if sym in sold_now or sym in pending: continue    # just exited / order in flight
            if sym in cooldown:
                print(f"  SKIP {sym} (stop-loss cooldown)"); continue
            if sym in plan["avoid"]:
                print(f"  SKIP {sym} (plan avoid-list)"); continue
            if spent >= spend_cap: break                      # per-run pacing cap reached
            if (invest_room - trade_spent) < 1.00 and (hold_room - hold_spent) < 1.00:
                break                                         # both sleeves full
            # A strong signal can top up a held name, but never past its per-name cap.
            held_value = 0.0
            if sym in positions and positions[sym]["qty"] > 0 and sym in market:
                held_value = positions[sym]["qty"] * market[sym]["live"]
            if held_value == 0 and sig["rsi"] > RSI_ENTRY_MAX:
                print(f"  SKIP {sym} (RSI {sig['rsi']:.0f} > {RSI_ENTRY_MAX:.0f} — blow-off chase guard)"); continue
            live     = market[sym]["live"]
            is_micro = live < MICRO_PX                          # sub-$2: gappy, stops unreliable → quarter size
            is_small = sym in small_caps or live < SMALL_PX     # cheap names = half size
            # NEVER average down: adds only pyramid into strength (live above the
            # position's own basis). Adding to a faller turns one bad entry into a
            # max-size bad position — the classic microcap-pump account killer.
            if held_value > 0:
                ref = (float(positions[sym].get("avg_cost") or 0)
                       or (float(holds[sym].get("basis") or 0) if sym in holds else 0))
                if ref > 0 and live < ref * 1.02:
                    print(f"  SKIP {sym} add (live ${live:.4g} ≤ basis ${ref:.4g}+2% — no averaging down)")
                    continue
                if sym in holds and sig["rsi"] > HOLD_RSI_MAX:
                    print(f"  SKIP {sym} hold-add (RSI {sig['rsi']:.0f} > {HOLD_RSI_MAX:.0f})")
                    continue
            # Hold entries demand QUALITY, not just strength: 4+ votes in an uptrend,
            # a calm entry (RSI<=70), and never a daily-spike movers name — those are
            # trade material, not buy-and-hold material (pump risk).
            strong   = (sig["buys"] >= 4 and sig["trend"] == "up"
                        and sig["rsi"] <= HOLD_RSI_MAX and sym not in movers_today
                        and risk_on)               # no NEW holds into a weak tape
            use_hold = (sym in holds) or (strong and sym not in positions
                                          and (hold_room - hold_spent) >= 1.00)
            # Concentration cap: sub-$5 names may fill at most half the hold sleeve.
            if use_hold and live < CHEAP_PX:
                cheap_cap = equity * HOLD_PCT * CHEAP_HOLD_MAX
                if cheap_hold_val + cheap_hold_spent >= cheap_cap:
                    print(f"  SKIP {sym} hold (sub-$5 holds at their {CHEAP_HOLD_MAX:.0%} sleeve cap)")
                    continue
            sleeve_room  = (hold_room - hold_spent) if use_hold else (invest_room - trade_spent)
            name_cap     = equity * (MICRO_POS_PCT if is_micro else
                                     SMALLCAP_POS_PCT if is_small else MAX_POS_PCT)
            room_in_name = name_cap - held_value
            if room_in_name < 1.00: continue
            amount = min(name_cap * vix_scale * plan["risk"], room_in_name, cash * 0.95,
                         spend_cap - spent, sleeve_room)
            if amount < max(1.00, equity * MIN_ORDER_PCT): continue   # no dust orders
            if held_value > 0 and amount < name_cap * 0.20:
                continue   # near its cap — skip dribble top-ups every run
            verb = (("HOLD-ADD" if held_value > 0 else "HOLD-BUY") if use_hold
                    else ("ADD" if held_value > 0 else "BUY"))
            print(f"{verb} {sym} ${amount:.2f} RSI={sig['rsi']:.1f}"
                  + (" [micro]" if is_micro else " [smallcap]" if is_small else ""))
            try:
                r = place_buy(sym, amount, live)
                if _ok(r):
                    # Whole-share orders may spend slightly less than requested.
                    actual = amount
                    if r.get("qty") and not r.get("notional"):
                        actual = round(float(r["qty"]) * live, 2)
                    print(f"  → placed {r['id']} (${actual:.2f})")
                    cash -= actual; spent += actual
                    if use_hold:
                        hold_spent += actual
                        if live < CHEAP_PX: cheap_hold_spent += actual
                    else:
                        trade_spent += actual
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
                        "side": "buy", "add": held_value > 0, "smallcap": is_small,
                        "hold": use_hold, "notional": round(actual, 2), "order_id": r["id"],
                        "live": round(live, 2), "rsi": round(sig["rsi"], 1),
                        "trend": sig["trend"], "consensus": sig["consensus"],
                        "delta": round(sig["delta"], 4), "buys": sig["buys"],
                        "macd_up": sig["macd_up"], "meme": sig["meme"], "vix": round(vix, 1)})
                else:
                    events.append(f"{verb} {sym} ${amount:.2f} → REJECTED: {(r or {}).get('message', r)}")
            except Exception as e:
                events.append(f"{verb} {sym} ${amount:.2f} → ERROR: {e}")

    # ── CRYPTO sleeve (10%): DOGE-style moonshots, same discipline, wider brackets.
    # Spot only, cash only, no averaging down. Crypto trades 24/7 but is managed on
    # the bot's market-hours schedule; the -15% stop covers overnight gaps at exit.
    try:
        cbars  = crypto_bars_multi(CRYPTO_UNIVERSE)
        clasts = crypto_latest_multi(CRYPTO_UNIVERSE)
        csigs  = {}
        for pair in CRYPTO_UNIVERSE:
            c, v = cbars.get(pair, (None, None))
            live = clasts.get(pair)
            if c and len(c) >= 20 and live:
                rr = compute_signals(pair, c, v, live, [])
                if rr: csigs[pair] = rr

        # Exits first: -15% stop (always) / +30% take-profit (unless still a buy).
        for flat, p in list(crypto_pos.items()):
            pair = crypto_flat[flat]
            live = clasts.get(pair); cost = float(p.get("avg_cost") or 0)
            if flat in pending or not live or cost <= 0 or p["qty"] <= 0: continue
            con = csigs.get(pair, {}).get("consensus", 0)
            tag = None
            if live <= cost * CRYPTO_STOP:                 tag = "CRYPTO-STOP"
            elif live >= cost * CRYPTO_TP and con <= 0:    tag = "CRYPTO-TP"
            if not tag: continue
            print(f"{tag} {pair} qty={p['qty']} ({(live/cost-1)*100:+.1f}% from ${cost:.6g})")
            try:
                r = place_crypto_sell(pair, p["qty"])
                if _ok(r):
                    print(f"  → placed {r['id']}")
                    cash += p["qty"] * live
                    events.append(f"{tag} {pair} ({(live/cost-1)*100:+.1f}%) → PLACED ({r['id']})")
                    trades_log.append({
                        "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag,
                        "symbol": pair, "side": "sell", "crypto": True, "qty": p["qty"],
                        "stop_loss": tag == "CRYPTO-STOP", "take_profit": tag == "CRYPTO-TP",
                        "order_id": r["id"], "live": live, "vix": round(vix, 1)})
                else:
                    events.append(f"{tag} {pair} → REJECTED: {(r or {}).get('message', r)}")
            except Exception as e:
                events.append(f"{tag} {pair} → ERROR: {e}")

        # Entries: +1 consensus, not already held, no blow-off chasing, sleeve+coin caps.
        if not low_cash and not halted:
            crypto_spent = 0.0
            for pair, sig in csigs.items():
                flat = pair.replace("/", "")
                if sig["consensus"] != 1 or flat in crypto_pos or flat in pending: continue
                if sig["rsi"] > RSI_ENTRY_MAX:
                    print(f"  SKIP {pair} (RSI {sig['rsi']:.0f} — blow-off chase guard)"); continue
                room = crypto_room - crypto_spent
                amount = min(equity * CRYPTO_POS_PCT, room, cash * 0.95, spend_cap - spent)
                if amount < max(1.00, equity * MIN_ORDER_PCT): continue
                print(f"CRYPTO-BUY {pair} ${amount:.2f} RSI={sig['rsi']:.1f}")
                try:
                    r = place_crypto_buy(pair, amount)
                    if _ok(r):
                        print(f"  → placed {r['id']}")
                        cash -= amount; spent += amount; crypto_spent += amount
                        events.append(f"CRYPTO-BUY {pair} ${amount:.2f} → PLACED ({r['id']})")
                        trades_log.append({
                            "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag,
                            "symbol": pair, "side": "buy", "crypto": True,
                            "notional": round(amount, 2), "order_id": r["id"], "live": sig and clasts.get(pair),
                            "rsi": round(sig["rsi"], 1), "trend": sig["trend"],
                            "consensus": sig["consensus"], "buys": sig["buys"],
                            "macd_up": sig["macd_up"], "vix": round(vix, 1)})
                    else:
                        events.append(f"CRYPTO-BUY {pair} ${amount:.2f} → REJECTED: {(r or {}).get('message', r)}")
                except Exception as e:
                    events.append(f"CRYPTO-BUY {pair} ${amount:.2f} → ERROR: {e}")
        if csigs:
            cline = [f"{p} {s['consensus']:+d} RSI={s['rsi']:.0f}" for p, s in csigs.items() if s["consensus"] != 0]
            print(f"  CRYPTO signals: {', '.join(cline) if cline else '(all neutral)'}")
    except Exception as e:
        print(f"  [crypto sleeve error (stocks unaffected): {e}]")

    # Summary
    print(f"\n--- {MODE} | {et.strftime('%H:%M ET')} | VIX={vix:.1f} | "
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
                f"dayP&L ${daily_pnl:.2f}",
                f"Sleeves: trade ${trade_val:,.0f}/{equity*MAX_INVESTED_PCT:,.0f} | "
                f"hold ${hold_val:,.0f}/{equity*HOLD_PCT:,.0f} ({len(holds)} holds) | "
                f"crypto ${crypto_val:,.0f}/{equity*CRYPTO_PCT:,.0f}", ""]
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


# ── Slot-stock ledger: which positions are speculative slots (vs the index core) ─
def load_slots():
    try:    return json.load(open("slots.json"))
    except Exception: return {}
def save_slots(s):
    with open("slots.json", "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


# ── LIVE STRATEGY: index core + slot machines ───────────────────────────────────
# ~80% buy-and-hold SPY (the part that wins), ~8% cheap/micro stock "slots" and ~7%
# crypto (the lottery tickets, hard-stopped), ~5% cash. Old momentum holds are sold
# off to rotate into the index core. Cash-only, no leverage. Buys are paced and never
# spend below the 5% cash floor (so unsettled sale proceeds aren't over-deployed).
def run_bot():
    open_, et = check_market()
    if not open_:
        print(f"Market closed ({et.strftime('%H:%M ET')}). Done.")
        return
    print(f"=== Alpaca bot run {et.strftime('%Y-%m-%d %H:%M ET')} | {MODE} | INDEX-CORE ===")
    events, trades_log = [], []
    cash, equity, daily_pnl = alpaca_account()
    acct_tag  = str((alpaca_get("/v2/account") or {}).get("account_number", "????"))[-4:]
    positions = alpaca_positions()
    pending   = alpaca_open_orders()
    plan      = load_plan(et)
    vix       = yf_vix()
    cooldown  = recent_stop_outs()
    slots     = load_slots()
    slots_dirty = False

    def log_trade(d):
        d.update({"ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "acct": acct_tag, "vix": round(vix, 1)})
        trades_log.append(d)

    crypto_flat = {p.replace("/", ""): p for p in CRYPTO_UNIVERSE}
    index_val  = positions.get(INDEX_SYMBOL, {}).get("mkt_val", 0.0)
    crypto_pos = {s: p for s, p in positions.items() if s in crypto_flat}
    crypto_val = sum(p["mkt_val"] for p in crypto_pos.values())
    slot_pos   = {s: p for s, p in positions.items()
                  if s in slots and s not in crypto_flat and s != INDEX_SYMBOL}
    slot_val   = sum(p["mkt_val"] for p in slot_pos.values())
    legacy_pos = {s: p for s, p in positions.items()
                  if s != INDEX_SYMBOL and s not in crypto_flat and s not in slots}
    legacy_val = sum(p["mkt_val"] for p in legacy_pos.values())

    print(f"  Cash=${cash:.2f}  EQ=${equity:.2f}  dayP&L=${daily_pnl:.2f}  acct=…{acct_tag}  VIX={vix:.1f}")
    print(f"  INDEX {INDEX_SYMBOL} ${index_val:,.0f}/{equity*INDEX_PCT:,.0f} | "
          f"slots ${slot_val:,.0f}/{equity*SLOT_STOCK_PCT:,.0f} | "
          f"crypto ${crypto_val:,.0f}/{equity*CRYPTO_PCT:,.0f} | "
          f"legacy ${legacy_val:,.0f} ({len(legacy_pos)})")

    loss_cap = max(LOSS_CAP_FLOOR, equity * LOSS_CAP_PCT)
    halted   = daily_pnl <= -loss_cap or vix > 35
    if halted:
        print(f"⛔ Buys OFF (dayP&L ${daily_pnl:.0f} or VIX {vix:.0f}). Exits still run.")

    # ===== EXITS (always run, even halted) =====
    # 1) Legacy liquidation: sell old momentum holds to rotate into the index core.
    for sym, p in list(legacy_pos.items()):
        if sym in pending or p["qty"] <= 0: continue
        live = yf_live(sym) or (p["mkt_val"] / p["qty"] if p["qty"] else 0)
        if not live: continue
        print(f"LEGACY-SELL {sym} qty={p['qty']}")
        try:
            r = place_sell(sym, p["qty"], live)
            if _ok(r):
                events.append(f"LEGACY-SELL {sym} → PLACED ({r['id']})")
                log_trade({"symbol": sym, "side": "sell", "legacy": True,
                           "qty": p["qty"], "live": round(live, 4), "order_id": r["id"]})
            else:
                events.append(f"LEGACY-SELL {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"LEGACY-SELL {sym} → ERROR: {e}")

    # 2) Slot-stock brackets: hard -7% stop / +15% take-profit.
    for sym, p in list(slot_pos.items()):
        if sym in pending or p["qty"] <= 0: continue
        live = yf_live(sym); cost = float(p.get("avg_cost") or 0)
        if not live or cost <= 0: continue
        tag = ("STOP-LOSS" if live <= cost*STOP_LOSS_PCT
               else "TAKE-PROFIT" if live >= cost*TAKE_PROFIT_PCT else None)
        if not tag: continue
        print(f"SLOT-{tag} {sym} qty={p['qty']} ({(live/cost-1)*100:+.1f}%)")
        try:
            r = place_sell(sym, p["qty"], live)
            if _ok(r):
                slots.pop(sym, None); slots_dirty = True
                events.append(f"SLOT-{tag} {sym} ({(live/cost-1)*100:+.1f}%) → PLACED ({r['id']})")
                log_trade({"symbol": sym, "side": "sell", "slot": True, "qty": p["qty"],
                           "live": round(live, 4), "stop_loss": tag == "STOP-LOSS",
                           "take_profit": tag == "TAKE-PROFIT", "order_id": r["id"]})
            else:
                events.append(f"SLOT-{tag} {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"SLOT-{tag} {sym} → ERROR: {e}")

    # 3) Crypto signals + brackets (-15% stop / +30% take-profit).
    clasts = crypto_latest_multi(CRYPTO_UNIVERSE)
    cbars  = crypto_bars_multi(CRYPTO_UNIVERSE)
    csigs  = {}
    for pair in CRYPTO_UNIVERSE:
        c, v = cbars.get(pair, (None, None)); live = clasts.get(pair)
        if c and len(c) >= 35 and live:
            rr = compute_signals(pair, c, v, live, [])
            if rr: csigs[pair] = rr
    for flat, p in list(crypto_pos.items()):
        pair = crypto_flat[flat]; live = clasts.get(pair); cost = float(p.get("avg_cost") or 0)
        if flat in pending or not live or cost <= 0 or p["qty"] <= 0: continue
        con = csigs.get(pair, {}).get("consensus", 0)
        tag = ("CRYPTO-STOP" if live <= cost*CRYPTO_STOP
               else "CRYPTO-TP" if (live >= cost*CRYPTO_TP and con <= 0) else None)
        if not tag: continue
        print(f"{tag} {pair} qty={p['qty']} ({(live/cost-1)*100:+.1f}%)")
        try:
            r = place_crypto_sell(pair, p["qty"])
            if _ok(r):
                events.append(f"{tag} {pair} ({(live/cost-1)*100:+.1f}%) → PLACED ({r['id']})")
                log_trade({"symbol": pair, "side": "sell", "crypto": True, "qty": p["qty"],
                           "live": live, "stop_loss": tag == "CRYPTO-STOP",
                           "take_profit": tag == "CRYPTO-TP", "order_id": r["id"]})
            else:
                events.append(f"{tag} {pair} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"{tag} {pair} → ERROR: {e}")

    # ===== BUYS (skip if halted). Budget = min(25%/run pacing, cash above the 5%
    # floor). 'spent' accrues across index+slots+crypto so we never over-deploy. =====
    cash_floor = equity * 0.05
    budget = min(max(0.0, cash) * SPEND_CAP_PCT, max(0.0, cash - cash_floor))
    spent  = 0.0
    if not halted and budget >= max(1.0, equity*MIN_ORDER_PCT):
        # 1) INDEX core top-up toward 80% (priority).
        index_room = equity*INDEX_PCT - index_val
        live = yf_live(INDEX_SYMBOL) or alpaca_latest_multi([INDEX_SYMBOL]).get(INDEX_SYMBOL)
        if index_room > equity*0.01 and live:
            amt = min(index_room, budget - spent)
            if amt >= max(1.0, equity*MIN_ORDER_PCT):
                print(f"INDEX-BUY {INDEX_SYMBOL} ${amt:.2f}")
                try:
                    r = place_buy(INDEX_SYMBOL, amt, live)
                    if _ok(r):
                        spent += amt
                        events.append(f"INDEX-BUY {INDEX_SYMBOL} ${amt:.2f} → PLACED ({r['id']})")
                        log_trade({"symbol": INDEX_SYMBOL, "side": "buy", "index": True,
                                   "notional": round(amt, 2), "live": round(live, 2), "order_id": r["id"]})
                    else:
                        events.append(f"INDEX-BUY {INDEX_SYMBOL} → REJECTED: {(r or {}).get('message', r)}")
                except Exception as e:
                    events.append(f"INDEX-BUY {INDEX_SYMBOL} → ERROR: {e}")

        # 2) SLOT stocks: cheap momentum lottery tickets, capped per-name and per-sleeve.
        slot_room = equity*SLOT_STOCK_PCT - slot_val
        if slot_room > equity*0.005 and (budget - spent) >= max(1.0, equity*MIN_ORDER_PCT):
            uni = set()
            for s in fetch_smallcaps() + fetch_market_movers() + fetch_day_gainers():
                if len(uni) < 30: uni.add(s)
            uni -= set(positions) | pending | cooldown | set(plan["avoid"])
            uni = list(uni)
            bars = alpaca_bars_multi(uni); lasts = alpaca_latest_multi(uni)
            slot_spent = 0.0
            for sym in uni:
                if slot_spent >= slot_room or spent >= budget: break
                c, v = bars.get(sym, (None, None))
                if not c or len(c) < 35: c, v = yf_ohlcv(sym)
                live = lasts.get(sym) or yf_live(sym)
                if not c or len(c) < 35 or not live or live > SLOT_MAX_PX or live < 0.10: continue
                rr = compute_signals(sym, c, v, live, [])
                if not rr or rr["consensus"] != 1 or rr["rsi"] > RSI_ENTRY_MAX: continue
                name_cap = equity * (MICRO_POS_PCT if live < MICRO_PX else SLOT_NAME_PCT)
                amt = min(name_cap, slot_room - slot_spent, budget - spent)
                if amt < max(1.0, equity*MIN_ORDER_PCT): continue
                print(f"SLOT-BUY {sym} ${amt:.2f} @${live:.4g} RSI={rr['rsi']:.0f}")
                try:
                    r = place_buy(sym, amt, live)
                    if _ok(r):
                        actual = round(float(r["qty"])*live, 2) if (r.get("qty") and not r.get("notional")) else amt
                        spent += actual; slot_spent += actual
                        slots[sym] = {"ts": et.strftime("%Y-%m-%dT%H:%M"), "basis": round(live, 4)}
                        slots_dirty = True
                        events.append(f"SLOT-BUY {sym} ${actual:.2f} → PLACED ({r['id']})")
                        log_trade({"symbol": sym, "side": "buy", "slot": True, "notional": round(actual, 2),
                                   "live": round(live, 4), "rsi": round(rr["rsi"], 1),
                                   "trend": rr["trend"], "order_id": r["id"]})
                    else:
                        events.append(f"SLOT-BUY {sym} → REJECTED: {(r or {}).get('message', r)}")
                except Exception as e:
                    events.append(f"SLOT-BUY {sym} → ERROR: {e}")

        # 3) CRYPTO slots: +1 signal, not held, no blow-off chase, capped per coin/sleeve.
        crypto_room = equity*CRYPTO_PCT - crypto_val
        cspent = 0.0
        for pair, sig in csigs.items():
            if spent >= budget or cspent >= crypto_room: break
            flat = pair.replace("/", "")
            if sig["consensus"] != 1 or flat in crypto_pos or flat in pending: continue
            if sig["rsi"] > RSI_ENTRY_MAX: continue
            amt = min(equity*CRYPTO_POS_PCT, crypto_room - cspent, budget - spent)
            if amt < max(1.0, equity*MIN_ORDER_PCT): continue
            print(f"CRYPTO-BUY {pair} ${amt:.2f} RSI={sig['rsi']:.0f}")
            try:
                r = place_crypto_buy(pair, amt)
                if _ok(r):
                    spent += amt; cspent += amt
                    events.append(f"CRYPTO-BUY {pair} ${amt:.2f} → PLACED ({r['id']})")
                    log_trade({"symbol": pair, "side": "buy", "crypto": True, "notional": round(amt, 2),
                               "live": clasts.get(pair), "rsi": round(sig["rsi"], 1), "order_id": r["id"]})
                else:
                    events.append(f"CRYPTO-BUY {pair} → REJECTED: {(r or {}).get('message', r)}")
            except Exception as e:
                events.append(f"CRYPTO-BUY {pair} → ERROR: {e}")

    # ===== summary / email / persist =====
    print(f"\n--- {MODE} | {et.strftime('%H:%M ET')} | VIX={vix:.1f} | dayP&L=${daily_pnl:.2f} | Cash=${cash:.2f} ---")
    cl = [f"{p.split('/')[0]} {s['consensus']:+d}" for p, s in csigs.items() if s["consensus"] != 0]
    print(f"  CRYPTO signals: {', '.join(cl) if cl else '(all neutral)'}")

    morning = (et.hour == 9 and et.minute >= 45)
    if events or morning:
        body = [f"Alpaca bot ({MODE})  {et.strftime('%Y-%m-%d %H:%M ET')}  [INDEX-CORE]",
                f"Plan: {plan['regime']} | risk {plan['risk']}",
                f"Equity ${equity:.2f} | Cash ${cash:.2f} | dayP&L ${daily_pnl:.2f}",
                f"Index ${index_val:,.0f}/{equity*INDEX_PCT:,.0f} | "
                f"slots ${slot_val:,.0f}/{equity*SLOT_STOCK_PCT:,.0f} | "
                f"crypto ${crypto_val:,.0f}/{equity*CRYPTO_PCT:,.0f} | legacy ${legacy_val:,.0f}", ""]
        body.append("ORDERS THIS RUN:" if events else "No orders this run.")
        body += [f"  • {e}" for e in events]
        if   any("PLACED" in e for e in events):                 subject = f"✅ Alpaca bot ({MODE}) — ORDER PLACED"
        elif any(("REJECTED" in e or "ERROR" in e) for e in events): subject = f"⚠️ Alpaca bot ({MODE}) — order rejected"
        else:                                                    subject = f"Alpaca bot ({MODE}) — morning status"
        send_email(subject, "\n".join(body))

    if trades_log:
        with open("trade_log.jsonl", "a") as f:
            for t in trades_log:
                f.write(json.dumps(t) + "\n")
        print(f"  [logged {len(trades_log)} trade(s) to trade_log.jsonl]")
    if slots_dirty:
        save_slots(slots)
        print(f"  [slots.json updated — {len(slots)} slot(s)]")


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
        cash_, eq, dpnl = alpaca_account()
        print(f"Account OK: Cash=${cash_:.2f}  EQ=${eq:.2f}")
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
