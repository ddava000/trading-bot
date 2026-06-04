# Cloud Trading Bot — Handoff / Resume Notes
_Imported from session "Robinhood Cloud Trading Bot Setup" (f4f105ee) · written 2026-06-04_

## TL;DR
The bot now runs in **GitHub's cloud** (no PC needed). Everything works **except placing buy orders** — Robinhood's order API keeps rejecting our payload. That's the one thing to fix. Resume here tomorrow at market open (9:45 AM ET).

## UPDATE 2026-06-04 PM (pushed, untested-until-open)
Reworked `place_buy`/`place_sell` before close:
- **Added `market_hours: "regular_hours"` + `extended_hours: "false"`** to every order — these are sent by robin_stocks on all fractional orders and were missing from our payload (most likely root cause).
- **Two-attempt ladder, first success wins:** (1) market order w/ upward price collar, then (2) marketable limit. Sells mirror this with a downward collar.
- **Verbose logging** of Robinhood's exact `detail` on each rejection, so tomorrow's first live signal is fully diagnosable in the Actions log.
- Syntax validated (`py_compile`). **Still first-tested at 9:45 AM ET** — RH rejects after-hours orders, so this could not be validated tonight.
- If both attempts still fail tomorrow: the logged `detail` strings are the key. Next move is byte-matching the MCP's request (it works on this account) or migrating execution to the sanctioned agentic endpoint.

---

## Architecture (what we built today)
```
cron-job.org  ──ping──>  GitHub Actions workflow  ──runs──>  main.py
                                                              │
                                                   robin_stocks (unofficial RH lib)
                                                              │
                                                     Robinhood account 950942706
```
- **Repo:** github.com/ddava000/trading-bot (PUBLIC)
- **Local copy:** `C:\Users\devon\OneDrive\Documents\Claude\trading-bot-cloud\`
- **Files:** `main.py` (the bot), `requirements.txt`, `setup_session.py` (one-time auth), `test_order.py` (standalone order tester), `.github/workflows/trading-bot.yml`
- **Trigger:** cron-job.org hits the workflow every ~5 min on weekdays
- **Watch runs at:** github.com/ddava000/trading-bot/actions

## Auth (working)
- Raw datacenter login was blocked by Robinhood as "unrecognized device."
- **Fix that worked:** generated a Robinhood session on the local (trusted) machine via `setup_session.py`, injected it into GitHub as a secret.
- GitHub Secrets in use: `RH_USERNAME`, `RH_PASSWORD`. (`RH_TOTP_SECRET` intentionally skipped — 2FA is app-based and only challenges non-bot access.)
- Account is **hard-locked to 950942706** in code (aborts on any other account). This is the `agentic_allowed=true` account.

---

## STATUS

### ✅ Working
- GitHub Actions infra (cron-job.org → GitHub → bot), runs with PC off
- Session auth / login from the cloud
- Market-hours gate (exits immediately when market closed)
- Market data fetch (Yahoo Finance OHLCV + VIX)
- Full signal engine (RSI, SMA, EMA9/21, MACD, Bollinger, vote tally) — mirrors local `signals.py`/`bot.py`, verified identical earlier
- Portfolio + positions + orders reads
- Account verification / abort-on-wrong-account

### ❌ Not working — THE ONE BUG: buy order placement
**The GitHub bot has never successfully placed a trade.** (The 15 positions currently in the account were placed June 1–3 by the *local* MCP routine, not by this cloud bot.)

Matrix of what we tried today and the errors:
| Attempt | Result |
|---|---|
| JSON body + `dollar_based_amount` | `Invalid order quantity` |
| JSON body + fractional `quantity` | `Invalid order quantity` |
| Form-encoded + fractional `quantity` (no price) | `Market buy order requested, but no price provided` |
| Form-encoded + fractional `quantity` + `price` | **untested at market open** (current state) |
| API version `1.432.0` | ✅ accepted (older 1.431.4 from robin_stocks 2.1.0 is rejected) |
| After-hours order test | dead end — RH returns "app version" error when market closed; only meaningful to test 9:45 AM–3:55 PM ET |

### Current code (main.py `place_buy`, ~line 282)
Sends to `POST https://api.robinhood.com/orders/` (form-encoded, `data=`), version header `1.432.0`:
```
type: "market"        <-- ⚠️ contradictory with price (see below)
time_in_force: "gfd"
trigger: "immediate"
side: "buy"
quantity: <fractional, e.g. 0.0123>
price: <round(live_price, 2)>
ref_id: <uuid4>
```

---

## NEXT STEPS (tomorrow, in priority order)

1. **Fix the contradictory payload.** `type: "market"` + `price` is self-conflicting. The standard Robinhood **fractional** buy is a *marketable limit*:
   - Change `type` to `"limit"`, keep `price` as a marketable limit (~0.1–0.3% above live for buys), keep fractional `quantity`, `time_in_force: "gfd"`. Try this FIRST — it's the most likely one-line fix.
2. **If that fails, byte-match the MCP.** The official MCP (`review_equity_order`/`place_equity_order`) places fractional dollar orders on this exact account flawlessly. Capture the precise HTTP request it emits and replicate the field set (likely `dollar_based_amount` + `market_hours` + `extended_hours` fields we were missing).
3. **Strategic option worth considering:** the MCP/agentic path is the *sanctioned* one for this account; robin_stocks is unofficial (ToS risk, already tripped device flagging once). Evaluate whether the cloud runner can drive the agentic endpoint instead of robin_stocks. May be the real unlock and removes the security exposure.
4. Use `test_order.py` for fast iteration during market hours (skips the hours gate) — but remember RH only accepts orders when the market is actually open.

## How to verify a fix
- Market opens 9:45 AM ET (bot waits until then). Watch github.com/ddava000/trading-bot/actions for the run log.
- A successful order returns JSON with an `id` and `state` (not a `detail` error).
- Confirm fills via the MCP `get_equity_orders` on account 950942706, or in this chat.

## Open security notes (user has accepted, documented for completeness)
- RH credentials + session token live in third-party CI; robin_stocks is unofficial (ToS). Mitigations user confirmed: app-based 2FA challenges any non-bot access; account scoped to the agentic account. Item #3 above would remove most of this exposure.

## User's running request log (for context)
clean up local tasks → stop PC popups → move to true cloud → chose GitHub over Google Cloud → repo live (ddava000/trading-bot) → secrets set → login fixed via session injection → order API debugging (unsolved) → added $25 buying power to enable a live test → resume to fix order placement.
