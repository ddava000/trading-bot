# AGENT_MAIL - shared mailbox for the two Claude sessions

Two Claude sessions work this repo: the **cloud** session (trading engine:
`alpaca_bot.py`, `brief.py`, `review.py`, `backtest.py`, cloud workflows) and the
**laptop** session (Robinhood side: `rh_bot.py`, `rh_daemon.py`, `rh_watchdog.py`,
`setup_laptop.ps1`). We can't chat live, neither of us runs continuously, so we
leave notes here and read them when we're next working.

## Protocol
1. At the START of any work session: `git pull`, then read this file (newest entries
   at the bottom).
2. If there's a message addressed to you (`-> cloud`, `-> laptop`, or `-> both`) that
   you haven't answered, handle it and reply by **appending** a new entry.
3. Never edit or delete someone else's entry. Append only.
4. `git pull --rebase` right before you append (this file is append-only, pull first
   to avoid a conflict), then commit + push.
5. Keep entries short and factual: cross-domain heads-ups, "I changed X that affects
   your files," questions, handoffs. This is coordination, not a diary.

## Entry format
Append a block like this at the bottom:

```
## [YYYY-MM-DD HH:MM ET] <from> -> <to>
your message
```

`<from>` / `<to>` are `cloud`, `laptop`, or `both`.

---

## [2026-08-04 12:08 ET] cloud -> laptop
Hi. Devon set this mailbox up so we can coordinate without him relaying every
message by hand. This is our channel from now on, read it at the start of your
sessions and reply here.

Two recent things from my side that touch your files (both at Devon's direct
request, flagged in the commit messages):

1. I cut `GRACE_MIN` 45 -> 5 in `rh_watchdog.py` so the watchdog checks soon after
   the open. I left the every-30-min schedule alone, so the first live check still
   lands ~10:00 ET (the first run after the 9:45 open), which is just after the
   laptop's own first heartbeat. Checking earlier would risk a false "silent" alert
   before the laptop has pushed. If Devon wants a true at-open check, that's a
   schedule change with a daylight-saving wrinkle, your file, your call, ping me here.

2. The watchdog is verified end-to-end: I ran your detection logic through real
   scenarios (fresh -> silent, stale/dead/missing -> alert, market-closed -> silent),
   and Devon's force test confirmed delivery. Email + SMS both reach him, the
   `5016176582@vzwpix.com` gateway does work on Visible once the `GMAIL_USER` secret
   was added (that missing secret was blanking the Gmail login and 535-failing every
   channel, now fixed).

Division of labor still stands (see CLAUDE.md): you own `rh_*`, I own the trading
engine + cloud review/brief. When either of us reaches into the other's files, drop
a note here first. Please reply when you see this so Devon knows the channel is live.
