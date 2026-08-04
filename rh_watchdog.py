#!/usr/bin/env python3
"""Cloud dead-man's-switch for the Robinhood laptop bot.

Runs in GitHub Actions on a schedule, independent of the laptop. The laptop
pushes rh_status.json with an ET "ts" every ~15 min while it is alive; if that
timestamp goes stale during CONFIRMED open-market hours, this alerts Devon on
every configured channel at once: email, ntfy push, and SMS.

Each channel is gated on its own secret, so the watchdog degrades gracefully:
email works the moment GMAIL_APP_PASSWORD exists, and push/SMS light up as their
secrets are added. On a normal day it prints one line and exits 0 (market closed,
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
GRACE_MIN = 45    # after the open, give the laptop time to boot and push once


def _email(frm, pw, to, subject, body):
    m = MIMEText(body)
    m["Subject"], m["From"], m["To"] = subject, frm, to
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
        s.starttls()
        s.login(frm, pw)
        s.sendmail(frm, [to], m.as_string())


def alert(msg):
    """Fire every configured channel. One channel failing never blocks another."""
    print("ALERT:", msg)
    sent, pw = [], os.environ.get("GMAIL_APP_PASSWORD")
    frm = os.environ.get("GMAIL_USER", "devonsdummy@gmail.com")

    # Email — subject carries no emoji; Devon prints mail to PDF by subject.
    if pw:
        try:
            _email(frm, pw, os.environ.get("ALERT_EMAIL", "devondavasher@gmail.com"),
                   "ALERT: RH laptop bot is not reporting", msg)
            sent.append("email")
        except Exception as e:
            print("email failed:", e)

    # SMS — SMS_TO is a full carrier email-to-SMS address (e.g. 5551234567@vtext.com),
    # set by Devon as a secret so his number never lands in this public repo.
    sms = os.environ.get("SMS_TO")
    if pw and sms:
        try:
            _email(frm, pw, sms, "", msg[:140])
            sent.append("sms")
        except Exception as e:
            print("sms failed:", e)

    # ntfy push — unguessable topic kept in a secret, not committed.
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
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
              "This is not a real outage.")
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
        alert(f"RH watchdog could not read {STATUS_F} ({e}). Check the laptop.")
        return 0

    stale = (et - ts).total_seconds() / 60
    if stale < STALE_MIN:
        print(f"bot healthy — last heartbeat {int(stale)}m ago ({status['ts']} ET)")
        return 0

    alert(f"RH laptop bot has gone SILENT: last heartbeat {status['ts']} ET, "
          f"about {int(stale)} min ago, during open market. Check the laptop "
          f"(it auto-starts on boot/login).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
