#!/usr/bin/env python3
"""Cloud dead-man's-switch for the Robinhood laptop bot.

Runs in GitHub Actions on a schedule, independent of the laptop. The laptop
pushes rh_status.json with an ET "ts" every ~15 min while it is alive; if that
timestamp goes stale during CONFIRMED open-market hours, this emails Devon.

Since Robinhood went index-only on 2026-08-22 a silent laptop is NOT urgent: there
are no stops waiting to fire, only deposits sitting uninvested. So routine alerts
go by email alone, and SMS/push are reserved for urgent=True. Each channel is also
gated on its own secret, so the watchdog degrades gracefully. On a normal day it prints one line and exits 0 (market closed,
or the bot is fresh), so it is silent unless something is actually wrong.

Freshness is read from the COMMITTED rh_status.json, i.e. the last state that
reached GitHub. That is deliberate: a laptop that is alive but cannot push is
also a monitoring blind spot, and this flags it too.
"""
import os, sys, json, smtplib, subprocess, urllib.request
from datetime import datetime
from email.mime.text import MIMEText

# Reuse the bot's own holiday-aware market clock so the watchdog and the bot
# agree on what "open" means, instead of duplicating the holiday list here.
# check_market() needs no live API call; the keys only satisfy the import.
os.environ.setdefault("ALPACA_API_KEY", "unused-in-watchdog")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-in-watchdog")
import alpaca_bot as bot

def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def _env_tuple(name, default):
    try:
        vals = tuple(int(x) for x in (os.environ.get(name) or "").split(",") if x.strip())
    except ValueError:
        vals = ()
    return vals or default


STATUS_F  = "rh_status.json"
STALE_MIN = 30    # heartbeat is 15 min, so >30 = ~2 missed pushes = likely down
CHECK_EVERY_MIN = 30   # this watchdog's own cadence (:00/:30 slots) ON THE NATIVE SCHEDULE
# GitHub delays and drops scheduled runs, so the runs do not tile 30-min windows.
# A threshold counts as crossed if it fell in the last CROSS_WINDOW_MIN: wider than
# the cadence so a late run cannot skip it. A rare duplicate beats a missed alert.
# These three are overridden per TRIGGER by rh-watchdog.yml (empty = the defaults here, which are
# the original behaviour). The standalone workflow's own */30 cron measured 1 to 3 runs a day on
# 2026-09-09 to 09-30, so on 10-01 it gained a trigger after every Arm A run (about every 15 min).
# CORRECTION 2026-10-05: alpaca-bot.yml ALSO ran this script at its :00/:30 triggers (2026-08-13 to
# 2026-10-06, then removed), on a shallow checkout, so the duration was unmeasurable there and it mailed
# "could not measure" at every slot. At 15-minute density the defaults would mail on every run while the
# laptop is down, so the workflow_run trigger passes a narrower window and thresholds for the stale path too.
CROSS_WINDOW_MIN = _env_int("WATCHDOG_CROSS_WINDOW_MIN", 45)
# Empty = alert on EVERY run while the heartbeat is stale (the original behaviour, right for a
# sparse schedule). Set, the stale alert fires once per threshold, in minutes THIS SESSION.
STALE_ALERT_MIN = _env_tuple("WATCHDOG_STALE_ALERT_MIN", ())

