"""
Standalone order placement test — runs outside market hours.
Tests the buy order API against a real symbol without the full bot flow.
Run locally: python test_order.py
"""
import os, uuid, base64
import robin_stocks.robinhood as rh

ACCOUNT     = "950942706"
ACCOUNT_URL = f"https://api.robinhood.com/accounts/{ACCOUNT}/"

RH_USERNAME    = os.environ["RH_USERNAME"]
RH_PASSWORD    = os.environ["RH_PASSWORD"]
RH_SESSION_B64 = os.environ.get("RH_SESSION_B64", "")

_ORDER_VERSIONS = ["1.432.0"]

def rh_login():
    if RH_SESSION_B64:
        home = os.path.expanduser("~")
        os.makedirs(os.path.join(home, ".tokens"), exist_ok=True)
        with open(os.path.join(home, ".tokens", "robinhood.pickle"), "wb") as f:
            f.write(base64.b64decode(RH_SESSION_B64.strip()))
        rh.login(RH_USERNAME, RH_PASSWORD, store_session=True)
    else:
        rh.login(RH_USERNAME, RH_PASSWORD, store_session=False)

def test_buy(sym, dollar_amount, live_price):
    instruments = rh.stocks.get_instruments_by_symbols(sym, info="url")
    if not instruments:
        print(f"  ERROR: no instrument for {sym}")
        return
    shares = round(dollar_amount / live_price, 6)
    print(f"  Instrument: {instruments[0]}")
    print(f"  Shares: {shares} @ ${live_price}")

    _orig_ver = rh.helper.SESSION.headers.get("X-Robinhood-API-Version", "")
    for ver in _ORDER_VERSIONS:
        rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": ver})
        payload = {
            "account":       ACCOUNT_URL,
            "instrument":    instruments[0],
            "symbol":        sym,
            "type":          "market",
            "time_in_force": "gfd",
            "trigger":       "immediate",
            "side":          "buy",
            "quantity":      str(shares),
            "price":         str(round(live_price, 2)),
            "ref_id":        str(uuid.uuid4()),
        }
        print(f"\n  --- Trying version {ver} / form-encoded ---")
        print(f"  Payload: {payload}")
        resp = rh.helper.SESSION.post("https://api.robinhood.com/orders/", data=payload)
        result = resp.json()
        print(f"  HTTP status: {resp.status_code}")
        print(f"  Response: {result}")
        if result.get("id"):
            print(f"\n  ✅ ORDER PLACED: {result['id']}")
            break
        rh.helper.SESSION.headers.update({"X-Robinhood-API-Version": _orig_ver})


print("=== Order API Test ===")
rh_login()

# Verify account
profile = rh.helper.request_get(ACCOUNT_URL, dataType="regular")
url = (profile or {}).get("url", "").rstrip("/")
acct = url.split("/")[-1]
print(f"Account: {acct}")
bp = (profile or {}).get("buying_power", "?")
print(f"Buying power: ${bp}")

# Test a buy order for AAPL (already held, so won't change position if placed)
# Using a tiny $1 amount to minimise risk if it somehow goes through
print("\nTesting BUY order for AAPL ($1.00):")
test_buy("AAPL", 1.00, 310.0)

rh.logout()
print("\nDone.")
