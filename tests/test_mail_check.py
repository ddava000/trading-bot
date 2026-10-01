"""mail_check.py: the watcher that tells Devon and the sessions that unread mail arrived.

Found by the 2026-10-01 audit: its header regex captured the sender with a bare (\\w+), which
cannot match cloud[daily], cloud[35819496] or laptop[daily]. 25 of the 46 entries in the live
mailbox, every one from a cloud session or a daily check, were INVISIBLE to it, so a laptop digest
could never report mail from the cloud. A watcher that cannot see the mail is the failure it exists
to prevent, and it said nothing, which is why this was never noticed.

The last class below reads the REAL mailbox, so a future header shape the watcher cannot parse turns
CI red on the push that wrote it, instead of going quiet.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
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

import mail_check as M  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=ET)
# The pattern as it stood before the fix, to show the tests can fail.
OLD_HDR = re.compile(r"^## \[([^\]]+)\]\s*(\w+)\s*->\s*(\w+)", re.M)


def entry(when, frm, to, first="subject line"):
    return "## [%s ET] %s -> %s  [%s]\nbody of the entry\n\n" % (when.strftime("%Y-%m-%d %H:%M"), frm, to, first)


def fixed(now):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    return FixedDT


class HeaderParsingTests(unittest.TestCase):
    def parse(self, header):
        m = M.HDR.search(header + "\nbody\n")
        return None if m is None else (m.group(2), m.group(3))

    def test_plain_senders(self):
        self.assertEqual(self.parse("## [2026-09-30 22:20 ET] laptop -> cloud  [x]"), ("laptop", "cloud"))
        self.assertEqual(self.parse("## [2026-09-30 22:20 ET] slack -> all  [x]"), ("slack", "all"))

    def test_bracketed_senders_are_seen(self):
        """The defect: these were all invisible."""
        self.assertEqual(self.parse("## [2026-10-01 00:40 ET] cloud[35819496] -> laptop  [x]"), ("cloud", "laptop"))
        self.assertEqual(self.parse("## [2026-09-30 16:10 ET] cloud[daily] -> laptop  [x]"), ("cloud", "laptop"))
        self.assertEqual(self.parse("## [2026-09-27 09:00 ET] laptop[daily] -> cloud  [x]"), ("laptop", "cloud"))

    def test_a_bracketed_recipient_is_seen(self):
        self.assertEqual(self.parse("## [2026-09-30 22:20 ET] laptop -> cloud[daily]  [x]"), ("laptop", "cloud"))

    def test_the_old_pattern_could_not_see_them(self):
        """Positive control: the same headers fail under the previous regex."""
        for h in ("## [2026-10-01 00:40 ET] cloud[35819496] -> laptop", "## [2026-09-30 16:10 ET] cloud[daily] -> laptop"):
            self.assertIsNone(OLD_HDR.search(h))
            self.assertIsNotNone(M.HDR.search(h))

    def test_things_that_are_not_headings_still_do_not_match(self):
        for h in ("## [YYYY-MM-DD HH:MM ET] <from> -> <to>", "## Entry format", "# [2026-09-30 10:00 ET] a -> b",
                  "text ## [2026-09-30 10:00 ET] a -> b", "## [2026-09-30 10:00 ET] cloud/daily -> laptop"):
            self.assertIsNone(M.HDR.search(h), h)

    def test_entries_use_the_bare_session_names_and_stop_at_the_next_heading(self):
        text = ("## [2026-10-01 00:40 ET] Cloud[35819496] -> Laptop  [first]\nline one\n\n"
                "## [2026-10-01 01:30 ET] laptop -> cloud  [second]\nanother\n")
        es = M.entries(text)
        self.assertEqual([(e["from"], e["to"]) for e in es], [("cloud", "laptop"), ("laptop", "cloud")])
        self.assertEqual(es[0]["first"], "[first]")                  # the bracketed subject is what the digest shows
        self.assertEqual(es[1]["first"], "[second]")
        self.assertIn("cloud[35819496]", es[0]["hdr"].lower())      # the full heading is the state key

    def test_timestamps_tolerate_the_typos_the_parser_was_loosened_for(self):
        for ts in ("2026-10-01 00:40", "2026-10-01 0:40", "2026-10-01 00:40:15", "2026-10-01 00:40 ET",
                   "2026-10-01 00:40 EDT", "2026-10-01 00:40 UTC"):
            self.assertIsNotNone(M.parse_ts(ts, ET), ts)
        self.assertIsNone(M.parse_ts("sometime tuesday", ET))

    def test_addressing(self):
        e = {"to": "laptop", "from": "cloud"}
        self.assertTrue(M.addressed_to(e, "laptop"))
        self.assertFalse(M.addressed_to(e, "cloud"))
        self.assertTrue(M.addressed_to({"to": "all"}, "cloud"))
        self.assertTrue(M.addressed_to({"to": "both"}, "laptop"))
        self.assertTrue(M.addressed_to(e, None))


class Runner(unittest.TestCase):
    """Runs main() in a throwaway directory with a fixed clock and no email."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def write_mail(self, text):
        with open(os.path.join(self.tmp.name, "AGENT_MAIL.md"), "w", encoding="utf-8") as f:
            f.write("# AGENT_MAIL\n\n" + text)

    def main(self, *argv, now=NOW, hdr=None):
        out, cwd, old_argv = io.StringIO(), os.getcwd(), sys.argv
        os.chdir(self.tmp.name)
        sys.argv = ["mail_check.py", *argv]
        try:
            with contextlib.ExitStack() as st:
                st.enter_context(mock.patch.object(M, "datetime", fixed(now)))
                if hdr is not None:
                    st.enter_context(mock.patch.object(M, "HDR", hdr))
                st.enter_context(contextlib.redirect_stdout(out))
                rc = M.main()
        finally:
            os.chdir(cwd)
            sys.argv = old_argv
        return rc, out.getvalue()


