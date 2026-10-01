"""The five named sessions, accounted for in one place (Devon 2026-10-01: "there should be 5 of you").

  token / recipient          name                      what it is
  cloud                      CLOUD                     the interactive cloud session
  cloud[daily]               BOT DAILY CHECK           the cloud's scheduled weekday mail check
  laptop                     LAPTOP BOT                the interactive session that owns the daemon
  laptop[daily]              LAPTOP BOT DAILY CHECK    the laptop's scheduled daily check
  audit                      BOT WEEKLY AUDIT          the Sunday GitHub Actions audit (self-reading)

The names are Devon's. A test that pins them is a tripwire, not a lock: changing one needs his say-so
(two sessions once flipped a name back and forth within minutes), and this file is where that shows.
Also pinned: every name routes correctly, the laptop daemon's two constants agree with the map, and
the audit, which is a workflow Devon cannot wake, is never asked to "check mail".

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

ROSTER = {
    "cloud": "CLOUD",
    "cloud[daily]": "BOT DAILY CHECK",
    "laptop": "LAPTOP BOT",
    "laptop[daily]": "LAPTOP BOT DAILY CHECK",
    "audit": "BOT WEEKLY AUDIT",
}


def entry(frm, to, when=NOW - timedelta(hours=1)):
    return "## [%s ET] %s -> %s  [subject]\nbody\n\n" % (when.strftime("%Y-%m-%d %H:%M"), frm, to)


def digest(mailbox, *argv):
    """Run the stateless digest on a throwaway mailbox with no network; returns [(subject, body)] sent."""
    sent = []

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        with open(os.path.join(d, "AGENT_MAIL.md"), "w", encoding="utf-8") as f:
            f.write("# AGENT_MAIL\n\n" + mailbox)
        cwd, old = os.getcwd(), sys.argv
        os.chdir(d)
        sys.argv = ["mail_check.py", *argv]
        try:
            with mock.patch.object(M, "datetime", FixedDT), \
                    mock.patch.object(M, "send", lambda s, b: sent.append((s, b)) or True), \
                    contextlib.redirect_stdout(io.StringIO()):
                M.main()
        finally:
            os.chdir(cwd)
            sys.argv = old
    return sent


class RosterTests(unittest.TestCase):
    def test_all_five_sessions_are_named_exactly_as_devon_named_them(self):
        self.assertEqual(M.DISPLAY, ROSTER)

    def test_there_are_five_distinct_names(self):
        self.assertEqual(len(set(M.DISPLAY.values())), 5)

    def test_every_session_token_the_mailbox_uses_has_a_name(self):
        for token in M.SESSIONS:
            self.assertIn(token, M.DISPLAY, token)

    def test_a_missing_name_would_be_caught(self):
        """Positive control: dropping one entry makes the roster comparison fail."""
        short = dict(M.DISPLAY)
        del short["cloud[daily]"]
        self.assertNotEqual(short, ROSTER)


class RoutingTests(unittest.TestCase):
    def test_a_recipient_qualifier_picks_the_qualified_name_when_one_exists(self):
        self.assertEqual(M.display(M.name_key("cloud", "daily")), "BOT DAILY CHECK")
        self.assertEqual(M.display(M.name_key("laptop", "daily")), "LAPTOP BOT DAILY CHECK")

    def test_a_bare_or_unknown_qualifier_falls_back_to_the_session_name(self):
        self.assertEqual(M.display(M.name_key("cloud", None)), "CLOUD")
        self.assertEqual(M.display(M.name_key("cloud", "35819496")), "CLOUD")      # the interactive session's id
        self.assertEqual(M.display(M.name_key("laptop", "")), "LAPTOP BOT")
        self.assertEqual(M.display(M.name_key("audit", None)), "BOT WEEKLY AUDIT")

    def test_mail_addressed_to_each_wakeable_session_names_that_session(self):
        for to, name in (("cloud", "CLOUD"), ("cloud[daily]", "BOT DAILY CHECK"),
                         ("laptop", "LAPTOP BOT"), ("laptop[daily]", "LAPTOP BOT DAILY CHECK")):
            with self.subTest(to=to):
                base = to.split("[")[0]
                sent = digest(entry("audit", to), "--for", base, "--since-hours", "24")
                self.assertEqual(sent[0][0], "AGENT_MAIL: have %s check mail (1 new)" % name)
                self.assertEqual(sent[0][1].splitlines()[0], "Have %s check mail." % name)


class DaemonStaysInStepTests(unittest.TestCase):
    """rh_daemon.py carries copies of the laptop's two names. Read from SOURCE, never imported."""

    def constant(self, name):
        with open(os.path.join(ROOT, "rh_daemon.py"), encoding="utf-8") as f:
            m = re.search(r'^%s\s*=\s*"([^"]+)"' % name, f.read(), re.M)
        self.assertIsNotNone(m, "rh_daemon.py no longer defines %s" % name)
        return m.group(1)

    def test_both_laptop_names_in_the_daemon_equal_the_shared_map(self):
        self.assertEqual(self.constant("SESSION_NAME"), M.DISPLAY["laptop"])
        self.assertEqual(self.constant("DAILY_NAME"), M.DISPLAY["laptop[daily]"])


