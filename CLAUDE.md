# Trading bots - operating guide for any Claude working in this repo

This repo runs two automated trading bots. One trades REAL MONEY. Read the hard
rails before doing anything.

## HARD SAFETY RAILS (never violate)
- **Never place, move, or approve a real-money trade yourself.** Claude engineers
  the automation; the BOT places orders. Do not buy, sell, or transfer on Devon's
  behalf in a conversation, ever.
- **Never change trading strategy or risk limits without Devon's explicit say-so.**
  Fixing a bug is fine. Retuning stops, sizing, or the universe is not, unless he
  asks.
- **Cash only. No options, no leverage, no shorting, no margin.** These are the
  standing rails for both bots.
- **Never commit account numbers or secrets.** The repo is PUBLIC. Account numbers
  live in `rh_config.json` (gitignored). Email/API passwords are GitHub secrets.
- **Backtests are hypothetical by default.** Running `backtest.py` touches nothing
  live. Never apply a backtest finding to a live bot without Devon saying so.

## The two bots (both trade REAL money; corrected 2026-09-30, this section used to say paper)
- **Arm A, the cloud bot** (`alpaca_bot.py`): Alpaca LIVE, ~$240, the HYBRID: index 50 /
  hold 25 / trade 20 / crypto 0. Runs in GitHub Actions every 15 min (triggered by
  cron-job.org) with a 60s protective loop. Crypto is PERMANENTLY retired: Alpaca does not
  offer it in Colorado, so do not "re-enable" it.
- **Arm B, the Robinhood laptop bot** (`rh_bot.py` decision engine, `rh_daemon.py` runner):
  ~$280, plain INDEX ETFs buy-and-hold (SPY/QQQ/IWM, no stops), on an always-on Windows
  laptop. `rh_bot.py` imports every rail from `alpaca_bot` so the mechanics cannot drift,
  but its sleeve allocation is set explicitly (index-only) and is NOT inherited.
- **They are an A/B experiment.** Window opened 2026-08-24, decision no earlier than
  2026-11-24. Read `experiment.json` (`rule`, `question_being_answered`) before comparing
  them, and do not draw conclusions early.

> IMPORTANT, learned the hard way (2026-07-28): the `rh_daemon` order EXECUTOR (a
> headless `claude -p` turn) is SANCTIONED, approved automation, not a rogue agent
> placing trades. It is the only supported path to Robinhood, and it merely places
> the exact order list `rh_bot.decide()` already produced. It MUST run with a
> working directory OUTSIDE this repo: if it starts inside the repo it reads this
> CLAUDE.md, sees "never place a real-money trade," and refuses the orders it was
> handed, silently blocking all trading (including protective stops). The "never
> trade" rule below governs INTERACTIVE, human-directed sessions only; the executor
> is kept isolated instead. Do NOT move the executor back into the repo dir, and do
> NOT "fix" it by weakening the rule, that isolation is the design.

## Two-session coordination
Two Claude sessions may work here at once. Coordinate through the repo (commit +
pull); the sessions cannot talk directly.
- **Cloud session owns:** `alpaca_bot.py`, `brief.py`, `review.py`, `backtest.py`,
  `.github/workflows/`, `realized.py`, `realized_report.py`, `build_realized_b.py`,
  `pnl.py`, `check_ascii.py`, `slack_notify.py`, `mail_check.py`, and `tests/`.
- **Laptop session owns:** `rh_bot.py`, `rh_daemon.py`, `rh_watchdog.py`,
  `setup_laptop.ps1`, `rh_deposits.json`, and refreshing `realized_b.json`.
- **Tests and CI.** Run `python -m unittest discover -s tests` before pushing code.
  `.github/workflows/ci.yml` runs it on any push that touches code, workflows, tests or
  the mailbox, and a red run is a finding, not noise. It validates every workflow file (an
  invalid one once never ran for ten weeks), syntax-checks their shell, scans for
  credentials and personal email addresses in this PUBLIC repo, and covers the realized
  gain/loss code that feeds Devon's tax number. Every check that hunts for something also
  proves it can find it.
