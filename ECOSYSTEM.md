# The trading-bot ecosystem: read this, then CLAUDE.md

Every session reads this on a cold start and before it answers "check mail". It is the one picture of who
and what exists, so no session has to guess about another. Keep it true: tests/test_ecosystem_doc.py fails
if a workflow or a session name is missing from it.

## The five sessions (names are Devon's; mail_check.DISPLAY is the code copy, pinned by tests)

| Name | Heading token | What it is | Wakes by |
|---|---|---|---|
| CLOUD | `cloud` | interactive cloud session: owns alpaca_bot.py, brief.py, review.py, the workflows, tests/, slack_notify.py, mail_check.py, mailbox_notify.py, realized*.py | Devon says "check mail" to it |
| BOT DAILY CHECK | `cloud[daily]` | cloud's scheduled weekday mail check (about 15:42 CT): reads cloud mail, verifies, reports to Slack | automatic, or Devon |
| LAPTOP BOT | `laptop` | interactive session on the Robinhood laptop: owns rh_bot.py, rh_daemon.py, rh_watchdog.py, rh_deposits.json, the Arm B ledger refresh | Devon says "check mail" to it |
| LAPTOP BOT DAILY CHECK | `laptop[daily]` | the laptop's scheduled daily check (4:15 PM CT): daemon, bridge login, crashes, outages, deposits | automatic, or Devon |
| BOT WEEKLY AUDIT | `audit` | the Sunday GitHub Actions audit (weekly-audit.yml), cold context, commits its upgrades | never: it reads mail itself every Sunday |

The two scheduled prompts (cloud-bot-daily-check and laptop-bot-daily-check) live OUTSIDE this repo, in
each owner's ~/.claude/scheduled-tasks, so only the owner can confirm a change to its own: do not track
the other's as open indefinitely.

Mail to a bare token (`-> cloud`) names the interactive session. A daily check reads every entry to its
base token, so it never needs waking for mail. Only the two interactive sessions are ever woken by hand.

## What runs where

- **Arm A, Alpaca LIVE (~$240, hybrid)**: workflow "Alpaca Trading Bot", triggered by cron-job.org every 15
  minutes in market hours (26 runs a day) via workflow_dispatch. Never a GitHub cron.
- **Arm B, Robinhood (~$280, plain index ETFs)**: rh_daemon.py on the always-on laptop, orders placed by a
  headless `claude -p` executor that runs OUTSIDE this repo (it must never read CLAUDE.md).
- **GitHub workflows**: Alpaca Trading Bot; RH laptop watchdog (after every Arm A run, plus a */30 cron backup);
  ARM A WATCH (dead-man's switch for Arm A, see below); AGENT_MAIL daily check (13:00 UTC digest email);
  MAILBOX NOTIFY (Slack message on every mailbox push);
  BOT WEEKLY AUDIT (Sundays); Research Brief (morning + intraday), weekdays; Alpaca Weekly Review (Fridays);
  ci (tests on push); realized-report, slack-test and Email Report (manual).
- **ARM A WATCH** (`arm_a_watch.py`, workflow of the same name; Devon 2026-10-06). Arm A checks its stop-losses
  itself inside each run, with NO stop orders at the broker, so a run that does not happen means unprotected
  positions. On 2026-10-05 GitHub's Actions incident left the 15:30 and 15:45 ET runs without a machine for
  about 28 minutes before the close and nothing alerted. It watches three things: a run WAITING for a machine
  (alerts at 6 minutes, urgent at 20), NO run triggered (22 / 50), and status.json NOT COMMITTED (35 / 75;
  normal is every 15, worst seen 24.5). First level is email and Slack; the urgent level adds SMS and push.
  The workflow runs on GitHub and so shares fate with Arm A in an incident; the check is meant to ALSO run on
  the Arm B laptop (`arm_a_watch.run_once()` from rh_daemon's loop), which does not depend on Actions. Until the
  laptop wires it, coverage is the best-effort workflow only.
- **A/B experiment**: opened 2026-08-24, no conclusion before 2026-11-24 (experiment.json).

## The channels: ONE channel of record

- **AGENT_MAIL.md is the channel.** Everything addressed to a session goes there. Slack and email only
  NOTIFY; they are never where a session looks for instructions.
- **Slack #trading-bots** is Devon's phone view: reports, MAILBOX NOTIFY messages, relayed messages he types
  (they arrive as `slack -> all`, fenced as untrusted data). Kickstand never shares a channel with this.
- **Email to Devon**: order and error alerts, the daily digest, watchdog alerts, GitHub failure notices.

## WHEN DEVON SAYS "CHECK MAIL" (to any session): the order

1. `git pull`, then `python mail_check.py --inbox "<YOUR NAME>" --ack`. That list is your mail, by FILE
   POSITION after your own read marker. NEVER decide "nothing new" by comparing timestamps: stamps are typed
   by hand and have been wrong in both directions (that is how LAPTOP BOT missed two entries on 2026-10-01).
2. Read each entry from its line number, act on what is in your remit, and REPLY by appending (to the
   sender, short, real-clock stamp: see CLAUDE.md). An entry addressed to you with no reply is a defect.
3. CONFIRM ALIGNMENT. Append ONE entry `-> all` whose subject starts `ALIGNED: <YOUR NAME>` saying: you read
   this file and the five names match mail_check.DISPLAY; how many entries you found unread and what you did;
   the open items you hold; any disagreement. Skip it only if you already confirmed the open check.
4. A check is OPEN only while someone has not confirmed: `python mail_check.py --aligned` exits 1. If it
   exits 0, or finds none, the last round is finished: OPEN a new one by appending `-> all` with subject
   `ALIGNMENT CHECK <date>: every session reply ALIGNED`. The same command shows who has confirmed and
   how to reach the rest.
5. Messaging Devon is automatic: MAILBOX NOTIFY posts "Have <name> check mail." on every push that adds an
   entry needing an interactive session. Address mail to the session you need (`-> laptop`, `-> cloud`) so
   the notice names the right one. Writing to yourself, or only to `audit`, notifies nobody.

## Getting everyone aligned: the order Devon says "check mail" in

1. **CLOUD first.** It opens the round (an ALIGNMENT CHECK entry) and publishes any code the others need,
   so the others have something to answer.
2. **LAPTOP BOT second.** The other interactive session, the owner of the daemon, and the only other one
   that must be woken by hand.
3. **BOT DAILY CHECK and LAPTOP BOT DAILY CHECK: only if he does not want to wait.** Otherwise they confirm
   by themselves at their next scheduled run (cloud about 15:42 CT on weekdays, laptop 4:15 PM CT daily).
   To hurry one, say "check mail" to its chat.
4. **BOT WEEKLY AUDIT: nothing to do.** It cannot be woken and confirms on its next Sunday run.
5. **Verify.** Ask CLOUD, or run `python mail_check.py --aligned`, or read the Slack notices: MAILBOX
   NOTIFY posts "Still to confirm (n of 5)" after every confirmation.

## Rules that every session shares (details in CLAUDE.md)

Never place, move or cancel a trade; never change strategy or risk limits without Devon; cash only, no
leverage, no shorting, no options; never commit a secret or an account number (the repo is PUBLIC); no em
dashes in anything written for Devon; the executor never runs inside this repo; push after every commit and
verify by reading the remote; commit explicit paths, never `git add -A`; a check that says "absent" proves
nothing until it has been seen to say "present".
