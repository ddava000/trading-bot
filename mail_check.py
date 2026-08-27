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

Two modes, because the two kinds of runner need different things:
  STATEFUL (default) tracks the last entry it saw in a gitignored per-runner file.
    Right for a long-lived machine like the laptop.
  STATELESS (--since-hours N) reports entries newer than N hours and keeps no state.
    Right for CI: a GitHub runner is fresh every time, so a state file would never
    exist, every run would look like a first run, and it would silently adopt the
    backlog and NEVER report anything. For a daily cron, "addressed to me in the
    last 24h" is the same question anyway.

Usage:
  python mail_check.py                          # new entries for anyone (stateful)
  python mail_check.py --for audit              # only entries addressed to audit/both/all
  python mail_check.py --for audit --since-hours 24   # stateless, for a daily cron
  python mail_check.py --quiet                  # no email, exit code only (0 none, 1 new)
"""
import os, re, json, sys, smtplib

NL = chr(10)

# How far back an entry with an unparseable timestamp still counts as "recent"
# in stateless mode. Bounds the repeat without going back to silently dropping it.
UNPARSED_TAIL = 10
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText

MAILBOX = "AGENT_MAIL.md"
STATE   = ".mail_check_state.json"
SESSIONS = ("cloud", "laptop", "audit")
BROADCAST = ("both", "all")

# Capture the timestamp, do NOT validate it. laptop's fe8c2e0 parser cross-check
# (2026-08-23) found the strict version silently skipped ordinary typos: a
# single-digit hour, a missing "ET", or seconds. The format is documented at the top
# of AGENT_MAIL.md, so the parser does not need to re-enforce it, and being strict
# about a field nobody reads costs mail.
HDR = re.compile(r"^## \[([^\]]+)\]\s*(\w+)\s*->\s*(\w+)", re.M)
_TZ_SUFFIX = re.compile(r"\s*(ET|EST|EDT|UTC|Z)\s*$", re.I)


def parse_ts(ts, tz):
    """Best-effort entry timestamp. Returns None if genuinely unparseable.
    Tolerates a missing/na timezone suffix, seconds, and a single-digit hour."""
    clean = _TZ_SUFFIX.sub("", ts).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def entries(text):
    out, ms = [], list(HDR.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end].strip()
        out.append({"ts": _TZ_SUFFIX.sub("", m.group(1)).strip(), "from": m.group(2).lower(),
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
    # No address literals: this repo is PUBLIC. Same pattern as alpaca_bot and
    # rh_watchdog -- recipient from ALERT_EMAIL, last resort is the SENDER, never a
    # literal, so a mistyped write-only secret cannot look like silence.
    frm = (os.environ.get("GMAIL_USER") or "").strip()
    to  = (os.environ.get("ALERT_EMAIL") or os.environ.get("ALERT_TO") or "").strip()
    if not frm:
        print("[mail_check: GMAIL_USER unset/blank - cannot send, set the repo secret]")
        return
    if not to:
        print("[mail_check: ALERT_EMAIL unset/blank - sending to the sender address]")
        to = frm
    try:
        msg = MIMEText(body); msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as s:
            s.starttls(); s.login(frm, pw); s.sendmail(frm, [to], msg.as_string())
        print(f"[email sent → {to}: {subject}]"); return True
    except Exception as e:
        print(f"[email failed: {e}]"); return False


def main():
    whos = [None]
    if "--for" in sys.argv:
        raw = sys.argv[sys.argv.index("--for") + 1].lower()
        whos = [w.strip() for w in raw.split(",") if w.strip()]
        bad = [w for w in whos if w not in SESSIONS]
        if bad:
            print(f"unknown session(s) {bad}; expected from {SESSIONS}"); return 2
    quiet = "--quiet" in sys.argv
    since = None
    if "--since-hours" in sys.argv:
        try:
            since = float(sys.argv[sys.argv.index("--since-hours") + 1])
        except (IndexError, ValueError):
            print("--since-hours needs a number"); return 2

    try:
        text = open(MAILBOX, encoding="utf-8").read()
    except FileNotFoundError:
        print(f"{MAILBOX} not found"); return 2

    all_e = entries(text)
    if not all_e:
        print("no entries parsed — mailbox format may have changed"); return 2

    # Stateless path: entries newer than N hours. No state file, so a fresh CI
    # runner behaves identically every time.
    if since is not None:
        ET = ZoneInfo("America/New_York")
        cutoff = datetime.now(ET) - timedelta(hours=since)
        new, unparsed, aged_out = [], 0, 0
        tail_start = len(all_e) - UNPARSED_TAIL
        for i, e in enumerate(all_e):
            when = parse_ts(e["ts"], ET)
            if when is None:
                # BIAS TOWARD REPORTING, BUT BOUNDED. Skipping these is the
                # silent-miss failure this watcher exists to prevent: a typo'd stamp
                # would drop a real message and nobody would ever know. But this path
                # keeps NO state, so reporting them unconditionally meant every
                # undateable entry reappeared in the daily digest forever, and a line
                # that shows up every day is how a digest becomes wallpaper. That
                # loses every message in it, which is the same silent miss through
                # the other door. (Empirically: a permanent Alpaca crypto rejection
                # firing ~26 identical alerts a day had already trained Devon to
                # ignore it. Caught by a peer session, 2026-08-25.)
                # So: undateable entries report only while they are among the newest
                # UNPARSED_TAIL, then age out on position instead of time.
                if i >= tail_start:
                    unparsed += 1
                    new.append(e)
                else:
                    aged_out += 1
                continue
            if when >= cutoff:
                new.append(e)
        if aged_out:
            # Never silent about the suppression itself.
            print(f"[{aged_out} undateable entr(y/ies) older than the newest "
                  f"{UNPARSED_TAIL} - aged out, not reported]")
        if unparsed:
            print(f"[{unparsed} entr(y/ies) had an unparseable timestamp — included rather than skipped]")
        buckets = {w: [e for e in new
                       if addressed_to(e, w) and not (w and e["from"] == w)] for w in whos}
        return _report(buckets, quiet, f"in the last {since:g}h")

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
    buckets = {w: [e for e in new
                   if addressed_to(e, w) and not (w and e["from"] == w)] for w in whos}
    json.dump({"last_hdr": all_e[-1]["hdr"]}, open(STATE, "w"), indent=1)

    return _report(buckets, quiet, f"({len(new)} new entr(y/ies))")


def _report(buckets, quiet, ctx):
    """ONE email covering every session asked about, rather than one per session.
    Devon was on three separate notification paths for a single file (audit's step,
    cloud's step, and the laptop daemon), all saying the same thing on a busy day.
    Sections are only included for sessions that actually have mail."""
    hits = {w: es for w, es in buckets.items() if es}
    names = ", ".join(w or "the sessions" for w in buckets)
    if not hits:
        print(f"no new mail for {names} {ctx}")
        return 0

    total = sum(len(es) for es in hits.values())
    lines = []
    for w, es in hits.items():
        label = w or "the sessions"
        lines.append(f"{len(es)} for {label}:")
        for e in es:
            lines += [f"  [{e['ts']} ET] {e['from']} -> {e['to']}",
                      f"      {e['first'][:100]}"]
        lines.append("")
    lines += ["Open a session in the repo and read AGENT_MAIL.md.",
              "This watcher reports that mail arrived; it does not read or act on content."]
    body = NL.join(lines)
    print(body)
    if not quiet:
        who_txt = " + ".join(w or "sessions" for w in hits)
        send(f"AGENT_MAIL: {total} new for {who_txt}", body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
