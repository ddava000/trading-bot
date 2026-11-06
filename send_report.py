"""
send_report.py — email a committed report file through the bot's existing Gmail.

The password lives only in the GMAIL_APP_PASSWORD GitHub secret, so a report can
only be sent from inside a GitHub Action (email-report.yml), never from a laptop
or dev box. Reuses alpaca_bot.send_email so there is one mail code path.

Usage (inside the workflow):  python send_report.py reports/<file>.md
Subject = the file's first heading line (ASCII only; Devon prints mail to PDF and
the subject becomes the filename, where emoji/em dashes break).
"""
import sys, os
os.environ.setdefault("ALPACA_API_KEY", "report-only")     # avoid the module's
os.environ.setdefault("ALPACA_SECRET_KEY", "report-only")  # hard key requirement
import alpaca_bot as bot

path = sys.argv[1] if len(sys.argv) > 1 else "reports/latest.md"
body = open(path, encoding="utf-8").read()
subject = "Trading bots - report"
for line in body.splitlines():
    if line.strip():
        subject = line.lstrip("# ").strip()
        break
subject = "".join(c for c in subject if ord(c) < 128)[:120]   # plain ASCII
bot.send_email(subject, body)
print(f"emailed: {subject}")
