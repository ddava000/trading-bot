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

## Two-session coordination
Two Claude sessions may work here at once. Coordinate through the repo (commit +
pull); the sessions cannot talk directly.
- **Cloud session owns:** `alpaca_bot.py`, `brief.py`, `review.py`, `backtest.py`,
  `.github/workflows/`.
- **Laptop session owns:** `rh_bot.py`, `rh_daemon.py`, `setup_laptop.ps1`.
- `git pull --rebase` before any edit. Do not edit the other session's files; if a
  shared change is needed, leave a note in a commit message or report for Devon to
  relay.

## Kill switch
Create a file named `rh_HALT` in the repo folder to pause the real-money bot on its
next check.

## Monitoring
Both bots publish status files read from the public repo: `status.json` (cloud) and
`rh_status.json` (Robinhood). Liveness uses `next_expected_utc`: a bot is "down"
only if the current UTC time is well past it. An old timestamp on a weekend or
overnight is normal, both bots rest when markets are closed.

## Working with Devon (hard preferences)
- **Never use em dashes.** Hard rule.
- **No emojis in email subject lines** (he prints mail to PDF; the subject becomes
  the filename and emoji break it).
- **No multi-step setup flows or option menus.** One decisive path, or do it fully
  yourself. Warn up front about any unavoidable login clicks.
- **Put copy-ready text in a fenced code block.**
- Be decisive and honest. Report failures plainly. Do not oversell.
