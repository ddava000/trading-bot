#!/usr/bin/env python3
"""Prove the REAL alert path reaches Slack from a GitHub runner.

`slack_notify.py --test` only proves the webhook secret and the channel work. It
does NOT prove that alpaca_bot.send_email -> _slack -> slack_notify actually
resolves the import and fires from inside the runner's working directory, which
is the thing that carries every genuine alert.

This calls the real send_email. GMAIL_APP_PASSWORD is deliberately NOT passed to
this workflow, so the email half no-ops and Devon gets no junk mail, while the
Slack half runs for real. If this posts, an actual trade alert will post too.

House rule this exists to satisfy: ask what the code does in the environment it
ACTUALLY runs in. A guard verified only on a dev box is a guard not verified.
"""
import os, sys

# alpaca_bot demands these at import time; it makes no API call on this path.
os.environ.setdefault("ALPACA_API_KEY", "probe-only-no-api-call")
os.environ.setdefault("ALPACA_SECRET_KEY", "probe-only-no-api-call")

import alpaca_bot as bot

if bot.GMAIL_APP_PW:
    print("REFUSING: GMAIL_APP_PASSWORD is set, this probe would send real mail.")
    sys.exit(2)

bot.send_email(
    "Slack alert path probe",
    "This came through alpaca_bot.send_email on a GitHub runner, which is the "
    "same function every real trade, stop-loss and rejection alert uses.\n\n"
    "No email was sent: the probe workflow is not given the Gmail secret.",
)

import slack_notify
if not slack_notify.enabled():
    print("FAIL: SLACK_WEBHOOK_URL not visible to this step")
    sys.exit(1)
print("probe complete")