- `git pull --rebase` before any edit. Prefer not to edit the other session's files;
  if you must (at Devon's request), say so in the commit message and post a note in
  the mailbox below so the owner has context.
- **Never `git add -A` or `git commit -a`.** Commit EXPLICIT paths. A broad add
  sweeps up whoever else is mid-edit.
- **Never autostash on a tree that may contain work that is not yours.** On a SHARED
  tree: commit your own work by explicit path, `git status`, then a plain `git pull`,
  and if status shows files you did not touch, STOP and report rather than resolving
  it. On 2026-08-25 autostash lifted and replaced a third session's uncommitted file
  on every pull for an afternoon; one conflict would have destroyed it.
  On a SINGLE-SESSION tree autostash is fine, and is specifically fine for a generated
  file the local process owns. (Corrected 2026-08-26 after laptop showed the blanket
  ban was unworkable: `rh_status.json` is TRACKED and its daemon rewrites it every
  pass, so that tree is never clean and a plain pull would refuse during market hours.
  The hazard is other people's work, not dirtiness as such.)
- **Verify a push landed by reading the remote, not the command output.** An `rm` that
  failed once short-circuited an `&&` chain so `git add`/`git commit` never ran, while
  a `git push` on the next line ran anyway and printed success. The "fix" sat
  uncommitted for two days and everyone believed it had shipped.

## Best-practices audit: CROSS-audit, never self-audit
Devon 2026-08-26: two sessions only, cloud and laptop, and the two of them handle the
audit. That works ONLY as a cross-audit, and the distinction is not pedantic:
- **The laptop audits the cloud's files** (`alpaca_bot.py`, `brief.py`, `review.py`,
  `.github/workflows/`). **The cloud audits the laptop's files** (`rh_bot.py`,
  `rh_daemon.py`, `rh_watchdog.py`). **Neither audits its own work.**
- Why: every real defect found the week of 2026-08-25 crossed a session boundary, and
  none came from a session checking itself. A session re-reads its own code with the
  same assumptions that produced the bug.
- Verify, do not accept. When the other session reports a fix, CHECK it: read the
  remote, run the failing case, prove the guard can still say "no". A report is not
  evidence. This caught three wrong claims in two days, in both directions.
- **A cross-audit is CODE REVIEW, not behavioural verification, and saying "audited"
  when you mean "read" is how a wrong result gets believed.** Neither session can run
  the other's code where it actually runs: cloud has no laptop, no broker bridge and
  no live ledger; laptop has no Alpaca keys, by design. The laptop's quote-gap money
  bug was only findable by REPRODUCING it against a live ledger, and a careful reader
  would have called that code correct. So when a finding depends on RUNTIME behaviour,
  say so and ask the owner to run the case, rather than reporting it as established
  either way.
- **Shared files** (`slack_notify.py`, `mail_check.py`, `CLAUDE.md`,
  `.github/audit-prompt.md`, `experiment.json`, `rh_deposits.json`) are
  write-by-either, audit-by-both. Owners for tie-breaks: cloud owns `slack_notify.py`
  and `mail_check.py`; laptop owns `rh_deposits.json` (its daemon writes it). Any
  change to `audit-prompt.md`, `experiment.json` or `rh_deposits.json` must be
  announced in AGENT_MAIL in the SAME commit: those three decide what Sunday's audit
  knows, what the experiment claims, and whether Arm B's number is real.
- `.github/workflows/weekly-audit.yml` (Sundays, cold context in Actions) is a third
  reviewer that costs no window and starts with no assumptions. It reads
  `.github/audit-prompt.md`, so that file is the ONLY channel to it. Keep it current.
  It was an INVALID workflow file from 2026-06-12 until 2026-08-26 and never ran once;
  a local scheduled task was silently producing the audit everyone credited to it.

## Realized gain/loss: EVERY report carries a running total (Devon 2026-09-29)
Devon wants a running total of REALIZED gains versus losses in every report, audit and
check, to track tax implications. This is NOT the equity change pnl.py prints: equity
includes unrealized paper P&L, and only positions actually SOLD are taxable.
- **One shared reader.** Never format these numbers yourself. Run
  `python -c "import realized; print(chr(10).join(realized.repo_report_lines()))"` from
  the repo root and quote the lines. It reads committed files only, so any session or
  runner gets the same answer. (`pnl.py`, the bot email, the weekly review, the audit and
  both daily checks all use it.)
- **Arm A** is computed by the bot every 15-min cycle from the full Alpaca fill history
  and published in `status.json` under `realized`, with a per-lot ledger in
  `realized_a.json`. It carries an accounting-identity residual (cash + market value -
  contributions - income == FIFO realized + unrealized) that must stay within 0.50 dollars.
