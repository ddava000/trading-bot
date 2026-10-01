#!/usr/bin/env python3
"""Message Devon, on Slack, whenever a push to AGENT_MAIL.md needs a session woken.

Devon 2026-10-01: "they should all message me when i need to have a session check the mail". Nobody
has to remember to do it: .github/workflows/mailbox-notify.yml runs this on every push that touches the
mailbox, reads the entries that push ADDED, and for each one posts "Have <name> check mail." naming only
the sessions Devon has to wake (never the writer itself, and never the Sunday audit, which reads its own).
When an entry is an ALIGNMENT CHECK or an ALIGNED reply it also posts who is still to confirm.

No state file: the diff between the push's before and after commits IS the list of new entries, so a
runner with an empty disk behaves the same as any other. Slack not configured, or a post that fails, is a
RED run (exit 1): a notifier that silently does nothing is indistinguishable from a quiet mailbox.

Usage:  python mailbox_notify.py BEFORE_SHA AFTER_SHA
"""
import subprocess
import sys

import mail_check as M
import slack_notify

MAX_POSTS = 5
ZERO = "0" * 40
MAILBOX = "AGENT_MAIL.md"


def added_headings(diff_text):
    """Entry headings a diff ADDED (a heading line that moved shows up in both sides; only '+' counts)."""
    return [ln[1:].strip() for ln in diff_text.split("\n") if ln.startswith("+## [")]


def parse_heading(head):
    """The entry dict for one heading line, or None if the watcher cannot parse it."""
    es = M.entries(head + "\nx\n")
    return es[0] if es else None


def wake_keys_for(e):
    """The DISPLAY keys Devon must wake for this entry: addressed to, wakeable, and not the writer."""
    if e["to"] in M.BROADCAST:
        targets = ["cloud", "laptop"]                    # the interactive sessions; dailies and audit read themselves
    else:
        targets = [M.name_key(e["to"], e["to_tag"])]
    return [k for k in M.wake_keys(targets) if not M.sent_by_key(k, e)]


def alignment_lines(mailbox_text):
    """'Still to confirm: ...' for the newest ALIGNMENT CHECK, or [] if none is open or all have confirmed."""
    req, rows = M.aligned_status(M.entries(mailbox_text))
    if req is None:
        return []
    todo = [(name, key) for key, name, state, _ in rows if state == "outstanding"]
    if not todo:
        return ["All five sessions have confirmed ALIGNED."]
    return ["Still to confirm (%d of %d):" % (len(todo), len(rows))] + [
        "- %s: %s" % (name, M.how_to_reach(key)) for name, key in todo]


def message_for(e, mailbox_text):
    is_reply = e["subject"].startswith(M.ALIGN_REPLY)
    # A confirmation wakes nobody: five replies must not mean five pings. It just shows who is still to confirm.
    wake = [] if is_reply else wake_keys_for(e)
    subject = M._ascii(e["subject"] or e["first"])[:160]
    is_align = e["subject"].startswith((M.ALIGN_REQUEST, M.ALIGN_REPLY))
    if not wake and not is_align:
        return None
    sender = M.display(e["from"] + ("[daily]" if e["from_tag"] == "daily" else ""))
    lines = []
    if wake:
        lines.append("*" + M.action_line(wake) + "*")
    lines.append("%s wrote (line %d): %s" % (sender, e["line"], subject))
    if is_align:
        lines += alignment_lines(mailbox_text)
    return "\n".join(lines)


def messages_for_diff(diff_text, mailbox_text):
    out = []
    for head in added_headings(diff_text):
        e = parse_heading(head)
        if e is None:
            continue
        # line numbers come from the whole file, not the one-line parse above
        for full in M.entries(mailbox_text):
            if full["line_text"] == e["line_text"]:
                e = full
        msg = message_for(e, mailbox_text)
        if msg:
            out.append(msg)
    return out


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8").stdout


def diff_between(before, after):
    if not before or before == ZERO or not _git("cat-file", "-t", before).strip():
        before = after + "~1"
    return _git("diff", "-U0", before, after, "--", MAILBOX)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__); return 2
    with open(MAILBOX, encoding="utf-8") as fh:
        text = fh.read()
    msgs = messages_for_diff(diff_between(argv[0], argv[1]), text)
    if not msgs:
        print("no new entry needs anyone woken")
        return 0
    if not slack_notify.enabled():
        print("SLACK NOT CONFIGURED: %d message(s) for Devon were not sent" % len(msgs))
        return 1
    extra = len(msgs) - MAX_POSTS
    ok = True
    for m in msgs[:MAX_POSTS]:
        ok = slack_notify.post(m) and ok
    if extra > 0:
        ok = slack_notify.post("(and %d more new mailbox entries; run: python mail_check.py --inbox \"<NAME>\")" % extra) and ok
    print("posted %d message(s) to Slack" % min(len(msgs), MAX_POSTS) if ok else "A SLACK POST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