# DEGRADED means the daemon is ALIVE and pushing but cannot reach the broker, so it
# can neither see nor trade the account. It copies the last known equity forward to
# keep monitoring fed, which means `ts` stays FRESH and the staleness check above
# reports "healthy". That is how a 110-minute blackout on 2026-09-23 reached nobody:
# the laptop's own alert counter was pinned at 1 by a separate bug, and this
# watchdog was never built to look at `degraded` at all. Two independent alert paths,
# both silent, because FRESHNESS IS NOT HEALTH.
#
# Thresholds are stateless on purpose. Each fires ONCE, when the duration crosses it
# within the last check interval, so a long outage produces at most three mails
# instead of one every 30 minutes. No state file to go stale on a fresh runner.
# First threshold 60, not 45: since dd21a55 the daemon itself alerts at 15 min, so
# this is the independent BACKSTOP, not a second copy of the same mail.
DEGRADED_ALERT_MIN = (60, 180, 360)
# Minute-of-day for the once-a-day rh_HALT reminder. The pause is not urgent, so this is
# the first slot after the open where knowing still lets Devon act the same trading day.
# Gated through crossed() on the WALL CLOCK rather than on a duration, because the
# watchdog keeps no state between runs.
#
# 10:15, NOT 10:00, AND IT MUST NOT PRECEDE THE GRACE EXPIRY. Cloud measured this on
# 2026-11-03: at 10:00 the reminder arrived only on the */30 cron slots, and the header
# above records GitHub running that cron 1 to 3 times a day, so a held pause often got no
# reminder at all. The cause is an OVERLAP, not the cadence. The workflow_run trigger
# passes GRACE_MIN=30 and CROSS_WINDOW_MIN=20, and the grace is measured from the bot's
# 09:45 open, so that trigger is silent until 10:15 - while crossed() stops accepting a
# 10:00 threshold at 10:20. That left 10:15 to 10:19, five minutes, for runs arriving
# every fifteen. Putting the reminder AT the grace expiry opens the whole 20-minute
# window, which is wider than the run spacing and therefore guarantees a run lands in it:
# the property crossed()'s own docstring is built on.
#
# COUPLED to WATCHDOG_GRACE_MIN and to session_bounds' 09:45 open - it must stay >= open
# + grace, or the narrow-window trigger goes back to missing it silently. The test pins
# that RELATION rather than this number, so moving the grace fails a test instead of
# quietly un-fixing the reminder.
HALT_REMIND_AT_MIN = 10 * 60 + 15
GRACE_MIN = _env_int("WATCHDOG_GRACE_MIN", 5)     # Devon 2026-08-04: minimal delay after the open. The 30-min
                  # workflow schedule still lands the first live check at ~10:00 ET
                  # (first run once the market is open), which is right after the
                  # laptop's own first heartbeat - so a no-show laptop is caught by
                  # then without false-alarming before it has had a chance to push.


def crossed(mins, thresholds, window=None):
    """Thresholds this duration crossed within the last `window` minutes. Stateless: each one
    fires on the run (or runs) landing in the window after it, so a window wider than the run
    spacing guarantees at least one, and may give two."""
    window = CROSS_WINDOW_MIN if window is None else window
    return [t for t in thresholds if mins >= t > (mins - window)]


def _email(frm, pw, to, subject, body):
    m = MIMEText(body)
    m["Subject"], m["From"], m["To"] = subject, frm, to
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
        s.starttls()
        s.login(frm, pw)
        s.sendmail(frm, [to], m.as_string())


