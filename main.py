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

# Email alerts — sent from GitHub's cloud so they arrive even if your PC is asleep.
# Sender = dedicated dummy account; recipient = your personal inbox.
# Only GMAIL_APP_PASSWORD (the dummy account's App Password) is a secret.
# Both addresses are overridable via env (ALERT_TO / GMAIL_USER) if ever needed.
ALERT_TO     = os.environ.get("ALERT_TO",   "devondavasher@gmail.com")  # you receive
GMAIL_USER   = os.environ.get("GMAIL_USER", "devonsdummy@gmail.com")    # bot sends as
GMAIL_APP_PW = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_email(subject, body):
    """Best-effort Gmail alert. Never raises — a mail failure must not break a
    trading run. No-op if GMAIL_APP_PASSWORD isn't configured as a GitHub secret."""
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




# Versions to try for order placement, newest first.
# robin_stocks 2.1.0 ships 1.431.4 which is too old for orders.
# 1.432.0 was confirmed accepted during market hours.
_ORDER_VERSION = "1.432.0"


def _order_post(payload):
    """
    POST a form-encoded order at the API version that accepts orders.
    Returns the parsed JSON (an order dict on success, or an error dict with
    a 'detail' field). Never raises on an HTTP/JSON error — surfaces it instead.
    """
    orig = rh.helper.SESSION.headers.get("X-Robinhood-API-Version", "")
    try:
        rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": _ORDER_VERSION})
        resp = rh.helper.SESSION.post("https://api.robinhood.com/orders/", data=payload)
        try:
            return resp.json()
        except Exception:
            return {"detail": f"non-JSON {resp.status_code}: {resp.text[:200]}"}
    finally:
        rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": orig})


def _ok(result):
    """Robinhood accepted the order iff it echoes an id and has no error detail."""
    return bool(result) and result.get("id") and not result.get("detail")


def _base_order(sym, instrument_url, side):
    """Common fields. market_hours + extended_hours are what robin_stocks sends on
    every fractional order and were MISSING from the prior hand-rolled payload —
    the most likely cause of the persistent rejections."""
    return {
        "account":        ACCOUNT_URL,
        "instrument":     instrument_url,
        "symbol":         sym,
        "time_in_force":  "gfd",
        "trigger":        "immediate",
        "side":           side,
        "market_hours":   "regular_hours",
        "extended_hours": "false",
        "ref_id":         str(uuid.uuid4()),
    }


def place_buy(sym, dollar_amount, live_price):
    """
    Fractional buy locked to ACCOUNT. Two attempts, first success wins:
      1) market order with an upward price collar (how robin_stocks frames it)
      2) marketable limit a hair above live
    Every Robinhood response is logged so a rejection tomorrow is diagnosable.
    """
    instruments = rh.stocks.get_instruments_by_symbols(sym, info="url")
    if not instruments:
        raise ValueError(f"No instrument found for {sym}")
    inst   = instruments[0]
    shares = round(dollar_amount / live_price, 6)
    if shares < 0.000001:
        raise ValueError(f"Amount too small for {sym}")

    # Attempt 1 — market + collar price
    p1 = _base_order(sym, inst, "buy")
    p1.update({"type": "market", "quantity": str(shares),
               "price": str(round(live_price * 1.01, 2))})
    r1 = _order_post(p1)
    if _ok(r1):
        return r1
    print(f"    [buy A1 market rejected: {(r1 or {}).get('detail', r1)}]")

    # Attempt 2 — marketable limit
    p2 = _base_order(sym, inst, "buy")
    p2.update({"type": "limit", "quantity": str(shares),
               "price": str(round(live_price * 1.005, 2))})
    r2 = _order_post(p2)
    if _ok(r2):
        return r2
    print(f"    [buy A2 limit rejected: {(r2 or {}).get('detail', r2)}]")
    return r2 or r1


def place_sell(sym, qty, live_price=None):
    """
    Fractional sell locked to ACCOUNT. Market with a downward collar, then a
    marketable limit fallback (when live_price is known).
    """
    instruments = rh.stocks.get_instruments_by_symbols(sym, info="url")
    if not instruments:
        raise ValueError(f"No instrument found for {sym}")
    inst = instruments[0]
    q    = str(round(qty, 6))

    p1 = _base_order(sym, inst, "sell")
    p1.update({"type": "market", "quantity": q})
    if live_price:
        p1["price"] = str(round(live_price * 0.99, 2))
    r1 = _order_post(p1)
    if _ok(r1):
        return r1
    print(f"    [sell A1 market rejected: {(r1 or {}).get('detail', r1)}]")

    if live_price:
        p2 = _base_order(sym, inst, "sell")
        p2.update({"type": "limit", "quantity": q,
                   "price": str(round(live_price * 0.995, 2))})
        r2 = _order_post(p2)
        if _ok(r2):
            return r2
        print(f"    [sell A2 limit rejected: {(r2 or {}).get('detail', r2)}]")
        return r2 or r1
    return r1


