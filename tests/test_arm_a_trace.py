"""The ARM A WATCH summary line must reach the LAPTOP's own log when a row was not counted.

Cloud asked for this on 2026-11-04 (their mailbox entry, section E) after shipping the git
corroboration of the queued signal. check_arm_a read `if rc: log(line)`, and run_once returns
rc 0 on every normal pass, so a pass where a run row was DISCOUNTED - the Actions API calling a
run queued while status.json proves its pass had already committed - left no trace on this
machine. The GitHub-hosted copy of the watch prints that line; the laptop did not. The laptop's
log is the copy that matters: it is where the 2026-10-26 evidence was recovered from when the
three muted episodes had to be replayed, and a suppression nobody can see later is how both
arms ended up arguing from memory this morning.

The gate keys on cloud's own wording, so it fails SILENT if they reword it - the opposite of
the mute in _deliver, which fails noisy. That is why the first test below builds the real
summary line through THEIR run_once instead of trusting the string: their rewording breaks
this test rather than quietly ending the only record the laptop keeps.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arm_a_watch as A      # noqa: E402
import rh_daemon as D        # noqa: E402

# The 2026-11-04 11:11 ET episode, from the real git history of status.json: run 41863263764
# was created 16:00:08Z, its job started 16:00:14Z and finished 16:10:42Z, the pass committed
# 16:10:34Z carrying ts 11:00 ET, and the Actions API was still calling the run queued when the
# watch looked at 16:11:14Z. Verified on the laptop 2026-11-04: ts equals as_of_utc as an
# instant on all 116 status.json commits of 10-05, 10-22, 10-23, 10-26 and 11-04, and equals
# the minute the matching job started on all four of today's passes I timed from the API.
NOW = datetime(2026, 11, 4, 16, 11, 14, tzinfo=timezone.utc)
COMMIT = datetime(2026, 11, 4, 16, 10, 34, tzinfo=timezone.utc)
CREATED = datetime(2026, 11, 4, 16, 0, 8, tzinfo=timezone.utc)
ITS_OWN_PASS = {"ts": "2026-11-04T11:00 ET"}      # proves the run ran
A_PREDECESSOR = {"ts": "2026-11-04T10:45 ET"}     # proves nothing about it


class TheMarkerIsCloudsOwnWording(unittest.TestCase):
    """Built from the real run_once. Nothing here is a string I typed on the laptop's behalf."""

    def real_line(self, status):
        rc, line, _ = A.run_once(now=NOW, fired={}, deliver=lambda *a, **k: True,
                                 get_commit_time=lambda ref: COMMIT,
                                 get_status=lambda ref: status,
                                 get_runs=lambda: [{"id": 41863263764, "status": "queued",
                                                    "created_at": CREATED}],
                                 get_job_state=lambda *a, **k: None)
        return rc, line

    def test_a_discounted_row_puts_the_marker_in_clouds_summary_line(self):
        rc, line = self.real_line(ITS_OWN_PASS)
        self.assertEqual(rc, 0, "a corrected row is not an error: %s" % line)
        self.assertIn(D.ARM_A_NOT_COUNTED, line,
                      "the laptop's log gate keys on this phrase; cloud's line no longer has it: %s" % line)
        self.assertIn("git shows their pass had already committed", line)
        self.assertIn("41863263764", line, "the summary must name the row it did not count")

    def test_the_control_a_pass_that_proves_nothing_leaves_no_marker(self):
        """Same instant, same run, only status.json's pass start moved back: the row IS counted."""
        rc, line = self.real_line(A_PREDECESSOR)
        self.assertEqual(rc, 0, line)
        self.assertNotIn(D.ARM_A_NOT_COUNTED, line, line)
        self.assertIn("ALERTED", line, "an 11-minute wait with no git proof is a real alert: %s" % line)


class TheLaptopLogsIt(unittest.TestCase):
    LINE = ("ARM A WATCH: status commit age 1m, last run created 11m ago, longest wait for a machine 0m"
            " (1 run row(s) said queued although git shows their pass had already committed; not counted"
            " at the first level: run 41863263764 created 16:00:08Z, newest status.json committed"
            " 16:10:34Z, its pass started 16:00Z)")
    QUIET = "ARM A WATCH: status commit age 1m, last run created 11m ago, longest wait for a machine 0m"

    def run_check(self, rc, line):
        logged = []
        with mock.patch.object(D, "log", logged.append), \
             mock.patch.object(D, "notify", lambda *a, **k: True), \
             mock.patch.object(D, "_remember_alerts", lambda l, **k: None), \
             mock.patch.object(A, "run_once", lambda **kw: (rc, line, [])):
            D._ARM_A_NEXT = 0
            D.check_arm_a({})
        return logged

    def test_a_pass_that_discounted_a_row_is_written_to_the_log(self):
        self.assertEqual(self.run_check(0, self.LINE), [self.LINE])

    def test_a_quiet_pass_is_still_not_logged_every_five_minutes(self):
        self.assertEqual(self.run_check(0, self.QUIET), [])

    def test_a_failure_is_logged_as_it_always_was(self):
        self.assertEqual(self.run_check(1, self.QUIET), [self.QUIET])

    def test_the_lagged_jobs_clause_carries_the_same_marker(self):
        """Cloud's other discounting clause. One gate has to cover both, or the older one goes dark."""
        lagged = self.QUIET + " (1 run row(s) said queued while their job was already running; not counted)"
        self.assertIn(D.ARM_A_NOT_COUNTED, lagged)
        self.assertEqual(self.run_check(0, lagged), [lagged])


if __name__ == "__main__":
    unittest.main()
