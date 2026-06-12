You are performing the WEEKLY BEST-PRACTICES AUDIT of this autonomous Alpaca trading bot (stocks + a small crypto sleeve). You are running headless in GitHub Actions, in the repo checkout (branch main). The market is closed — make CODE changes only; never place, trigger, or simulate any trades. You have NO broker keys (deliberately), so order endpoints are unreachable — do not try.

Key files: alpaca_bot.py (trading engine), brief.py (AI research brief), review.py (weekly review), .github/workflows/*.yml, trade_log.jsonl (per-trade context), holds.json (buy-and-hold ledger), daily_plan.json, journal.md.

STEP 1 — RESEARCH (WebSearch, ~4-6 searches)
Look for anything NEW or commonly recommended that the bot lacks:
- r/algotrading + practitioner blogs: momentum/swing bot lessons, risk-management norms, common failure modes (stocks AND crypto bots).
- Alpaca: API changes/deprecations (the 2026-06-04 PDT retirement removed daytrade_count — fields like that get deleted), new endpoints, order-type changes, data-feed changes (bot uses free IEX via /v2/stocks/bars + /v2/stocks/trades/latest, and public v1beta3 crypto endpoints).
- Yahoo query2 fallback endpoints/screeners (most_actives, day_gainers, small_cap_gainers, aggressive_small_caps) — still working?
- US regulatory changes affecting small retail accounts.

STEP 2 — SCORE THE WEEK
- Read trade_log.jsonl entries from the past 7 days: buys/sells, stop_loss/take_profit/time_stop/hold_stop counts, crypto trades; win/loss on closed round trips where computable.
- Read holds.json (current holds, basis vs notional).
- Latest Friday review: `gh run list --workflow alpaca-review.yml --limit 1`, then `gh run view <id> --log` for the vs-SPY summary.
- Bot health: `gh run list --workflow alpaca-bot.yml --limit 20` — investigate any failures.

STEP 3 — AUDIT vs DESIGN PHILOSOPHY (invariants; NEVER loosen without overwhelming evidence — recommend instead)
- Cash-only, NO leverage, NO shorting, NO options; crypto is SPOT long-only.
- Three sleeves (max 80% deployed, 20% cash floor): TRADING 40% (-7% stop / +15% TP / 5-day time-stop), HOLD 30% (entries: 4+ buy votes, uptrend, RSI<=70, non-mover, SPY>SMA50; exits only -25% basis stop or 40%-from-peak ratchet), CRYPTO 10% (BTC/ETH/SOL/DOGE/SHIB/LINK/AVAX/LTC; 4%/coin; -15% stop / +30% TP).
- Per-name caps 10% (5% under $15); sub-$5 names <= half the hold sleeve; stock screener floor $0.10 with >=500k sh/day and >=$5M/day.
- Never average down (adds need live >= basis+2%); no entries at RSI>78; 3-day stop cooldown; daily -10% loss halt blocks buys but never exits; VIX>35 halt; marketable limits under $5; crypto tif=gtc.
Did the week's data show a rule misfiring (stops too tight and recovering after exit? time-stop churn? ratchet giving back too much? crypto stop churning on normal vol)? Do research findings suggest a missing guard?

STEP 4 — IMPLEMENT (conservatively)
- At most 2-3 well-justified changes. Reliability/bug fixes > new guards > parameter tweaks. NO strategy rewrites, no new asset classes, nothing that increases risk-taking.
- `python -m py_compile alpaca_bot.py` (and any edited .py) must pass before committing.
- Commit with evidence-based messages, then: `git pull --rebase --autostash origin main && git push`.
- If a fix is risky/unclear: do NOT change code — put it in the report as a recommendation.

STEP 5 — REPORT (this is the deliverable; never skip it)
Email Devon the summary (GMAIL_APP_PASSWORD is set; alpaca_bot imports need dummy broker keys):
  ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python3 -c "import alpaca_bot; alpaca_bot.send_email('📋 Weekly bot audit — <date>', '''<plain-English summary>''')"
Summary must cover: (a) week's performance vs SPY, (b) research findings that mattered, (c) exactly what changed and why (commit SHAs), (d) recommendations deferred to Devon. Plain English, decision-ready, no jargon.