# ── Main bot ──────────────────────────────────────────────────────────────────
def run_bot(request=None):
    # Step 1: market hours
    open_, et = check_market()
    if not open_:
        print(f"Market closed ({et.strftime('%H:%M ET')}). Done.")
        return ("market_closed", 200)

    print(f"=== Bot run {et.strftime('%Y-%m-%d %H:%M ET')} | account={ACCOUNT} ===")
    events = []   # order outcomes this run — drives the email alert at the end

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
            result = place_sell(sym, qty, market[sym]["live"])
            if result and result.get("id"):
                print(f"  → Order placed: {result['id']}")
                buying_power += qty * market[sym]["live"]
                low_cash = False
                events.append(f"SELL {sym} qty={qty:.6f} → PLACED (id {result['id']})")
            else:
                print(f"  → SELL rejected: {result}")
                events.append(f"SELL {sym} → REJECTED: {(result or {}).get('detail', result)}")
        except Exception as e:
            print(f"  → SELL error: {e}")
            events.append(f"SELL {sym} → ERROR: {e}")

    # Step 7: buy
    if not low_cash and not pdt_exhausted:
        for sym, sig in sigs.items():
            if sig["consensus"] != 1 or sym in positions: continue
            if spent >= spend_cap: break
            amount = min(max_pos * vix_scale, buying_power * 0.95, spend_cap - spent)
            if amount < 1.00: continue
            print(f"BUY {sym} ${amount:.2f} RSI={sig['rsi']:.1f}")
            try:
                result = place_buy(sym, amount, market[sym]["live"])
                if result and result.get("id"):
                    print(f"  → Order placed: {result['id']}")
                    buying_power -= amount; spent += amount
                    positions[sym] = {"qty": 0, "avg_cost": 0}
                    events.append(f"BUY {sym} ${amount:.2f} → PLACED (id {result['id']})")
                else:
                    print(f"  → BUY rejected: {result}")
                    events.append(f"BUY {sym} ${amount:.2f} → REJECTED: {(result or {}).get('detail', result)}")
            except Exception as e:
                print(f"  → BUY error: {e}")
                events.append(f"BUY {sym} ${amount:.2f} → ERROR: {e}")

    # Step 8: summary
    print(f"\n--- Summary | {et.strftime('%H:%M ET')} | acct={ACCOUNT} | "
          f"VIX={vix:.1f} | PDT={pdt} | P&L=${daily_pnl:.2f} | BP=${buying_power:.2f} ---")
    for sym, sig in sigs.items():
        if sig["consensus"] != 0:
            print(f"  {sym}: {sig['consensus']:+d}  RSI={sig['rsi']:.1f}  {sig['trend']}")
    print(f"  Zero-consensus: {sum(1 for s in sigs.values() if s['consensus']==0)}")

    # Email alert: send on any order activity, plus once at the morning open run
    # (9:45 ET) so you get a daily status even when nothing trades. Quiet otherwise.
    morning = (et.hour == 9 and et.minute >= 45)
    if events or morning:
        nonzero = [f"  {s}: {sig['consensus']:+d}  RSI={sig['rsi']:.1f}  {sig['trend']}"
                   for s, sig in sigs.items() if sig["consensus"] != 0]
        body = [f"Bot run {et.strftime('%Y-%m-%d %H:%M ET')}  (account {ACCOUNT})",
                f"Equity ${equity:.2f} | Buying power ${buying_power:.2f} | "
                f"P&L ${daily_pnl:.2f} | PDT {pdt}/3", ""]
        body.append("ORDERS THIS RUN:" if events else "No orders this run.")
        body += [f"  • {e}" for e in events]
        body += ["", "Signals (non-neutral):"] + (nonzero or ["  (all neutral)"])
        if any("PLACED" in e for e in events):
            subject = "✅ Trading bot — ORDER PLACED"
        elif any(("REJECTED" in e or "ERROR" in e) for e in events):
            subject = "⚠️ Trading bot — order rejected"
        else:
            subject = "Trading bot — morning status"
        send_email(subject, "\n".join(body))

    rh.logout()
    return ("ok", 200)


if __name__ == "__main__":
    # Manual test: `gh workflow run trading-bot.yml -f email_test=true` sends one
    # email and exits, to confirm alerts work without waiting for market open.
    if os.environ.get("EMAIL_TEST", "").lower() == "true":
        send_email("📧 Trading bot — email test OK",
                   "Cloud email alerts are working. You'll get a morning status each "
                   "trading day, plus an alert on every order (placed or rejected).")
        raise SystemExit(0)
    try:
        run_bot()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        # Only email a crash during the morning open window, so a persistent
        # failure can't spam 28 emails/day. Still always visible in the Actions log.
        try:
            _, _et = check_market()
            if _et.hour == 9 and _et.minute >= 45:
                send_email("❌ Trading bot — CRASHED at open", tb[-3000:])
        except Exception:
            pass
        raise
