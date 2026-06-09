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
# Daily loss cap scales with the account: max(3% of equity, $20). On the $100k
# paper account that's ~$3k (ignores normal intraday noise); on a small live
# account the $20 floor still applies — fits both without a code change.
LOSS_CAP_PCT   = 0.03
LOSS_CAP_FLOOR = 20.00
PDT_LIMIT      = 3
MAX_POS_PCT    = 0.30
SPEND_CAP_PCT  = 0.75

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


# ── Alpaca broker layer (official REST API) ───────────────────────────────────
def alpaca_get(path):
    r = requests.get(ALPACA_BASE + path, headers=ALPACA_HDRS, timeout=15)
    return r.json()

def alpaca_account():
    """Returns (buying_power, equity, daily_pnl, daytrade_count).
    daily_pnl = equity − last_equity (today's total account change; used only for
    the safety loss-cap). PDT count comes straight from Alpaca's daytrade_count."""
    a = alpaca_get("/v2/account")
    bp  = float(a["buying_power"])
    eq  = float(a["equity"])
    leq = float(a.get("last_equity") or eq)
    pdt = int(a.get("daytrade_count") or 0)
    return bp, eq, eq - leq, pdt

def alpaca_positions():
    pos = {}
    for p in alpaca_get("/v2/positions"):
        q = float(p.get("qty_available") or p.get("qty") or 0)
        if q > 0:
            pos[p["symbol"]] = {"qty": q, "avg_cost": float(p.get("avg_entry_price") or 0)}
    return pos


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

def place_buy(sym, dollar_amount):
    """Fractional MARKET buy by dollar amount (notional). Alpaca computes the
    share count — no live price needed, no version gate."""
    r = alpaca_order({"symbol": sym, "notional": str(round(dollar_amount, 2)),
                      "side": "buy", "type": "market", "time_in_force": "day"})
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

    buying_power, equity, daily_pnl, pdt = alpaca_account()
    max_pos   = equity * MAX_POS_PCT
    spend_cap = buying_power * SPEND_CAP_PCT
    spent     = 0.0
    low_cash  = buying_power < 5.00
    positions = alpaca_positions()
    plan      = load_plan(et)
    print(f"  BP=${buying_power:.2f}  EQ=${equity:.2f}  dayP&L=${daily_pnl:.2f}  PDT={pdt}")
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
    if low_cash:
        print("LOW_CASH — positions only.")
    else:
        wsb    = fetch_wsb()
        screen = fetch_screener()
        meme_tickers = wsb
        for s in wsb + screen + plan["favor"]:   # plan's favored names get considered too
            if len(universe) < 20: universe.add(s)
    universe = list(universe)
    print(f"UNIVERSE ({len(universe)}): {universe}")

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

    # SELL first (frees buying power)
    for sym, sig in sigs.items():
        if sig["consensus"] != -1 or sym not in positions: continue
        qty = positions[sym]["qty"]
        if qty <= 0: continue
        print(f"SELL {sym} qty={qty} RSI={sig['rsi']:.1f}")
        try:
            r = place_sell(sym, qty)
            if _ok(r):
                print(f"  → placed {r['id']}")
                buying_power += qty * market[sym]["live"]; low_cash = False
                events.append(f"SELL {sym} qty={qty} → PLACED ({r['id']})")
                trades_log.append({
                    "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "symbol": sym,
                    "side": "sell", "qty": qty, "order_id": r["id"],
                    "live": round(market[sym]["live"], 2), "rsi": round(sig["rsi"], 1),
                    "trend": sig["trend"], "consensus": sig["consensus"],
                    "delta": round(sig["delta"], 4), "sells": sig["sells"],
                    "macd_up": sig["macd_up"], "vix": round(vix, 1)})
            else:
                events.append(f"SELL {sym} → REJECTED: {(r or {}).get('message', r)}")
        except Exception as e:
            events.append(f"SELL {sym} → ERROR: {e}")

    # BUY
    if not low_cash and not pdt_exhausted:
        for sym, sig in sigs.items():
            if sig["consensus"] != 1 or sym in positions: continue
            if sym in plan["avoid"]:
                print(f"  SKIP {sym} (plan avoid-list)"); continue
            if spent >= spend_cap: break
            amount = min(max_pos * vix_scale * plan["risk"], buying_power * 0.95, spend_cap - spent)
            if amount < 1.00: continue
            print(f"BUY {sym} ${amount:.2f} RSI={sig['rsi']:.1f}")
            try:
                r = place_buy(sym, amount)
                if _ok(r):
                    print(f"  → placed {r['id']}")
                    buying_power -= amount; spent += amount
                    positions[sym] = {"qty": 0, "avg_cost": 0}
                    events.append(f"BUY {sym} ${amount:.2f} → PLACED ({r['id']})")
                    trades_log.append({
                        "ts": et.strftime("%Y-%m-%dT%H:%M"), "mode": MODE, "symbol": sym,
                        "side": "buy", "notional": round(amount, 2), "order_id": r["id"],
                        "live": round(market[sym]["live"], 2), "rsi": round(sig["rsi"], 1),
                        "trend": sig["trend"], "consensus": sig["consensus"],
                        "delta": round(sig["delta"], 4), "buys": sig["buys"],
                        "macd_up": sig["macd_up"], "meme": sig["meme"], "vix": round(vix, 1)})
                else:
                    events.append(f"BUY {sym} ${amount:.2f} → REJECTED: {(r or {}).get('message', r)}")
            except Exception as e:
                events.append(f"BUY {sym} ${amount:.2f} → ERROR: {e}")

    # Summary
    print(f"\n--- {MODE} | {et.strftime('%H:%M ET')} | VIX={vix:.1f} | PDT={pdt} | "
          f"dayP&L=${daily_pnl:.2f} | BP=${buying_power:.2f} ---")
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
                f"Equity ${equity:.2f} | Buying power ${buying_power:.2f} | "
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
    # The workflow commits trade_log.jsonl back to the repo so it survives runs.
    if trades_log:
        with open("trade_log.jsonl", "a") as f:
            for t in trades_log:
                f.write(json.dumps(t) + "\n")
        print(f"  [logged {len(trades_log)} trade(s) to trade_log.jsonl]")


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