class AuditIsSelfReadingTests(unittest.TestCase):
    """A GitHub workflow cannot be told "check mail": alerts say it reads its own, on Sundays."""

    def test_the_lead_line_for_mail_to_the_audit_alone(self):
        self.assertEqual(M.action_line(["audit"]),
                         "BOT WEEKLY AUDIT reads mail by itself every Sunday. Nothing for you to do.")

    def test_the_audit_is_left_out_when_others_must_be_woken(self):
        self.assertEqual(M.action_line(["cloud", "audit"]), "Have CLOUD check mail.")
        self.assertEqual(M.action_line(["cloud", "laptop", "audit"]), "Have CLOUD and LAPTOP BOT check mail.")
        self.assertEqual(M.wake_keys(["cloud", "audit", "laptop[daily]"]), ["cloud", "laptop[daily]"])

    def test_a_digest_for_the_audit_alone_never_says_have_it_check_mail(self):
        sent = digest(entry("laptop", "audit"), "--for", "audit", "--since-hours", "24")
        subject, body = sent[0]
        self.assertEqual(subject, "AGENT_MAIL: 1 new for BOT WEEKLY AUDIT (it reads them itself, nothing to do)")
        self.assertEqual(body.splitlines()[0],
                         "BOT WEEKLY AUDIT reads mail by itself every Sunday. Nothing for you to do.")
        self.assertNotIn("Open", body)
        self.assertNotIn("check mail ", subject)

    def test_a_digest_for_the_audit_and_cloud_asks_only_for_cloud(self):
        sent = digest(entry("laptop", "all"), "--for", "cloud,audit", "--since-hours", "24")
        self.assertEqual(sent[0][0], "AGENT_MAIL: have CLOUD check mail (2 new)")
        self.assertIn("Open CLOUD and say: check mail.", sent[0][1])
        self.assertNotIn("Open CLOUD and BOT WEEKLY AUDIT", sent[0][1])

    def test_the_slack_mirror_agrees(self):
        self.assertEqual(S._who_checks("## [2026-10-01 10:00 ET] laptop -> audit  [x]"),
                         "*BOT WEEKLY AUDIT reads mail by itself every Sunday. Nothing for you to do.*\n")
        lead = S._who_checks("## [2026-10-01 10:00 ET] laptop -> all  [x]")
        self.assertIn("CLOUD", lead)
        self.assertNotIn("AUDIT", lead)


class WorkflowAndPromptNamesTests(unittest.TestCase):
    def test_the_audit_workflow_carries_its_name_where_devon_sees_it(self):
        import yaml
        with open(os.path.join(ROOT, ".github", "workflows", "weekly-audit.yml"), encoding="utf-8") as f:
            self.assertEqual(yaml.safe_load(f)["name"], "BOT WEEKLY AUDIT")

    def test_the_audit_is_told_its_name_and_that_it_reads_its_own_mail(self):
        with open(os.path.join(ROOT, ".github", "audit-prompt.md"), encoding="utf-8") as f:
            head = f.read(900)
        self.assertIn("YOUR NAME IS BOT WEEKLY AUDIT", head)
        self.assertIn("never asked to wake you", head)


if __name__ == "__main__":
    unittest.main()
