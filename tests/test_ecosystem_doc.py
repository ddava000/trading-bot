"""ECOSYSTEM.md is the one picture every session shares, so it must not rot.

Devon 2026-10-01: "make sure everyone is aware of the full ecosystem". The map names every session and every
GitHub workflow; these tests fail when a workflow is added without being added to it, when a session name
changes in mail_check.DISPLAY but not here, and when the entry points that tell sessions to read it go stale.

Run:  python -m unittest discover -s tests -v
"""
import glob
import os
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mail_check as M  # noqa: E402


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


class EcosystemDocTests(unittest.TestCase):
    def setUp(self):
        self.doc = read("ECOSYSTEM.md")

    def test_every_session_name_is_in_the_map(self):
        for name in M.DISPLAY.values():
            self.assertIn(name, self.doc, name)

    def test_every_session_token_is_in_the_map(self):
        for key in M.DISPLAY:
            self.assertIn("`%s`" % key, self.doc, key)

    def test_every_github_workflow_is_in_the_map(self):
        for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
            with open(path, encoding="utf-8") as f:
                name = yaml.safe_load(f)["name"]
            self.assertIn(name, self.doc, "workflow %r (%s) is not in ECOSYSTEM.md" % (name, os.path.basename(path)))

    def test_the_check_mail_order_names_the_commands_that_exist(self):
        self.assertIn('python mail_check.py --inbox "<YOUR NAME>" --ack', self.doc)
        self.assertIn("python mail_check.py --aligned", self.doc)
        self.assertIn("ALIGNED: <YOUR NAME>", self.doc)
        self.assertIn("ALIGNMENT CHECK", self.doc)
        self.assertEqual((M.ALIGN_REQUEST, M.ALIGN_REPLY), ("ALIGNMENT CHECK", "ALIGNED"))

    def test_it_says_there_is_one_channel_and_what_the_others_are_for(self):
        self.assertIn("AGENT_MAIL.md is the channel", self.doc)
        self.assertIn("Slack and email only NOTIFY", " ".join(self.doc.split()))

    def test_no_em_dashes_and_plain_ascii(self):
        self.assertTrue(self.doc.isascii(), [c for c in self.doc if ord(c) > 127][:5])

    def test_the_check_can_say_no(self):
        """Positive control: a name that is not in the map is reported missing by the same test."""
        self.assertNotIn("SOME SESSION NOBODY NAMED", self.doc)


class EntryPointsTests(unittest.TestCase):
    def test_claude_md_sends_every_cold_session_to_the_map(self):
        self.assertIn("ECOSYSTEM.md", read("CLAUDE.md"))

    def test_the_audit_is_told_to_read_it_and_how_to_check_mail(self):
        head = read(".github/audit-prompt.md")[:2500]
        self.assertIn("ECOSYSTEM.md", head)
        self.assertIn('--inbox "BOT WEEKLY AUDIT"', head)
        self.assertIn("ALIGNED", head)

    def test_the_mail_commands_are_wired_into_the_cli(self):
        src = read("mail_check.py")
        for flag in ('"--inbox"', '"--aligned"', '"--ack"'):
            self.assertIn(flag, src)


if __name__ == "__main__":
    unittest.main()