def alert(msg, urgent=False, subject=None, title=None):
    """Notify. Email always; SMS and push only when urgent.

    subject/title (optional) let arm_a_watch reuse these channels and their secrets rules for a different
    subject. Left as None, every line below behaves exactly as it always has for the laptop watchdog.

    Robinhood went index-only buy-and-hold on 2026-08-22, so a laptop that is
    down no longer means unenforced stops. The real consequence is that
    deposits sit uninvested until it is back, which is worth an email and is
    not worth a 3am text. Texting for a non-urgent condition is how alerting
    gets trained into background noise, which would matter if anything
    time-critical ever lands on this machine again.
    """
    print("ALERT:", msg)
    sent, pw = [], os.environ.get("GMAIL_APP_PASSWORD")

    # Slack first and outside the pw guard: an empty GMAIL_USER/PASSWORD secret
    # has blanked every other channel here before, and an outage alert is
    # exactly the message that must not go missing.
    try:
        import slack_notify
        if slack_notify.post(("*" + title + "*\n" if title else "*RH laptop bot needs attention*\n" if urgent
                              else "*RH laptop bot is not reporting (not urgent)*\n") + msg):
            sent.append("slack")
    except Exception as e:
        print("slack failed:", e)
    # SENDER comes from the secret only. It used to carry a hardcoded fallback so an
    # empty GMAIL_USER could not 535-fail the login silently (2026-08-04), but that
    # published Devon's bot sender address in a PUBLIC repo: the exact address his
    # alerts arrive from, which is a ready-made phishing kit. The fallback's real
    # job was making the failure LOUD, and the print below does that without
    # publishing anything. Safe because every workflow already passes GMAIL_USER.
    frm = (os.environ.get("GMAIL_USER") or "").strip()
    if pw and not frm:
        print("email SKIPPED: GMAIL_USER is unset/blank here - set the repo secret; "
              "deliberately NOT falling back to a hardcoded address")

    # RECIPIENT from secrets only. Devon set ALERT_EMAIL 2026-08-27 and it is wired
    # into rh-watchdog.yml, so the hardcoded address is gone from this public repo.
    # LAST RESORT is the sender, never a literal: if ALERT_EMAIL is missing or
    # mistyped the mail still goes to an inbox Devon owns, carrying a line saying
    # why, instead of vanishing. Deleting the fallback outright would have made a
    # typo in a write-only secret indistinguishable from silence, in Actions, where
    # the loud print goes to a log nobody reads.
    to = (os.environ.get("ALERT_EMAIL") or os.environ.get("ALERT_TO") or "").strip()
    misrouted = False
    if not to and frm:
        to, misrouted = frm, True
        print("ALERT_EMAIL unset/blank - falling back to the SENDER address so this "
              "still reaches an inbox; set the repo secret to fix routing")
    # THE NOTE GOES ON THE EMAIL ONLY (cloud's review, item 10). It was prepended to
    # `msg` itself, and the urgent SMS below sends msg[:140] - so 127 of those 140
    # characters were this note about email routing, leaving thirteen for the actual
    # emergency. The note is also meaningless on SMS and ntfy, which do not use
    # ALERT_EMAIL at all. Only fires when the secret is blank, which is why it went
    # unnoticed: it breaks the urgent path precisely when configuration is already
    # wrong, i.e. exactly when the alert matters most.
    email_msg = msg
    if misrouted:
        email_msg = ("[ALERT_EMAIL is not set, so this went to the sender address instead "
                     "of Devon's usual inbox. Set the ALERT_EMAIL repo secret.]"
                     + chr(10) + chr(10) + msg)

    # Email - subject carries no emoji; Devon prints mail to PDF by subject.
    if pw and frm and to:
        try:
            subject = subject or ("ALERT: RH laptop bot needs attention" if urgent
                                  else "RH laptop bot is not reporting (not urgent)")
            _email(frm, pw, to, subject, email_msg)
            sent.append("email")
        except Exception as e:
            print("email failed:", e)

    # SMS - SMS_TO is a full carrier email-to-SMS address (e.g. 5551234567@vtext.com),
    # set by Devon as a secret so his number never lands in this public repo.
    sms = os.environ.get("SMS_TO")
    if pw and frm and sms and urgent:
        try:
            _email(frm, pw, sms, "", msg[:140])
            sent.append("sms")
        except Exception as e:
            print("sms failed:", e)

    # ntfy push - unguessable topic kept in a secret, not committed.
    topic = os.environ.get("NTFY_TOPIC")
    if topic and urgent:
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topic, data=msg.encode(),
                headers={"Title": title or "RH laptop bot down", "Priority": "high",
                         "Tags": "rotating_light"})
            urllib.request.urlopen(req, timeout=15)
            sent.append("ntfy")
        except Exception as e:
            print("ntfy failed:", e)

    print("sent via:", ", ".join(sent) if sent else "NOTHING (no channels configured)")
    return bool(sent)


def notify(msg, urgent=False):
    """alert() as an exit code. An alert that reached NO channel must turn the run RED: a green
    run that says 'sent via: NOTHING' is a dead-man's switch that has quietly stopped switching,
    and a red run makes GitHub itself email the repo owner."""
    return 0 if alert(msg, urgent) else 1



def degraded_minutes(now_et):
    """How long the COMMITTED status has continuously carried `degraded`.

    Measured by walking git history rather than reading a field the laptop
    computes. That independence is the whole point: the laptop's own
    `_reconcile_fails` counter was pinned at 1 on 2026-09-23, so anything derived
    from it would have inherited the same blindness this check exists to cover.

    Returns None when history is unavailable (a shallow checkout), which the caller
    must treat as "cannot tell" and NOT as zero.
    """
    try:
        out = subprocess.run(["git", "log", "--format=%H", "-400", "--", STATUS_F],
                             capture_output=True, text=True, timeout=60)
        shas = out.stdout.split()
        if len(shas) < 2:
            return None                      # shallow clone or no history: cannot tell
    except Exception as e:
        print(f"degraded-duration check unavailable: {e}")
        return None
    oldest_degraded_ts = None
    for sha in shas:                          # newest first
        try:
            raw = subprocess.run(["git", "show", f"{sha}:{STATUS_F}"],
                                 capture_output=True, text=True, timeout=30).stdout
            snap = json.loads(raw)
        except Exception:
            continue
        if not snap.get("degraded"):
            break                             # first healthy snapshot ends the run
        try:
            oldest_degraded_ts = datetime.strptime(
                snap["ts"], "%Y-%m-%dT%H:%M").replace(tzinfo=bot.ET_TZ)
        except Exception:
            continue
    if oldest_degraded_ts is None:
        return None
    return (now_et - oldest_degraded_ts).total_seconds() / 60