class StatelessDigestTests(Runner):
    def test_a_cloud_daily_entry_to_the_laptop_is_reported(self):
        self.write_mail(entry(NOW - timedelta(hours=3), "cloud[daily]", "laptop", "daily check clean"))
        rc, out = self.main("--for", "laptop", "--since-hours", "24", "--quiet")
        self.assertEqual(rc, 1)
        self.assertIn("1 for %s" % M.display("laptop"), out)
        self.assertIn("cloud -> laptop", out)

    def test_the_same_mailbox_was_silent_under_the_old_pattern(self):
        """Positive control for the above: this is the bug, reproduced."""
        self.write_mail(entry(NOW - timedelta(hours=3), "cloud[daily]", "laptop")
                        + entry(NOW - timedelta(hours=2), "laptop", "cloud"))
        rc, out = self.main("--for", "laptop", "--since-hours", "24", "--quiet", hdr=OLD_HDR)
        self.assertEqual(rc, 0)
        self.assertIn("no new mail", out)

    def test_a_sessions_own_entries_are_not_mail_to_it(self):
        self.write_mail(entry(NOW - timedelta(hours=1), "laptop[daily]", "laptop"))
        self.assertEqual(self.main("--for", "laptop", "--since-hours", "24", "--quiet")[0], 0)

    def test_entries_older_than_the_window_are_not_reported(self):
        self.write_mail(entry(NOW - timedelta(hours=30), "cloud[35819496]", "laptop"))
        self.assertEqual(self.main("--for", "laptop", "--since-hours", "24", "--quiet")[0], 0)
        self.assertEqual(self.main("--for", "laptop", "--since-hours", "36", "--quiet")[0], 1)

    def test_broadcasts_reach_every_session_asked_about_in_one_digest(self):
        self.write_mail(entry(NOW - timedelta(hours=1), "slack", "all", "relayed")
                        + entry(NOW - timedelta(hours=2), "cloud[daily]", "laptop"))
        rc, out = self.main("--for", "cloud,laptop", "--since-hours", "24", "--quiet")
        self.assertEqual(rc, 1)
        self.assertIn("1 for %s:" % M.display("cloud"), out)
        self.assertIn("2 for %s:" % M.display("laptop"), out)

    def test_no_entries_parsed_is_an_error_not_a_clean_bill(self):
        self.write_mail("nothing that looks like an entry\n")
        rc, out = self.main("--for", "laptop", "--since-hours", "24", "--quiet")
        self.assertEqual(rc, 2)
        self.assertIn("no entries parsed", out)

    def test_unknown_session_and_missing_mailbox_are_errors(self):
        self.write_mail(entry(NOW, "cloud", "laptop"))
        self.assertEqual(self.main("--for", "nobody", "--quiet")[0], 2)
        os.remove(os.path.join(self.tmp.name, "AGENT_MAIL.md"))
        self.assertEqual(self.main("--for", "laptop", "--since-hours", "24", "--quiet")[0], 2)


class StatefulTests(Runner):
    def test_first_run_adopts_the_backlog_then_only_new_mail_is_reported(self):
        self.write_mail(entry(NOW - timedelta(days=3), "cloud[35819496]", "laptop", "old"))
        rc, out = self.main("--for", "laptop", "--quiet")
        self.assertEqual(rc, 0)
        self.assertIn("first run", out)
        with open(os.path.join(self.tmp.name, ".mail_check_state.json"), encoding="utf-8") as f:
            self.assertIn("cloud[35819496]", json.load(f)["last_hdr"])
        self.assertEqual(self.main("--for", "laptop", "--quiet")[0], 0)           # nothing new
        with open(os.path.join(self.tmp.name, "AGENT_MAIL.md"), "a", encoding="utf-8") as f:
            f.write(entry(NOW, "cloud[daily]", "laptop", "fresh"))
        rc, out = self.main("--for", "laptop", "--quiet")
        self.assertEqual(rc, 1)
        self.assertIn("fresh", out)
        self.assertEqual(self.main("--for", "laptop", "--quiet")[0], 0)           # reported once only


def unparseable_headings(text):
    """Entry headings (outside fenced blocks) that mail_check.HDR cannot read."""
    bad, in_fence = [], False
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln.lstrip().startswith("`" * 3):
            in_fence = not in_fence
            continue
        if in_fence or "<from>" in ln or not ln.startswith("## ["):
            continue
        if not M.HDR.match(ln):
            bad.append(ln[:90])
    return bad


class LiveMailboxTests(unittest.TestCase):
    def test_the_checker_can_find_an_unreadable_heading(self):
        text = "## [2026-10-01 00:40 ET] cloud/daily -> laptop  [x]\nbody\n## [2026-10-01 00:41 ET] a -> b  [ok]\n"
        self.assertEqual(len(unparseable_headings(text)), 1)
        fenced = "```\n## [2026-10-01 00:40 ET] cloud/daily -> laptop  [x]\n```\n"
        self.assertEqual(unparseable_headings(fenced), [])          # quoted examples are not entries

    def test_every_entry_heading_in_the_real_mailbox_is_one_the_watcher_can_see(self):
        with open(os.path.join(ROOT, "AGENT_MAIL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertGreater(len(M.entries(text)), 10)
        self.assertEqual(unparseable_headings(text), [],
                         "mail_check cannot parse these headings, so it would never report them. Use "
                         "'## [YYYY-MM-DD HH:MM ET] <from> -> <to>' where from/to are a session name, "
                         "optionally followed by [qualifier].")


if __name__ == "__main__":
    unittest.main()
