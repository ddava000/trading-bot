Trading bots - investigation report (Mon Jul 27)

BOTTOM LINE
Both bots are healthy right now. Nothing is broken. There were zero failed
GitHub runs today (or in the last 7 days), and no gaps in the 15-minute trigger
schedule. What you saw as "cron failures" and "status not updated in 4 days" was
mostly the weekend plus a phone-side caching quirk, not a dead bot. I found the
real weakness behind it and fixed it.

WHAT YOU SAW vs WHAT WAS ACTUALLY HAPPENING
- "Status not updated in 4 days": Markets are closed Saturday and Sunday, so
  NEITHER bot trades or updates its status over a weekend. The cloud bot's last
  Friday run was 3:45pm ET; it resumed normally Monday morning. The Robinhood
  laptop's last Friday activity was 3:40pm ET; it resumed Monday at 9:51am ET.
  Both were correctly idle, not dead. On top of that, the Claude phone app has
  been serving cached copies of the files (the same caching problem we hit
  before), which can show a reading several days old even when the live file is
  current.
- "Cron failures today": I checked every workflow run. Zero failed, zero
  cancelled, zero skipped, and no missing 15-minute slots. If you received a
  failure email, it came from cron-job.org or GitHub as a transient blip that
  retried successfully. It did not affect trading. Note: I cannot see your email
  inbox or the cron-job.org dashboard from here, so if those show something
  specific, forward it and I will trace it.

THE REAL PROBLEM, AND THE FIX
The genuine weakness: the monitoring could not tell "market closed, bot resting"
from "bot is dead." Both just show an old timestamp. A monitor that cries wolf
every weekend gets ignored, and then it misses a real outage. That is the actual
failure behind your bad experience.

Fixed (committed and pushed): the cloud status.json now publishes market-aware
liveness, all in UTC so no timezone guessing:
  - as_of_utc: when the file was written
  - market_open: whether the market was open at that moment
  - next_expected_utc: when the next run is actually due (one cycle out during
    market hours, or the next session's 9:45am ET open when closed)
The rule for any monitor is now simple and reliable: the bot is only "down" if
the CURRENT time is more than about 45 minutes past next_expected_utc. An old
timestamp on a weekend or overnight is expected and no longer looks like a
failure. These fields go live at the next market open (Tuesday 9:45am ET).

REAL-MONEY ROBINHOOD BOT (checked directly against the broker)
- Total value $119.18: about $101 in stock positions, $18 cash, $10 pending
  deposit. No options, no leverage, no crypto. The rails are holding.
- Live and trading (dry mode is OFF, as you chose). It committed heartbeats
  every 15 minutes through today's session, so monitoring on this bot is working.
- You chose to leave it running while away. It is running and healthy. If you
  want it stopped at any point, create a file named rh_HALT in the repo folder
  and it pauses on its next check.
- Note for context: because it is a cash account and only trades during market
  hours, it does nothing overnight or on weekends. That is normal.

CLOUD ALPACA BOT (paper, the main experiment)
- Equity about $443, roughly -2.8% versus the July 15 baseline of $455.67.
- Risk-off regime, not halted, running clean all day.
- The 60-second protective loop and news tripwire are active.

UPDATED PHONE MONITORING PROMPT
Paste this into the Claude phone app. The key change is that it uses
next_expected_utc and forces a fresh fetch, so it will stop false-alarming.

  Check my two trading bots. Fetch these fresh: add ?v=NNNNNN to each URL where
  NNNNNN is a NEW random number every time (defeats caching).
  Cloud:     https://raw.githubusercontent.com/ddava000/trading-bot/main/status.json?v=NNNNNN
  Robinhood: https://raw.githubusercontent.com/ddava000/trading-bot/main/rh_status.json?v=NNNNNN

  Report under 150 words:
  1. CLOUD: equity, vs_baseline_pct, regime, halted. Holds worst-first.
  2. ROBINHOOD: equity, dry true/false, positions.
  3. Anything wrong.

  LIVENESS RULE (do not get this wrong): the cloud file has next_expected_utc.
  Compare it to the CURRENT UTC time. The bot is only DOWN if now is more than 45
  minutes PAST next_expected_utc. An old "ts" by itself is NOT a problem, markets
  are closed nights, weekends, and holidays, and the bots correctly rest then.
  Do not report staleness unless the next_expected_utc test actually fails.

  FLAG LOUDLY: cloud halted is true; any hold worse than -25%; cloud
  vs_baseline_pct worse than -10%; Robinhood dry is false is EXPECTED (it is live
  now), but flag if positions vanish entirely or equity drops more than 20%.

  Do not tell me to buy or sell. Just report the numbers.

CAN THE PHONE CLAUDE WORK ON THE REPO LIKE I DO?
Short version: the Claude phone APP can only read the repo, not act on it. It has
no ability to run commands, edit files, or push code. But there IS a version that
can: Claude Code on the web (claude.ai/code), which runs in Anthropic's cloud,
can open this repo, and can edit, commit, and push exactly like I do, from a
phone browser, no laptop needed. That is the real answer to what you are asking.
One caution worth stating plainly: this repo controls a real-money bot, so a
Claude with push access can change live trading behavior. I would keep any
phone-driven work to monitoring and clearly-scoped fixes, and I have kept a
CLAUDE.md in the repo so any Claude that opens it inherits the safety rails
(no real-money moves, no strategy changes without you, the two-session
coordination protocol). If you want, tell me and I will confirm that file is in
place and complete.

OPEN ITEMS AND RECOMMENDATIONS
1. Mirror the market-aware fields (as_of_utc, next_expected_utc) into the laptop
   bot's rh_status.json. That file belongs to the laptop's Claude session, so I
   left it a note rather than editing it directly, to avoid two sessions
   colliding on the same file.
2. A true dead-man alert (something that emails you if a bot genuinely stops
   during market hours) is worth adding. It needs its own always-on trigger, so I
   did not build it unsupervised. I can design it when you are back.
3. The Claude phone-app caching is the one thing outside our code. The
   cache-busting ?v=NNNNNN trick in the prompt above is the workaround.

Nothing here needs an urgent decision from you. Everything is running.
