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
- HYBRID strategy (backtested 2026-06-24, ~95% deployed / ~5% cash): INDEX CORE 50% (buy-and-hold SPY/QQQ/IWM equal-weight, the shock absorber) + TRADING 15% (-7% stop / +15% TP / 5-day time-stop) + HOLD 25% (entries: 4+ buy votes, uptrend, RSI<=70, non-mover, SPY>SMA50; exits only -25% basis stop or 40%-from-peak ratchet, plus a DIVERSIFY trim capping correlated-theme clusters at 2 names) + CRYPTO 5% (BTC/ETH/SOL/DOGE/SHIB/LINK/AVAX/LTC; 4%/coin; -15% stop / +30% TP). Check alpaca_bot.py's Config block comments for the current numbers before flagging a mismatch — the split has changed before and code is the source of truth.
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
  ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python3 -c "import alpaca_bot; alpaca_bot.send_email('Weekly bot audit - <date>', '''<plain-English summary>''')"
  Subject line: plain ASCII only, no emojis, hyphens not em dashes (Devon prints these to PDF and the subject becomes the filename; non-ASCII chars error out — see commit 38bfade).
Summary must cover: (a) week's performance vs SPY, (b) research findings that mattered, (c) exactly what changed and why (commit SHAs), (d) recommendations deferred to Devon. Plain English, decision-ready, no jargon.

ALSO append that SAME summary to `AGENT_MAIL.md` as a `cloud -> both` entry, then commit and push it (Devon 2026-08-23: the audit must land in the repo mailbox, not only in his inbox). Follow the mailbox protocol at the top of that file: append only, never edit an existing entry, and `git pull --rebase` right before appending since both sessions write to it. Reason: the cloud and laptop sessions read AGENT_MAIL.md at the start of every session, so this is the only channel that reaches them without Devon relaying it by hand.
Write that copy FOR THOSE SESSIONS, not for Devon: state which findings touch SHARED rails imported from alpaca_bot.py (stops, ratchet, RSI caps, correlation, news, earnings) since a break there silently affects both bots, and flag anything a future session might "fix" back or mistake for a bug. Keep the same four sections. If the email fails, still write the mailbox entry, and vice versa: they are independent deliverables.
ALSO archive settled threads as part of the run (Devon 2026-08-23, protocol rule 6): move closed entries verbatim from `AGENT_MAIL.md` into `AGENT_MAIL_ARCHIVE.md`, move rather than delete or summarise in place, and confirm each thread is genuinely closed before moving it (check the code, not just the conversation - an "optional, your call" suggestion may have been quietly implemented, or quietly not). Lift any still-true operational fact into the STANDING FACTS block at the top of the live file so archiving never costs working knowledge. Verify entry counts and content survive across the two files before committing.
ASK WHAT THE CODE DOES IN THE ENVIRONMENT IT ACTUALLY RUNS IN, not the one you are testing from (added 2026-08-23, after this caught two fail-open bugs in two days, both in already-reviewed code). Concretely: does this behave the same from a GitHub runner as from Devon's residential IP (Yahoo blocks datacenter ranges)? On a FRESH runner with no state file, no cache, no prior run? On a weekend, when the bot's trigger never fires? Outside market hours? The failures this finds are silent by construction, so nothing in a log or an alert will point at them. Two real examples: the Yahoo crumb fix that had never once executed on CI, and a mailbox watcher whose state file could never exist on a runner, so it would have reported nothing forever while everyone believed they had daily coverage.

FAIL-OPEN GUARD LIVENESS (every run, added 2026-08-23). Read `status.json`'s `earnings_guard` field from the most recent MARKET-HOURS run: `live` = Yahoo crumb handshake works from the GitHub runner, `degraded` = it does not and the earnings guard is returning False for everything, `unknown` = never exercised. `degraded` is a real finding to report, not a warning to skip: the guard is silently off. Verifying from a residential IP proves nothing about the runner, because Yahoo blocks the cookie/crumb flow from datacenter ranges. Generalise this: any guard that fails OPEN needs a published liveness signal, since a broken one is indistinguishable from a quiet one. Audit for other guards with the same shape.
Note that BOTH files are context you need: read `AGENT_MAIL.md` and `AGENT_MAIL_ARCHIVE.md` before STEP 3, since the invariant list above is a summary and the reasoning behind several "settled, do not re-explore" items only exists there.
