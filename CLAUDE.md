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

## The two bots
- **Cloud bot** (`alpaca_bot.py`): Alpaca PAPER money. Runs in GitHub Actions every
  15 min with a 60s protective loop. The main validation experiment.
- **Robinhood laptop bot** (`rh_bot.py` decision engine, `rh_daemon.py` runner):
  REAL money, ~$120, on an always-on Windows laptop. `rh_bot.py` imports every rail
  from `alpaca_bot` so the two strategies cannot drift.

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
  `.github/workflows/`.
- **Laptop session owns:** `rh_bot.py`, `rh_daemon.py`, `setup_laptop.ps1`.
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