def main():
    # Manual test path: verify every channel reaches the phone without waiting
    # for a real outage. Triggered from the Actions tab with force=true.
    if os.environ.get("FORCE_ALERT", "").lower() == "true":
        return notify("TEST alert from the RH watchdog. All three channels are wired up. "
                      "This is not a real outage.", urgent=True)

    et = datetime.now(bot.ET_TZ)
    open_now, _ = bot.check_market()
    if not open_now:
        print("market closed, weekend, or holiday - nothing to check")
        return 0

    # MEASURED FROM THE BOT'S OWN OPEN, NOT THE EXCHANGE'S (cloud's review, item 10).
    # This was hardcoded to 09:30 while the daemon's window opens at 09:45, so with
    # GRACE_MIN at 5 the grace expired at 09:35 - ten minutes before the bot was even
    # supposed to be working, which is a window where "not reporting" is the correct
    # state and alerting on it is a false alarm. since_open also caps the staleness
    # figures below, so every one of them read 15 minutes worse than the truth.
    # session_bounds is the single authority the docstring asks everything to use, so
    # an early close is modelled here too instead of being missed in this one file.
    _b = bot.session_bounds(et)
    _open = _b[0] if _b else et.replace(hour=9, minute=45, second=0, microsecond=0)
    since_open = (et - _open).total_seconds() / 60
    if since_open < GRACE_MIN:
        print(f"within {GRACE_MIN}m grace after the open - skipping")
        return 0

    try:
        with open(STATUS_F, encoding="utf-8-sig") as f:
            status = json.load(f)
        ts = datetime.strptime(status["ts"], "%Y-%m-%dT%H:%M").replace(tzinfo=bot.ET_TZ)
    except Exception as e:
        # A missing or unreadable status file during open market is itself a red flag.
        return notify(f"RH watchdog could not read {STATUS_F} ({e}). Check the laptop "
                      f"when convenient; Robinhood is index-only so nothing urgent is pending.")

    # DEGRADED comes first: a degraded daemon keeps `ts` fresh, so the staleness
    # check below would call it healthy and return before ever looking.
    stale = (et - ts).total_seconds() / 60
    # A degraded status that has also gone STALE means the daemon stopped pushing
    # (crash, or a Modern Standby hang overnight after an outage ran past the close).
    # That is a dead laptop, not a live one waiting on the broker: fall through to the
    # stale alert below. Without this the degraded branch returned early forever and
    # the watchdog went permanently silent, saying "the laptop is ALIVE".
    # A DELIBERATE PAUSE IS NOT AN OUTAGE. rh_HALT is Devon's kill switch; the daemon
    # now keeps the heartbeat up while it is held so monitoring can tell the pause
    # from a dead machine (cloud's review, item 10). Paging about a state someone
    # chose on purpose is how a watchdog gets muted, so this reports calmly, rarely,
    # and never as urgent. If the heartbeat then goes stale the checks below still
    # fire: a halted bot that stops reporting really is a dead laptop.
    if status.get("degraded") == "halted_by_rh_HALT" and stale < STALE_MIN:
        print(f"rh_HALT is present and the laptop is reporting normally "
              f"({stale:.0f}m since the last heartbeat) - trading is paused on purpose")
        # notify() carries no subject - it is notify(msg, urgent) and forwards only
        # those two to alert(). My first version passed subject= and would have raised
        # TypeError the first time rh_HALT existed; the suite stayed green because
        # nothing exercised this branch, which is precisely what the surviving mutant
        # for it was saying. So the distinguishing words lead the BODY instead.
        #
        # A REMINDER, NOT A STREAM - AND NOT ONCE EVER EITHER. This branch shipped with
        # no gate at all, unlike every other branch below, so it announced the same pause
        # on every run: the watchdog is stateless and runs after each Arm A pass plus a
        # */30 cron, which made a halt held through one trading day a few dozen identical
        # emails. The comment above this `if` claimed it "reported once" and the code
        # never did. Cloud's follow-up review, 2026-11-03.
        #
        # Exactly once is not available here and is not the right target. crossed() is
        # the only dedupe this file has and it is deliberately "at least one, maybe two"
        # (see its docstring); a stateless check invoked by two schedulers has nowhere
        # durable to remember what it already said. Nor should a pause be announced a
        # single time and never again - the failure mode of that is Devon forgetting the
        # bot is paused while deposits sit uninvested, which is the whole reason a halt
        # is worth an email in the first place. So: announce a NEW pause, then remind
        # once a day, and stay quiet in between. A few per day, not a few dozen.
        #
        # held is None (shallow checkout, no history) counts as NEW and announces. I had
        # it the other way first, reasoning that for a deliberate pause the storm is the
        # dangerous direction and a delay costs nothing. Cloud's control test said no:
        # with no measurable duration the pause went unannounced entirely, and this
        # file's standing rule - an unmeasurable duration is never read as zero, say so
        # and alert rather than infer fine - is the right one here too. A pause nobody
        # is told about is the failure that matters; repeating it is only noise.
        held = degraded_minutes(et)
        new_pause = held is None or held < CROSS_WINDOW_MIN
        if new_pause or crossed(et.hour * 60 + et.minute, (HALT_REMIND_AT_MIN,)):
            return notify("PAUSED BY rh_HALT, not broken. The laptop is alive and reporting "
                          "normally; nothing is trading and no stops are being enforced "
                          "because nothing is being managed. Delete the rh_HALT file in the "
                          "repo folder to resume.", urgent=False)
        print("the pause has already been reported; next reminder at "
              f"{HALT_REMIND_AT_MIN // 60:02d}:{HALT_REMIND_AT_MIN % 60:02d} ET")
        return 0
    if status.get("degraded") and stale < STALE_MIN:
        why = str(status.get("degraded"))
        mins = degraded_minutes(et)
        if mins is None:
            # Cannot measure duration. Say so and alert anyway rather than infer zero:
            # a check that reports "fine" when it could not look is the failure this
            # whole change exists to remove.
            return notify(f"Arm B is DEGRADED ({why}) and the watchdog could not measure how "
                          f"long, so this may repeat. The laptop is alive and pushing but "
                          f"cannot reach the broker, so it can neither see nor trade the "
                          f"account. Not urgent: index-only has no stops waiting to fire.")
        # Minutes THIS SESSION, never more than the time since the open. An outage that ran past
        # yesterday's close and is still going has a wall-clock duration of many hours, every
        # threshold is then long past, and the stateless window below would never match again:
        # the watchdog would stay silent all day while the account could not trade.
        mins = min(mins, since_open)
        if crossed(mins, DEGRADED_ALERT_MIN):
            return notify(chr(10).join([
                f"Arm B has been unable to trade for about {int(mins)} minutes "
                f"({why}), during open market.",
                "",
                "The laptop is ALIVE and still pushing status, so its heartbeat looks",
                "fresh. It just cannot reach the broker. Deposits sit uninvested and",
                "the ETFs do not rebalance until it is back.",
                "",
                "NOT URGENT: Robinhood is index-only, so no stops are going unenforced.",
                "",
                "Usually self-heals (a Claude usage limit). If it persists, the CLI",
                "login has probably expired, which never recovers on its own:",
                '    claude -p "Reply with exactly: ALIVE"',
                "and if that fails:  claude auth login",
            ]))
        print(f"Arm B degraded ({why}) for {int(mins)}m this session - no threshold crossed "
              f"in the last {CROSS_WINDOW_MIN}m (an earlier one fired on an earlier run)")
        return 0

    if stale < STALE_MIN:
        print(f"bot healthy - last heartbeat {int(stale)}m ago ({status['ts']} ET)")
        return 0

    if STALE_ALERT_MIN:
        silent = min(stale, since_open)       # a heartbeat missing since yesterday is missing since the open
        if not crossed(silent, STALE_ALERT_MIN):
            print(f"laptop silent {int(silent)}m this session - no threshold crossed in the last "
                  f"{CROSS_WINDOW_MIN}m (an earlier one fired on an earlier run)")
            return 0
    return notify(chr(10).join([
        f"The RH laptop bot has stopped reporting. Last heartbeat {status['ts']} ET, "
        f"about {int(stale)} min ago, during open market."
        + (f" It was already DEGRADED ({status.get('degraded')}) when it went quiet."
           if status.get("degraded") else ""),
        "",
        "NOT URGENT. Robinhood is index-only buy-and-hold, so there are no stops",
        "waiting to fire. The cost of downtime is that deposits sit uninvested and",
        "the ETFs do not rebalance until it is back.",
        "",
        "The daemon auto-starts on boot and login, so a reboot usually fixes it.",
        "If it is running but still silent, the Claude CLI login has probably",
        "lapsed. Diagnose with:",
        '    claude -p "Reply with exactly: ALIVE"',
        "and if that fails:  claude auth login",
        "Do NOT trust `claude mcp list`; it reports Connected even when the bridge",
        "cannot authenticate at all.",
    ]))


if __name__ == "__main__":
    sys.exit(main())
