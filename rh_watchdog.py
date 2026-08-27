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
import os, sys, json, smtplib, urllib.request
from datetime import datetime
from email.mime.text import MIMEText

# Reuse the bot's own holiday-aware market clock so the watchdog and the bot
# agree on what "open" means, instead of duplicating the holiday list here.
# check_market() needs no live API call; the keys only satisfy the import.
os.environ.setdefault("ALPACA_API_KEY", "unused-in-watchdog")
os.environ.setdefault("ALPACA_SECRET_KEY", "unused-in-watchdog")
import alpaca_bot as bot

STATUS_F  = "rh_status.json"
STALE_MIN = 30    # heartbeat is 15 min, so >30 = ~2 missed pushes = likely down
GRACE_MIN = 5     # Devon 2026-08-04: minimal delay after the open. The 30-min
                  # workflow schedule still lands the first live check at ~10:00 ET
                  # (first run once the market is open), which is right after the
                  # laptop's own first heartbeat — so a no-show laptop is caught by
                  # then without false-alarming before it has had a chance to push.


def _email(frm, pw, to, subject, body):
    m = MIMEText(body)
    m["Subject"], m["From"], m["To"] = subject, frm, to
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
        s.starttls()
        s.login(frm, pw)
        s.sendmail(frm, [to], m.as_string())


def alert(msg, urgent=False):
    """Notify. Email always; SMS and push only when urgent.

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
        if slack_notify.post(("*RH laptop bot needs attention*\n" if urgent
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
    if misrouted:
        msg = ("[ALERT_EMAIL is not set, so this went to the sender address instead "
               "of Devon's usual inbox. Set the ALERT_EMAIL repo secret.]"
               + chr(10) + chr(10) + msg)

    # Email — subject carries no emoji; Devon prints mail to PDF by subject.
    if pw and frm and to:
        try:
            subject = ("ALERT: RH laptop bot needs attention" if urgent
                       else "RH laptop bot is not reporting (not urgent)")
            _email(frm, pw, to, subject, msg)
            sent.append("email")
        except Exception as e:
            print("email failed:", e)

    # SMS — SMS_TO is a full carrier email-to-SMS address (e.g. 5551234567@vtext.com),
    # set by Devon as a secret so his number never lands in this public repo.
    sms = os.environ.get("SMS_TO")
    if pw and frm and sms and urgent:
        try:
            _email(frm, pw, sms, "", msg[:140])
            sent.append("sms")
        except Exception as e:
            print("sms failed:", e)

    # ntfy push — unguessable topic kept in a secret, not committed.
    topic = os.environ.get("NTFY_TOPIC")
    if topic and urgent:
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topic, data=msg.encode(),
                headers={"Title": "RH laptop bot down", "Priority": "high",
                         "Tags": "rotating_light"})
            urllib.request.urlopen(req, timeout=15)
            sent.append("ntfy")
        except Exception as e:
            print("ntfy failed:", e)

    print("sent via:", ", ".join(sent) if sent else "NOTHING (no channels configured)")


def main():
    # Manual test path: verify every channel reaches the phone without waiting
    # for a real outage. Triggered from the Actions tab with force=true.
    if os.environ.get("FORCE_ALERT", "").lower() == "true":
        alert("TEST alert from the RH watchdog. All three channels are wired up. "
              "This is not a real outage.", urgent=True)
        return 0

    et = datetime.now(bot.ET_TZ)
    open_now, _ = bot.check_market()
    if not open_now:
        print("market closed, weekend, or holiday — nothing to check")
        return 0

    since_open = (et - et.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
    if since_open < GRACE_MIN:
        print(f"within {GRACE_MIN}m grace after the open — skipping")
        return 0

    try:
        with open(STATUS_F, encoding="utf-8-sig") as f:
            status = json.load(f)
        ts = datetime.strptime(status["ts"], "%Y-%m-%dT%H:%M").replace(tzinfo=bot.ET_TZ)
    except Exception as e:
        # A missing or unreadable status file during open market is itself a red flag.
        alert(f"RH watchdog could not read {STATUS_F} ({e}). Check the laptop "
              f"when convenient; Robinhood is index-only so nothing urgent is pending.")
        return 0

    stale = (et - ts).total_seconds() / 60
    if stale < STALE_MIN:
        print(f"bot healthy — last heartbeat {int(stale)}m ago ({status['ts']} ET)")
        return 0

    alert(chr(10).join([
        f"The RH laptop bot has stopped reporting. Last heartbeat {status['ts']} ET, "
        f"about {int(stale)} min ago, during open market.",
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