- **Arm B** comes from the BROKER's own figures (`realized_b.json`, built by
  `build_realized_b.py` from Robinhood's get_pnl_trade_history and get_equity_orders).
  It is index-only and rarely sells, so the ledger can sit unchanged and be right, which is
  exactly how it goes silently wrong after the next sale. **After ANY sale by the Robinhood
  daemon, the laptop must refresh it.** The runnable procedure,
  with a copy-ready prompt for the headless export, is the docstring of `build_realized_b.py`
  (raw exports go OUTSIDE the repo; the builder refuses an empty or truncated export). `realized.arm_b_block()` detects a missed refresh
  by comparing sells logged in `rh_trade_log.jsonl` against the count the ledger accounted
  for, and reports `stale`. Treat a stale Arm B figure as a finding, never as a number.
- **State is part of the number.** ok / unverified / stale / unknown. Never report a
  non-ok figure as if it were clean, and never render unknown as zero.
- **Per calendar year.** Tax nets per year, so the current-year line restarts in January.
  From Jan 1 to Oct 31 the reader also prints the PRIOR year's lines (returns are being
  prepared and extended ones are due mid-October), so last year's final figures do not vanish.
  Wash-sale exposure on a line is that year's own: a loss belongs to the year it was SOLD.
- **Wash sales are WATCHED, never adjusted.** Reports carry an upper bound on losses that may
  be wash sales, same-account and cross-account (both arms buy SPY/QQQ/IWM, and no broker
  reports cross-account ones). The rule also spans accounts no session can see, including
  IRAs. Report the figure; only Devon's tax preparer decides what applies.
- **Not a tax document.** The broker's 1099-B is authoritative. Do not give tax advice.
- **After a split, spinoff, symbol change or securities transfer** the FIFO is wrong: the bot
  marks Arm A unverified. Run the `realized-report` workflow to see every closed lot.

## Agent mailbox (how the two sessions talk)
The sessions cannot chat live (neither runs continuously). They leave notes in
`AGENT_MAIL.md` at the repo root. **RULE: at the start of any work session, `git
pull` and read `AGENT_MAIL.md`. If there is a message addressed to you with no reply
from you, handle it and reply by APPENDING a new entry (never edit or delete an
existing one).** Use it for cross-domain heads-ups, questions, and handoffs. The
format and protocol are documented at the top of that file.

## Kill switch
Create a file named `rh_HALT` in the repo folder to pause the real-money bot on its
next check.

## Monitoring
Both bots publish status files read from the public repo: `status.json` (cloud) and
`rh_status.json` (Robinhood). Liveness uses `next_expected_utc`: a bot is "down"
only if the current UTC time is well past it. An old timestamp on a weekend or
overnight is normal, both bots rest when markets are closed.

The cloud watchdog for the laptop (`rh_watchdog.py`, `rh-watchdog.yml`) cannot rely on
GitHub's native cron: measured 2026-09-09 to 09-30 it ran 1 to 3 times a day, once inside
market hours. It also runs after every Arm A run (which cron-job.org triggers about every 15
minutes), and on that path alerts once per threshold rather than on every run. Count its real
runs (`gh run list --workflow rh-watchdog.yml`) instead of trusting the cron line.

## Work from your phone (Claude Code on the web)
Devon can drive this repo from a phone with zero computer running, via Claude Code
on the web (claude.ai/code, or the Code tab in the Claude app). One-time: connect
GitHub (one approval), then pick `ddava000/trading-bot` and start a session. Edits,
test runs, commits and PRs all happen in Anthropic's cloud.

To make a web session as capable as a local one, paste this into the cloud
environment's **Setup script** field (claude.ai environment settings). It installs
`gh` (for triggering/reading the bot's Actions runs) and the Python deps:

```bash
#!/bin/bash
apt update && apt install -y gh || true
pip install -r requirements-alpaca.txt || true
```

Not covered by that script: the Robinhood account-lookup MCP connector is a
claude.ai connector, so a web session may need it connected separately, or it may
not be available there. Core dev work (edit, run tests, fix, commit) needs none of
it.

## Working with Devon (hard preferences)
- **Never use em dashes.** Hard rule.
- **No emojis in email subject lines** (he prints mail to PDF; the subject becomes
  the filename and emoji break it).
- **No multi-step setup flows or option menus.** One decisive path, or do it fully
  yourself. Warn up front about any unavoidable login clicks.
- **Put copy-ready text in a fenced code block.**
- Be decisive and honest. Report failures plainly. Do not oversell.
