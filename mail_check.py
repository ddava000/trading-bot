#!/usr/bin/env python3
"""
AGENT_MAIL.md daily watcher — tells Devon when a session has unread mail.

WHY THIS EXISTS. Three Claude sessions coordinate through AGENT_MAIL.md, but none
of them runs continuously, so an entry only gets read when that session next opens.
On 2026-08-23 the laptop found the cadence protocol was asserting a read frequency
that nothing in the code actually delivered: the daemon pulled the file to disk and
nothing read it, so mail addressed to it could sit for days. Devon's rule after that:
all three check at least DAILY. A cadence with no code behind it is fiction, so this
is the code.

It does NOT parse or act on message content. It reports that mail arrived, which is
what prompts a session to be opened. Deliberately dependency-free (stdlib only, no
Alpaca keys) so any of the three can run it from any trigger.

Usage:
  python mail_check.py                 # report new entries for anyone
  python mail_check.py --for audit     # only entries addressed to audit/both/all
  python mail_check.py --quiet         # no email, just exit code (0 none, 1 new mail)
"""
import os, re, json, sys, smtplib
from email.mime.text import MIMEText

MAILBOX = "AGENT_MAIL.md"
STATE   = ".mail_check_state.json"
SESSIONS = ("cloud", "laptop", "audit")
BROADCAST = ("both", "all")

HDR = re.compile(r"^## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ET\]\s+(\w+)\s*->\s*([\w]+)", re.M)


def entries(text):
    out, ms = [], list(HDR.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end].strip()
        out.append({"ts": m.group(1), "from": m.group(2).lower(),
                    "to": m.group(3).lower(), "hdr": m.group(0).strip(),
                    "first": next((l for l in body.split("\n") if l.strip()), "")})
    return out


def addressed_to(e, who):
    return who is None or e["to"] == who or e["to"] in BROADCAST


def send(subject, body):
    """Same Gmail path the bot uses. Falls back exactly like alpaca_bot.send_email:
    a MISSING secret expands to an empty string, so never trust os.environ.get's
    default alone (that is what 535-failed every alert channel once)."""
    pw = os.environ.get("GMAIL_APP_PASSWORD") or ""
    if not pw:
        print("[email skipped — GMAIL_APP_PASSWORD not set]"); return False
    frm = os.environ.get("GMAIL_USER") or "devonsdummy@gmail.com"
    to  = os.environ.get("ALERT_TO")   or "devondavasher@gmail.com"
    try:
        msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls(); s.login(frm, pw); s.sendmail(frm, [to], msg.as_string())
        print(f"[email sent → {to}: {subject}]"); return True
    except Exception as e:
        print(f"[email failed: {e}]"); return False


def main():
    who = None
    if "--for" in sys.argv:
        who = sys.argv[sys.argv.index("--for") + 1].lower()
        if who not in SESSIONS:
            print(f"unknown session {who!r}; expected one of {SESSIONS}"); return 2
    quiet = "--quiet" in sys.argv

    try:
        text = open(MAILBOX, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"{MAILBOX} not found"); return 2

    all_e = entries(text)
    if not all_e:
        print("no entries parsed — mailbox format may have changed"); return 2

    try:
        seen = json.load(open(STATE)).get("last_hdr", "")
    except Exception:
        seen = ""

    # First run adopts the backlog silently rather than emailing weeks of history.
    if not seen:
        json.dump({"last_hdr": all_e[-1]["hdr"]}, open(STATE, "w"), indent=1)
        print(f"first run — adopted backlog of {len(all_e)} entries, no email sent")
        return 0

    idx = next((i for i, e in enumerate(all_e) if e["hdr"] == seen), None)
    new = all_e[idx + 1:] if idx is not None else all_e
    if idx is None:
        print("last-seen entry not found (rewritten?) — reporting only the newest")
        new = all_e[-1:]

    # A session's own entries are not mail TO it.
    fresh = [e for e in new if addressed_to(e, who) and not (who and e["from"] == who)]
    json.dump({"last_hdr": all_e[-1]["hdr"]}, open(STATE, "w"), indent=1)

    if not fresh:
        print(f"no new mail{' for ' + who if who else ''} ({len(new)} new entr(y/ies), none addressed)")
        return 0

    label = who or "the sessions"
    lines = [f"{len(fresh)} new AGENT_MAIL entr{'y' if len(fresh)==1 else 'ies'} for {label}:", ""]
    for e in fresh:
        lines += [f"  [{e['ts']} ET] {e['from']} -> {e['to']}", f"      {e['first'][:100]}", ""]
    lines += ["Open a session in the repo and read AGENT_MAIL.md.",
              "This watcher reports that mail arrived; it does not read or act on content."]
    body = "\n".join(lines)
    print(body)
    if not quiet:
        send(f"AGENT_MAIL: {len(fresh)} new for {label}", body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
