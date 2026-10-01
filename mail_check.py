#!/usr/bin/env python3
"""
AGENT_MAIL.md daily watcher - tells Devon when a session has unread mail.

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

# What Devon calls each session (Devon 2026-10-01: he could never tell WHO needs to check mail).
# Keys are the bare session names the mailbox headings use; values are the names he says out loud.
# A session he has not named yet shows its bare name. Add names here as he gives them.
#
# Mail addressed to `cloud` names CLOUD, the interactive cloud session (Devon chose that
# 2026-10-01 when the cloud daily-check chat had named itself BOT DAILY CHECK; that chat signs
# cloud[daily] and still reads cloud mail by itself each weekday afternoon, so it never needs waking
# for mail). Names are learned from the sessions: each introduces itself in AGENT_MAIL and whoever
# hears it adds one line here. Heading tokens (cloud, laptop, audit) never change.
# Both values below were given by Devon himself on 2026-10-01 (cloud: CLOUD, chosen from a
# question asked of him; laptop: LAPTOP BOT, his words to that session at 16:06 ET). Do not change
# either without asking him: two sessions alternating one name is worse than either value.
# TWO laptop sessions have names (Devon 2026-10-01, asked directly and he chose two names over
# one): the interactive session that owns the daemon is LAPTOP BOT, the scheduled daily check is
# LAPTOP BOT DAILY CHECK. They share the heading token `laptop`, so the RECIPIENT QUALIFIER
# tells them apart: `-> laptop` is LAPTOP BOT, `-> laptop[daily]` is LAPTOP BOT DAILY CHECK.
# A "name[qualifier]" key here wins over the bare name when an entry is addressed that way.
# Broadcasts (`-> all`, `-> both`) name the bare session.
# The weekly audit (token audit) is a GitHub Actions workflow, not a chat: it cannot introduce itself
# and Devon cannot tell it anything. He asked CLOUD to name it (2026-10-01): BOT WEEKLY AUDIT, after
# BOT DAILY CHECK. See SELF_READING for why alerts never tell him to wake it.
# The five named sessions (tests/test_session_roster.py pins this exactly; changing a name needs Devon):
#   cloud CLOUD | cloud[daily] BOT DAILY CHECK | laptop LAPTOP BOT | laptop[daily] LAPTOP BOT DAILY CHECK
#   | audit BOT WEEKLY AUDIT. Mail addressed to a bare token names the interactive session.
DISPLAY = {"cloud": "CLOUD", "cloud[daily]": "BOT DAILY CHECK",
           "laptop": "LAPTOP BOT", "laptop[daily]": "LAPTOP BOT DAILY CHECK",
           "audit": "BOT WEEKLY AUDIT"}

# Sessions that are not a chat Devon can open and say "check mail" to. They read the mailbox on their
# own schedule, so an alert says that instead of asking him to wake them. name -> when it reads.
SELF_READING = {"audit": "every Sunday"}


def _base(key):
    return key.split("[")[0]


def wake_keys(keys):
    """The DISPLAY keys Devon can actually wake (everything except the self-reading sessions)."""
    return [k for k in keys if _base(k) not in SELF_READING]


def display(w):
    return DISPLAY.get(w, w or "the sessions")


def names_text(names):
    """'A', 'A and B', 'A, B and C'."""
    shown = [display(n) for n in names]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def name_key(to, tag):
    """DISPLAY key for a recipient: "laptop[daily]" if that qualified name exists, else "laptop"."""
    k = "%s[%s]" % (to, (tag or "").lower())
    return k if tag and k in DISPLAY else to


def action_line(names):
    """The one sentence an email or Slack post leads with, so the reader knows who to wake."""
    wake = wake_keys(names)
    if not wake:
        when = SELF_READING.get(_base(names[0]), "on its schedule") if names else "on its schedule"
        return names_text(names) + " reads mail by itself " + when + ". Nothing for you to do."
    return "Have " + names_text(wake) + " check mail."

# Capture the timestamp, do NOT validate it. laptop's fe8c2e0 parser cross-check
# (2026-08-23) found the strict version silently skipped ordinary typos: a
# single-digit hour, a missing "ET", or seconds. The format is documented at the top
# of AGENT_MAIL.md, so the parser does not need to re-enforce it, and being strict
# about a field nobody reads costs mail.
#
# A sender or recipient may carry a bracketed qualifier: cloud[daily], cloud[35819496],
# laptop[daily]. The first version captured the name with a bare (\w+) and so could not match
# those at all: 25 of the 46 live entries, EVERY one from a cloud session or a daily check, were
# invisible to this watcher, so a laptop digest could never report mail from the cloud. The
# qualifier is accepted and dropped; group 2 and 3 stay the bare session names.
# (tests/test_mail_check.py also checks every heading in the real mailbox against this.)
# Group 4 is the RECIPIENT qualifier (None if absent); see DISPLAY for why it is kept.
HDR = re.compile(r"^## \[([^\]]+)\]\s*(\w+)(?:\[[^\]]*\])?\s*->\s*(\w+)(?:\[([^\]]*)\])?", re.M)
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


_SENDER = re.compile(r"^## \[[^\]]*\]\s*(\w+)(?:\[([^\]]*)\])?")
_SUBJECT = re.compile(r"->\s*\w+(?:\[[^\]]*\])?\s*\[(.*)\]\s*$")


def entries(text):
    out, ms = [], list(HDR.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end():end].strip()
        eol = text.find("\n", m.start())
        line_text = text[m.start(): eol if eol != -1 else len(text)].strip()
        sm, subj = _SENDER.match(line_text), _SUBJECT.search(line_text)
        out.append({"ts": _TZ_SUFFIX.sub("", m.group(1)).strip(), "from": m.group(2).lower(),
                    "to": m.group(3).lower(), "to_tag": ((m.group(4) if m.re.groups >= 4 else "") or "").lower(),
                    "hdr": m.group(0).strip(),
                    # Position, not time: the FILE ORDER is the only order every session agrees on. Stamps
                    # are typed by hand and have been wrong in both directions (2026-09-30 and 10-01).
                    "line": text.count("\n", 0, m.start()) + 1, "line_text": line_text,
                    "from_tag": ((sm.group(2) if sm else "") or "").lower(),
                    "subject": subj.group(1) if subj else "",
                    "first": next((l for l in body.split("\n") if l.strip()), "")})
    return out


def addressed_to(e, who):
    return who is None or e["to"] == who or e["to"] in BROADCAST


# ---------------------------------------------------------------------------------------------
# THE ONE WAY EVERY SESSION CHECKS MAIL (Devon 2026-10-01: "check mail" must mean the same thing to all).
#
#   python mail_check.py --inbox "LAPTOP BOT" --ack      what is unread for me; then move my marker
#   python mail_check.py --aligned                        who has confirmed the latest ALIGNMENT CHECK
#
# Why it exists: LAPTOP BOT looked for new mail by comparing timestamps with its own last entry, which
# it had stamped 25 minutes into the future, so two entries addressed to it (including an open decision)
# looked old and it reported "no new mail". UNREAD HERE IS BY FILE POSITION AFTER A PER-SESSION MARKER,
# never by timestamp. The marker lives in a gitignored file on the machine that runs the check, keyed by
# session, so the two laptop sessions (and the two cloud ones) never consume each other's mail.
CURSORS_FILE = ".mail_inbox_cursors.json"
INBOX_RECENT = 10
ALIGN_REQUEST, ALIGN_REPLY = "ALIGNMENT CHECK", "ALIGNED"


def resolve_key(name):
    """'LAPTOP BOT', 'laptop[daily]', 'cloud' -> the DISPLAY key, or None."""
    low = (name or "").strip().lower()
    for k, v in DISPLAY.items():
        if low in (k.lower(), v.lower()):
            return k
    return None


def _is_daily(key):
    return key.endswith("[daily]")


def addressed_to_key(key, e):
    """Is this entry mail for the session `key`? A daily check reads every entry to its base token (it is
    the standing reader of that mailbox); an interactive session skips entries addressed to its daily sibling."""
    if e["to"] in BROADCAST:
        return True
    if e["to"] != _base(key):
        return False
    return True if _is_daily(key) else e.get("to_tag") != "daily"


def sent_by_key(key, e):
    """Was it written by this very session (not a sibling that shares the heading token)?"""
    return e["from"] == _base(key) and (e.get("from_tag") == "daily") == _is_daily(key)


def unread_for(all_e, key, marker):
    """(entries, how). With a marker: everything addressed to `key` after it, by position. Without one, or
    if the marked heading is gone: the last INBOX_RECENT addressed to it (bias toward reporting)."""
    mine = lambda e: addressed_to_key(key, e) and not sent_by_key(key, e)
    if marker:
        for i in range(len(all_e) - 1, -1, -1):
            if all_e[i]["line_text"] == marker:
                return [e for e in all_e[i + 1:] if mine(e)], "since your read marker"
    return [e for e in all_e if mine(e)][-INBOX_RECENT:], "no read marker found: the last %d addressed to you" % INBOX_RECENT


def _ascii(s):
    return str(s).encode("ascii", "replace").decode()


def _load_cursors():
    try:
        with open(CURSORS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_cursors(d):
    with open(CURSORS_FILE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, sort_keys=True)


def _read_entries():
    try:
        with open(MAILBOX, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        print(f"{MAILBOX} not found"); return None
    all_e = entries(text)
    if not all_e:
        print("no entries parsed - mailbox format may have changed"); return None
    return all_e


def inbox_cli(argv):
    i = argv.index("--inbox")
    name = argv[i + 1] if len(argv) > i + 1 else ""
    key = resolve_key(name)
    if not key:
        print("unknown session %r; expected one of: %s" % (name, ", ".join(DISPLAY.values()))); return 2
    all_e = _read_entries()
    if all_e is None:
        return 2
    cursors = _load_cursors()
    unread, how = unread_for(all_e, key, cursors.get(key))
    print("INBOX for %s: %d unread (%s; by file position, never by timestamp)" % (display(key), len(unread), how))
    for e in unread:
        print("  line %-5d %s -> %s%s   [stamp %s ET]" % (
            e["line"], _ascii(e["line_text"].split("]", 1)[1].split("->")[0].strip()), e["to"],
            ("[%s]" % e["to_tag"]) if e["to_tag"] else "", _ascii(e["ts"])))
        print("             " + _ascii(e["subject"] or e["first"])[:110])
    if unread:
        print("Read each from its line (sed -n 'LINE,+40p' AGENT_MAIL.md), act, REPLY by appending, then confirm "
              "with the ALIGNED reply if an ALIGNMENT CHECK is open (python mail_check.py --aligned).")
    if "--ack" in argv:
        cursors[key] = all_e[-1]["line_text"]
        _save_cursors(cursors)
        print("[read marker moved to the newest entry (line %d) for %s]" % (all_e[-1]["line"], display(key)))
    return 1 if unread else 0


def aligned_status(all_e):
    """(request_entry or None, [(key, name, state, reply_entry)]). state: asked, aligned or outstanding."""
    req = None
    for i in range(len(all_e) - 1, -1, -1):
        if all_e[i]["subject"].startswith(ALIGN_REQUEST):
            req = i
            break
    if req is None:
        return None, []
    rows = []
    for key in DISPLAY:
        reply = next((e for e in all_e[req + 1:]
                      if e["subject"].startswith(ALIGN_REPLY) and sent_by_key(key, e)), None)
        state = "asked" if sent_by_key(key, all_e[req]) else ("aligned" if reply else "outstanding")
        rows.append((key, display(key), state, reply))
    return all_e[req], rows


def how_to_reach(key):
    if key in SELF_READING or _base(key) in SELF_READING:
        return "automatic: it reads mail itself " + SELF_READING.get(_base(key), "on its schedule")
    if _is_daily(key):
        return "automatic at its next scheduled run, or Devon says 'check mail' to " + display(key)
    return "Devon says 'check mail' to " + display(key)


def aligned_cli(argv):
    all_e = _read_entries()
    if all_e is None:
        return 2
    req, rows = aligned_status(all_e)
    if req is None:
        print("no %s entry in the mailbox" % ALIGN_REQUEST); return 0
    print("%s open since line %d: %s" % (ALIGN_REQUEST, req["line"], _ascii(req["subject"])[:100]))
    out = 0
    for key, name, state, reply in rows:
        extra = ("line %d" % reply["line"]) if reply else (how_to_reach(key) if state == "outstanding" else "asked it")
        print("  %-24s %-12s %s" % (name, state.upper(), _ascii(extra)))
        out += state == "outstanding"
    print("%d of %d still to confirm" % (out, len(rows)))
    return 1 if out else 0


def send(subject, body):
    """Same Gmail path the bot uses. Falls back exactly like alpaca_bot.send_email:
    a MISSING secret expands to an empty string, so never trust os.environ.get's
    default alone (that is what 535-failed every alert channel once)."""
    pw = os.environ.get("GMAIL_APP_PASSWORD") or ""
    if not pw:
        print("[email skipped - GMAIL_APP_PASSWORD not set]"); return False
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
        print(f"[email sent -> {to}: {subject}]"); return True
    except Exception as e:
        print(f"[email failed: {e}]"); return False


def main():
    if "--inbox" in sys.argv:
        return inbox_cli(sys.argv)
    if "--aligned" in sys.argv:
        return aligned_cli(sys.argv)
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
        with open(MAILBOX, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        print(f"{MAILBOX} not found"); return 2

    all_e = entries(text)
    if not all_e:
        print("no entries parsed - mailbox format may have changed"); return 2

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
            print(f"[{unparsed} entr(y/ies) had an unparseable timestamp - included rather than skipped]")
        buckets = {w: [e for e in new
                       if addressed_to(e, w) and not (w and e["from"] == w)] for w in whos}
        return _report(buckets, quiet, f"in the last {since:g}h")

    try:
        with open(STATE, encoding="utf-8") as fh:
            seen = json.load(fh).get("last_hdr", "")
    except Exception:
        seen = ""

    # First run adopts the backlog silently rather than emailing weeks of history.
    if not seen:
        _save_state(all_e[-1]["hdr"])
        print(f"first run - adopted backlog of {len(all_e)} entries, no email sent")
        return 0

    idx = next((i for i, e in enumerate(all_e) if e["hdr"] == seen), None)
    new = all_e[idx + 1:] if idx is not None else all_e
    if idx is None:
        print("last-seen entry not found (rewritten?) - reporting only the newest")
        new = all_e[-1:]

    # A session's own entries are not mail TO it.
    buckets = {w: [e for e in new
                   if addressed_to(e, w) and not (w and e["from"] == w)] for w in whos}
    _save_state(all_e[-1]["hdr"])

    return _report(buckets, quiet, f"({len(new)} new entr(y/ies))")


def _save_state(hdr):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump({"last_hdr": hdr}, fh, indent=1)


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
    def keys_for(w, es):               # ordered, unique DISPLAY keys this bucket's entries name
        ks = [name_key(w, e.get("to_tag")) if e["to"] == w else w for e in es]
        return list(dict.fromkeys(ks))
    all_keys = list(dict.fromkeys(k for w, es in hits.items() for k in keys_for(w, es)))
    lines = [action_line(all_keys), ""]
    for w, es in hits.items():
        label = " / ".join(display(k) for k in keys_for(w, es))
        lines.append(f"{len(es)} for {label}:")
        for e in es:
            lines += [f"  [{e['ts']} ET] {e['from']} -> {e['to']}" + ("[%s]" % e["to_tag"] if e.get("to_tag") else ""),
                      f"      {e['first'][:100]}"]
        lines.append("")
    wake = wake_keys(all_keys)
    if wake:
        lines.append(f"Open {names_text(wake)} and say: check mail.")
    lines.append("This watcher reports that mail arrived; it does not read or act on content.")
    body = NL.join(lines)
    print(body)
    if not quiet:
        subject = (f"AGENT_MAIL: have {names_text(wake)} check mail ({total} new)" if wake else
                   f"AGENT_MAIL: {total} new for {names_text(all_keys)} (it reads them itself, nothing to do)")
        send(subject, body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
