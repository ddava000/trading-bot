"""The daemon must not tell Devon to wake a session that no longer exists.

Devon RETIRED both daily-check sessions on 2026-10-17: one froze and skipped two days
unnoticed, the other was killed by the shared Claude limit, and a GitHub DAILY DIGEST took
over their report. Commit ced73e41 taught the other two paths that day - mail_check.RETIRED,
which `--aligned` reads, and slack_notify's MAILBOX NOTIFY, which posts nothing at all for
such an entry - and missed rh_daemon's own notifier, which went on emailing "have LAPTOP BOT
DAILY CHECK check mail" about a session that wakes nobody. Found 2026-11-04 while reading this
file to answer cloud's request about check_arm_a.

This path deliberately does NOT copy the Slack one and go silent: an entry headed
`-> laptop[daily]` still has to be READ by somebody, and the live session is the only reader
left. So it names LAPTOP BOT, reports the entry, and says the heading was addressed to a
retired session. Silence here would lose mail; naming the dead session wastes Devon's time.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mail_check as M       # noqa: E402
import rh_daemon as D        # noqa: E402

BASE = "## [2026-11-04 09:00 ET] cloud -> laptop  [the entry already seen]\nbody\n\n"


def entry(to, when="2026-11-04 15:30"):
    return "## [%s ET] cloud[35819496] -> %s  [a new one]\nbody\n\n" % (when, to)


class TheNotifierNamesAWakeableSession(unittest.TestCase):
    def notify_for(self, text):
        """Drive the real check_mail: adopt the baseline first, then append and look again."""
        sent, logged = [], []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "AGENT_MAIL.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(BASE)
            with mock.patch.object(D, "MAIL_F", path), \
                 mock.patch.object(D, "notify", lambda s, b, untrusted=False: (sent.append((s, b)), True)[1]), \
                 mock.patch.object(D, "log", logged.append), \
                 mock.patch.object(D, "reached_nobody", lambda: False):
                led = {}
                D.check_mail(led)                  # first run adopts the backlog, says nothing
                self.assertEqual(sent, [], "the baseline entry must not be announced")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(text)
                D.check_mail(led)
        return sent

    def test_mail_headed_to_the_retired_daily_check_names_the_live_session(self):
        sent = self.notify_for(entry("laptop[daily]"))
        self.assertEqual(len(sent), 1, "the entry must still be reported, not dropped")
        subject, body = sent[0]
        self.assertIn("have LAPTOP BOT check mail", subject)
        self.assertNotIn(D.DAILY_NAME, subject,
                         "this is the 2026-10-17 retirement: never ask him to wake it")
        self.assertNotIn(D.DAILY_NAME, body.splitlines()[0])
        self.assertIn(D.DAILY_NAME, body, "name the retired session as context, just never as the one to wake")
        self.assertIn("RETIRED", body, "say why the heading is not being honoured")
        self.assertIn("laptop[daily]", body, "and quote the heading, so he can see what arrived")
        self.assertIn("Open the LAPTOP BOT session", body)

    def test_the_control_a_plain_laptop_heading_is_unchanged_and_carries_no_note(self):
        sent = self.notify_for(entry("laptop"))
        subject, body = sent[0]
        self.assertIn("have LAPTOP BOT check mail", subject)
        self.assertNotIn("RETIRED", body)

    def test_a_broadcast_is_unchanged_too(self):
        for to in ("all", "both"):
            with self.subTest(to=to):
                subject, body = self.notify_for(entry(to))[0]
                self.assertIn("have LAPTOP BOT check mail", subject)
                self.assertNotIn("RETIRED", body)


class TheCopyIsKeptInStep(unittest.TestCase):
    """rh_daemon does not import mail_check (it stays ungated so slack_notify can import it
    lazily), so the retired qualifiers are a copy. Same idiom as SESSION_NAME: a test, not trust."""

    def test_retired_tags_match_mail_checks_authority(self):
        theirs = {k.split("[", 1)[1].rstrip("]") for k in M.RETIRED if k.startswith("laptop[")}
        self.assertEqual(set(D.RETIRED_TAGS), theirs,
                         "mail_check.RETIRED is the authority: %s" % sorted(M.RETIRED))

    def test_the_retired_names_are_not_in_the_wakeable_roster_either(self):
        self.assertEqual(M.action_line(["laptop", "laptop[daily]"]), "Have LAPTOP BOT check mail.")


if __name__ == "__main__":
    unittest.main()
