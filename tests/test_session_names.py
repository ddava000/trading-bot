"""Mail alerts must say WHICH session has to check mail, in the name Devon uses for it.

Devon 2026-10-01: "when i get mail i can never tell who needs to check mail." Every mail alert now
leads with "Have <name> check mail." and the names live in one place, mail_check.DISPLAY. These tests
pin the wording on each path that can reach him (the digest email, the Slack mirror, the laptop
daemon's own alert) and keep the laptop daemon's hardcoded copy from drifting away from the map.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mail_check as M      # noqa: E402
import slack_notify as S    # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=ET)


def entry(frm, to, when=NOW - timedelta(hours=1)):
    return "## [%s ET] %s -> %s  [subject]\nbody\n\n" % (when.strftime("%Y-%m-%d %H:%M"), frm, to)


class NameTests(unittest.TestCase):
    def test_the_names_devon_has_given(self):
        self.assertEqual(M.display("cloud"), "CLOUD")
        self.assertEqual(M.display("laptop"), "LAPTOP BOT")

    def test_a_session_not_yet_named_shows_its_bare_name_not_nothing(self):
        self.assertEqual(M.display("audit"), "audit")
        self.assertEqual(M.display(None), "the sessions")

    def test_the_lead_line_reads_naturally_for_one_two_and_three_sessions(self):
        self.assertEqual(M.action_line(["cloud"]), "Have CLOUD check mail.")
        self.assertEqual(M.action_line(["cloud", "laptop"]), "Have CLOUD and LAPTOP BOT check mail.")
        self.assertEqual(M.action_line(["cloud", "laptop", "audit"]),
                         "Have CLOUD, LAPTOP BOT and audit check mail.")

    def test_display_values_are_plain_text_a_subject_line_can_carry(self):
        """Devon prints mail to PDF by subject: no emoji, and nothing that is not ASCII."""
        for name in M.DISPLAY.values():
            self.assertTrue(name.isascii() and name.strip() == name and name, name)


class DigestEmailTests(unittest.TestCase):
    def run_digest(self, mailbox, *argv):
        sent = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            with open(os.path.join(d, "AGENT_MAIL.md"), "w", encoding="utf-8") as f:
                f.write("# AGENT_MAIL\n\n" + mailbox)

            class FixedDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return NOW.astimezone(tz) if tz else NOW

            cwd, old_argv = os.getcwd(), sys.argv
            os.chdir(d)
            sys.argv = ["mail_check.py", *argv]
            try:
                with mock.patch.object(M, "datetime", FixedDT), \
                        mock.patch.object(M, "send", lambda s, b: sent.append((s, b)) or True), \
                        contextlib.redirect_stdout(io.StringIO()):
                    M.main()
            finally:
                os.chdir(cwd)
                sys.argv = old_argv
        return sent

    def test_mail_for_cloud_says_have_cloud_check_mail_in_subject_and_body(self):
        sent = self.run_digest(entry("laptop", "cloud"), "--for", "cloud", "--since-hours", "24")
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertEqual(subject, "AGENT_MAIL: have CLOUD check mail (1 new)")
        self.assertEqual(body.splitlines()[0], "Have CLOUD check mail.")
        self.assertTrue(subject.isascii())            # he prints mail to PDF by subject

    def test_mail_for_both_sessions_names_both(self):
        sent = self.run_digest(entry("audit", "all"), "--for", "cloud,laptop", "--since-hours", "24")
        self.assertEqual(sent[0][0], "AGENT_MAIL: have CLOUD and LAPTOP BOT check mail (2 new)")
        self.assertEqual(sent[0][1].splitlines()[0], "Have CLOUD and LAPTOP BOT check mail.")

    def test_only_the_session_with_mail_is_named(self):
        sent = self.run_digest(entry("cloud[daily]", "laptop"), "--for", "cloud,laptop", "--since-hours", "24")
        self.assertEqual(sent[0][1].splitlines()[0], "Have LAPTOP BOT check mail.")
        self.assertNotIn("CLOUD", sent[0][0])

    def test_no_mail_sends_nothing(self):
        self.assertEqual(self.run_digest(entry("laptop", "laptop"), "--for", "laptop", "--since-hours", "24"), [])


class SlackMirrorTests(unittest.TestCase):
    def test_the_mirror_leads_with_who_must_check(self):
        self.assertEqual(S._who_checks("## [2026-10-01 10:00 ET] laptop -> cloud  [x]"), "*Have CLOUD check mail.*\n")
        self.assertEqual(S._who_checks("## [2026-10-01 10:00 ET] cloud[35819496] -> laptop  [x]"),
                         "*Have LAPTOP BOT check mail.*\n")

    def test_a_broadcast_names_every_session(self):
        lead = S._who_checks("## [2026-10-01 10:00 ET] laptop -> all  [x]")
        self.assertIn("CLOUD", lead)
        self.assertIn("LAPTOP BOT", lead)

    def test_an_unparseable_heading_gets_no_lead_line_and_does_not_raise(self):
        self.assertEqual(S._who_checks("## not an entry"), "")


class LaptopDaemonStaysInStepTests(unittest.TestCase):
    """rh_daemon.py cannot import mail_check, so it carries its own constant. Read from its SOURCE
    (never imported: it is the real-money executor) and compare."""

    def source(self):
        with open(os.path.join(ROOT, "rh_daemon.py"), encoding="utf-8") as f:
            return f.read()

    def test_the_daemons_session_name_equals_the_shared_map(self):
        m = re.search(r'^SESSION_NAME\s*=\s*"([^"]+)"', self.source(), re.M)
        self.assertIsNotNone(m, "rh_daemon.py no longer defines SESSION_NAME")
        self.assertEqual(m.group(1), M.DISPLAY["laptop"])

    def test_the_daemons_alert_leads_with_the_same_sentence(self):
        src = self.source()
        self.assertIn('f"AGENT_MAIL: have {SESSION_NAME} check mail', src)
        self.assertIn('f"Have {SESSION_NAME} check mail."', src)

    def test_the_drift_check_can_say_no(self):
        """Positive control: a renamed constant would be caught by the same comparison."""
        m = re.search(r'^SESSION_NAME\s*=\s*"([^"]+)"', 'SESSION_NAME = "SOMETHING ELSE"', re.M)
        self.assertNotEqual(m.group(1), M.DISPLAY["laptop"])


if __name__ == "__main__":
    unittest.main()
