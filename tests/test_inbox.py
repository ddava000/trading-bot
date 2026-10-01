"""One way for every session to check mail: unread by FILE POSITION after a per-session marker.

The failure this exists for (2026-10-01): LAPTOP BOT judged "new" by comparing timestamps with its own last
entry, which it had stamped 25 minutes ahead of the real clock. Two entries addressed to it, one carrying an
open decision, looked older than its own, and it reported "no new mail". Tests below reproduce that exactly,
show the timestamp method misses it, and pin the rest of the rules: identity (interactive vs daily
sessions share a heading token but not an inbox), broadcasts, own-entry exclusion, markers that survive the
session writing more entries, and the ALIGNMENT CHECK tracker.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mail_check as M  # noqa: E402


def e(stamp, frm, to, subject="s"):
    return "## [%s ET] %s -> %s  [%s]\nbody line\n\n" % (stamp, frm, to, subject)


# The real shape of 2026-10-01: LAPTOP BOT's entry is stamped 16:35 but sits BEFORE two cloud entries
# stamped 16:16 and 16:24 (they were written later, at about 16:16 and 16:24 real time).
SCENARIO = (e("2026-10-01 16:20", "laptop", "all", "INTRODUCTION: LAPTOP BOT DAILY CHECK")
            + e("2026-10-01 16:35", "laptop", "all", "INTRODUCTION: LAPTOP BOT (stamped ahead)")
            + e("2026-10-01 16:16", "cloud[35819496]", "laptop", "open decision for you")
            + e("2026-10-01 16:24", "cloud[35819496]", "all", "roster of five"))


class Box(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)

    def write(self, body):
        with open("AGENT_MAIL.md", "w", encoding="utf-8") as f:
            f.write("# AGENT_MAIL\n\n" + body)

    def append(self, body):
        with open("AGENT_MAIL.md", "a", encoding="utf-8") as f:
            f.write(body)

    def cli(self, *args):
        out, old = io.StringIO(), sys.argv
        sys.argv = ["mail_check.py", *args]
        try:
            with contextlib.redirect_stdout(out):
                rc = M.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def entries(self):
        with open("AGENT_MAIL.md", encoding="utf-8") as f:
            return M.entries(f.read())


class TheFailureThisFixes(Box):
    def test_a_future_stamped_last_entry_no_longer_hides_mail_written_after_it(self):
        self.write(SCENARIO)
        es = self.entries()
        marker = es[1]["line_text"]                       # LAPTOP BOT's own 16:35 entry
        unread, _ = M.unread_for(es, "laptop", marker)
        self.assertEqual([x["subject"] for x in unread], ["open decision for you", "roster of five"])

    def test_the_timestamp_method_misses_exactly_that_mail(self):
        """Positive control: comparing stamps with the last own entry finds NOTHING, the bug as reported."""
        es = self.entries_after_write()
        by_stamp = [x for x in es if x["ts"] > "2026-10-01 16:35" and x["from"] != "laptop"]
        self.assertEqual(by_stamp, [])

    def entries_after_write(self):
        self.write(SCENARIO)
        return self.entries()

    def test_writing_a_new_entry_does_not_mark_older_unread_mail_as_read(self):
        """'Unread = after my last entry' would reset on every post; a read marker does not."""
        self.write(SCENARIO)
        marker = self.entries()[1]["line_text"]
        self.append(e("2026-10-01 16:28", "laptop", "all", "a correction written without reading"))
        unread, _ = M.unread_for(self.entries(), "laptop", marker)
        self.assertEqual([x["subject"] for x in unread], ["open decision for you", "roster of five"])


class WhoseMailIsIt(Box):
    def setUp(self):
        super().setUp()
        self.write(e("2026-10-01 10:00", "cloud[35819496]", "laptop", "to the interactive laptop")
                   + e("2026-10-01 10:01", "cloud[35819496]", "laptop[daily]", "to the laptop daily check")
                   + e("2026-10-01 10:02", "laptop", "cloud", "from LAPTOP BOT to cloud")
                   + e("2026-10-01 10:03", "laptop[daily]", "all", "daily check broadcast")
                   + e("2026-10-01 10:04", "cloud[daily]", "cloud", "daily to cloud"))
        self.es = self.entries()

    def subjects(self, key):
        return [x["subject"] for x in M.unread_for(self.es, key, None)[0]]

    def test_the_interactive_laptop_skips_mail_addressed_to_its_daily_sibling(self):
        self.assertEqual(self.subjects("laptop"), ["to the interactive laptop", "daily check broadcast"])

    def test_the_laptop_daily_check_reads_every_entry_to_its_base_token(self):
        self.assertEqual(self.subjects("laptop[daily]"),
                         ["to the interactive laptop", "to the laptop daily check"])   # its own broadcast is excluded

    def test_cloud_sees_its_siblings_entries_but_not_its_own(self):
        self.assertEqual(self.subjects("cloud"), ["from LAPTOP BOT to cloud", "daily check broadcast", "daily to cloud"])
        self.assertEqual(self.subjects("cloud[daily]"), ["from LAPTOP BOT to cloud", "daily check broadcast"])

    def test_a_session_never_sees_its_own_entries(self):
        self.assertNotIn("from LAPTOP BOT to cloud", self.subjects("laptop"))
        self.assertNotIn("daily check broadcast", self.subjects("laptop[daily]"))

    def test_the_audit_reads_broadcasts_and_its_own_token(self):
        self.append(e("2026-10-01 10:05", "laptop", "audit", "for the audit"))
        self.assertEqual([x["subject"] for x in M.unread_for(self.entries(), "audit", None)[0]],
                         ["daily check broadcast", "for the audit"])


class MarkersAndTheCommand(Box):
    def test_ack_moves_the_marker_and_the_next_check_is_empty(self):
        self.write(SCENARIO)
        rc, out = self.cli("--inbox", "LAPTOP BOT", "--ack")
        self.assertEqual(rc, 1)
        self.assertIn("open decision for you", out)
        rc, out = self.cli("--inbox", "LAPTOP BOT")
        self.assertEqual(rc, 0)
        self.assertIn("0 unread", out)

    def test_new_mail_after_the_marker_is_reported_once(self):
        self.write(SCENARIO)
        self.cli("--inbox", "LAPTOP BOT", "--ack")
        self.append(e("2026-10-01 17:00", "cloud[35819496]", "laptop", "fresh one"))
        rc, out = self.cli("--inbox", "LAPTOP BOT", "--ack")
        self.assertEqual(rc, 1)
        self.assertIn("fresh one", out)
        self.assertNotIn("open decision", out)
        self.assertEqual(self.cli("--inbox", "LAPTOP BOT")[0], 0)

    def test_check_without_ack_does_not_consume_the_mail(self):
        self.write(SCENARIO)
        self.assertEqual(self.cli("--inbox", "LAPTOP BOT")[0], 1)
        self.assertEqual(self.cli("--inbox", "LAPTOP BOT")[0], 1)

    def test_sessions_sharing_a_heading_token_have_separate_markers(self):
        self.write(e("2026-10-01 10:00", "cloud[35819496]", "laptop", "for both laptop sessions"))
        self.cli("--inbox", "LAPTOP BOT", "--ack")
        self.assertEqual(self.cli("--inbox", "LAPTOP BOT")[0], 0)
        self.assertEqual(self.cli("--inbox", "LAPTOP BOT DAILY CHECK")[0], 1)       # not consumed by its sibling
        with open(M.CURSORS_FILE, encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)), ["laptop"])

    def test_a_lost_marker_falls_back_to_the_last_ten_addressed_to_me(self):
        self.write("".join(e("2026-10-01 10:%02d" % i, "cloud[35819496]", "laptop", "n%d" % i) for i in range(15)))
        with open(M.CURSORS_FILE, "w", encoding="utf-8") as f:
            json.dump({"laptop": "## [gone] heading that no longer exists"}, f)
        rc, out = self.cli("--inbox", "laptop")
        self.assertEqual(rc, 1)
        self.assertIn("10 unread", out)
        self.assertIn("n14", out)
        self.assertNotIn("n4 ", out + " ")

    def test_names_resolve_by_key_or_display_name_in_any_case(self):
        for name, key in (("laptop", "laptop"), ("LAPTOP BOT", "laptop"), ("laptop bot daily check", "laptop[daily]"),
                          ("Cloud[daily]", "cloud[daily]"), ("bot weekly audit", "audit"), ("CLOUD", "cloud")):
            self.assertEqual(M.resolve_key(name), key, name)
        self.assertIsNone(M.resolve_key("nobody"))

    def test_an_unknown_name_is_an_error_not_an_empty_inbox(self):
        self.write(SCENARIO)
        rc, out = self.cli("--inbox", "nobody")
        self.assertEqual(rc, 2)
        self.assertIn("LAPTOP BOT", out)                      # it tells you the valid names

    def test_output_is_ascii_safe_for_a_cp1252_console(self):
        self.write(e("2026-10-01 10:00", "cloud[35819496]", "laptop", "café → arrow"))
        rc, out = self.cli("--inbox", "laptop")
        self.assertTrue(out.isascii(), out)


class LineNumbersAreRealLineNumbers(Box):
    def test_the_line_an_entry_reports_is_the_line_of_its_heading_in_the_file(self):
        self.write(SCENARIO)
        with open("AGENT_MAIL.md", encoding="utf-8") as f:
            lines = f.read().split("\n")
        for x in self.entries():
            self.assertEqual(lines[x["line"] - 1].strip(), x["line_text"])

    def test_on_the_real_mailbox_too(self):
        with open(os.path.join(ROOT, "AGENT_MAIL.md"), encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")
        es = M.entries(text)
        self.assertGreater(len(es), 40)
        for x in es:
            self.assertEqual(lines[x["line"] - 1].rstrip("\r").strip(), x["line_text"])


class AlignmentTracker(Box):
    def setUp(self):
        super().setUp()
        self.write(e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK 2026-10-01: reply ALIGNED"))

    def states(self):
        _, rows = M.aligned_status(self.entries())
        return {name: state for _, name, state, _ in rows}

    def test_the_asker_is_marked_and_everyone_else_is_outstanding(self):
        self.assertEqual(self.states(), {"CLOUD": "asked", "BOT DAILY CHECK": "outstanding",
                                         "LAPTOP BOT": "outstanding", "LAPTOP BOT DAILY CHECK": "outstanding",
                                         "BOT WEEKLY AUDIT": "outstanding"})

    def test_a_reply_aligns_only_the_session_that_wrote_it(self):
        self.append(e("2026-10-01 17:05", "laptop", "all", "ALIGNED: LAPTOP BOT read ECOSYSTEM.md"))
        s = self.states()
        self.assertEqual(s["LAPTOP BOT"], "aligned")
        self.assertEqual(s["LAPTOP BOT DAILY CHECK"], "outstanding")          # same token, different session

    def test_a_reply_from_before_the_request_does_not_count(self):
        self.write(e("2026-10-01 16:00", "laptop", "all", "ALIGNED: stale")
                   + e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK 2026-10-01"))
        self.assertEqual(self.states()["LAPTOP BOT"], "outstanding")

    def test_the_request_subject_is_not_mistaken_for_a_reply(self):
        self.assertFalse("ALIGNMENT CHECK".startswith(M.ALIGN_REPLY))

    def test_the_newest_request_wins(self):
        self.append(e("2026-10-01 17:05", "laptop", "all", "ALIGNED: LAPTOP BOT"))
        self.append(e("2026-10-02 09:00", "laptop[daily]", "all", "ALIGNMENT CHECK 2026-10-02: again"))
        s = self.states()
        self.assertEqual(s["LAPTOP BOT DAILY CHECK"], "asked")
        self.assertEqual(s["LAPTOP BOT"], "outstanding")

    def test_cli_exit_code_and_how_to_reach_each_outstanding_session(self):
        rc, out = self.cli("--aligned")
        self.assertEqual(rc, 1)
        self.assertIn("4 of 5 still to confirm", out)
        self.assertIn("Devon says 'check mail' to LAPTOP BOT", out)
        self.assertIn("automatic at its next scheduled run", out)             # the daily checks
        self.assertIn("automatic: it reads mail itself every Sunday", out)      # the audit

    def test_everyone_aligned_is_exit_zero(self):
        for frm, who in (("cloud[daily]", "BOT DAILY CHECK"), ("laptop", "LAPTOP BOT"),
                         ("laptop[daily]", "LAPTOP BOT DAILY CHECK"), ("audit", "BOT WEEKLY AUDIT")):
            self.append(e("2026-10-01 17:10", frm, "all", "ALIGNED: " + who))
        rc, out = self.cli("--aligned")
        self.assertEqual(rc, 0)
        self.assertIn("0 of 5 still to confirm", out)

    def test_no_request_in_the_mailbox_is_reported_plainly(self):
        self.write(SCENARIO)
        rc, out = self.cli("--aligned")
        self.assertEqual(rc, 0)
        self.assertIn("no ALIGNMENT CHECK", out)


if __name__ == "__main__":
    unittest.main()
