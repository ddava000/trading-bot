"""rh_daemon.py carries its OWN copy of the mailbox header regex (_MAIL_HEAD), and the copy has the
same defect mail_check.HDR had until 2026-10-01: the sender is captured with a bare (\\w+), which
cannot match cloud[daily], cloud[35819496] or laptop[daily].

Effect, measured on the live mailbox: 24 of the 47 entries addressed to the laptop are invisible to
the daemon's "new mail for the laptop session" notification, every one from a cloud session. That
notification is what makes "the laptop is the fastest reader of its mail" true, so Devon was never
told by it about any of them.

rh_daemon.py is the laptop's real-money executor, so the cloud session does not edit it. This test
reads the regex out of its SOURCE (never importing the daemon) and checks it against the live
mailbox. It is marked as an expected failure until the laptop applies the one-line fix, so CI stays
green now and flips to "unexpected success" the moment it is fixed, which is the cue to delete the
decorator. If the daemon switches to the shared mail_check.HDR, the test passes without it.

The fix:
    _MAIL_HEAD = re.compile(r"^## \\[([^\\]]+)\\]\\s*(\\w+)(?:\\[[^\\]]*\\])?\\s*->\\s*([A-Za-z]+)", re.M)

Run:  python -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mail_check  # noqa: E402


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def entry_headings(text):
    """Entry headings outside fenced blocks, excluding the format template."""
    out, in_fence = [], False
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.lstrip().startswith("`" * 3):
            in_fence = not in_fence
            continue
        if not in_fence and ln.startswith("## [") and "<from>" not in ln:
            out.append(ln)
    return out


def daemon_regex():
    """The daemon's header pattern, or None if it now uses the shared mail_check one."""
    src = read("rh_daemon.py")
    if "mail_check" in src and "HDR" in src:
        return None
    m = re.search(r'^_MAIL_HEAD\s*=\s*re\.compile\(r"(.*?)",\s*re\.M\)', src, re.M)
    if not m:
        raise AssertionError("cannot find _MAIL_HEAD in rh_daemon.py and it does not use mail_check.HDR: "
                             "update this test to match how the daemon reads mail headers")
    return re.compile(m.group(1), re.M)


class DaemonMailRegexTests(unittest.TestCase):
    def test_the_comparison_can_find_a_blind_spot(self):
        """Positive control: the OLD pattern misses a bracketed sender, the shared one does not."""
        old = re.compile(r"^## \[([^\]]+)\]\s*(\w+)\s*->\s*([A-Za-z]+)", re.M)
        heading = "## [2026-10-01 00:40 ET] cloud[35819496] -> laptop  [x]"
        self.assertIsNone(old.match(heading))
        self.assertIsNotNone(mail_check.HDR.match(heading))

    @unittest.expectedFailure     # remove this line when rh_daemon._MAIL_HEAD accepts [qualifier]
    def test_the_daemons_notifier_sees_every_live_entry_addressed_to_the_laptop(self):
        rx = daemon_regex()
        if rx is None:
            return
        missed = [h for h in entry_headings(read("AGENT_MAIL.md"))
                  if re.search(r"->\s*(laptop|both|all)\b", h, re.I) and not rx.match(h)]
        self.assertEqual(missed, [], "the daemon's mail notifier cannot see these entries: %s" % missed[:3])


if __name__ == "__main__":
    unittest.main()
