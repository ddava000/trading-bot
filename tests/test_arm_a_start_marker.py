"""The run-start marker as PROOF in arm_a_watch (Devon 2026-11-05, chosen from clickable options).

The workflow's "Mark run start" step force-pushes one parentless commit to the branch `arm-a-start` the moment a job starts (tests/test_arm_a_workflow_steps.py
runs that step for real). arm_a_watch reads it through GIT and treats a queued run whose id is at or before the marker's as STARTED: exact proof, from a
source that shares no failure mode with the Actions API that served stale state on 2026-10-22 and 2026-11-04. That closes the one class git could not
reach before: a stale read while the pass is still RUNNING (wait 6 to about 10.5 minutes), where status.json has no commit yet. The five 2026-10-22
instants are exactly that class; the marker rows below are SYNTHETIC (the marker did not exist then), built on the recorded rows.

It corrects the FIRST level only. Missing or inconsistent marker data is no proof, and the alert is what it was before the marker existed.

Run:  python -m unittest discover -s tests -v      (needs git)
"""
import contextlib
import copy
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import arm_a_replay_data as R  # noqa: E402
import arm_a_watch as A  # noqa: E402
from test_arm_a_watch_replay import Harness, ROWS, T, QUEUED_TEXT, waiting_run  # noqa: E402

UTC = timezone.utc
MSG = "arm-a run-start run=%d attempt=1 started=%s"


def marker(run_id, started="2026-10-22T14:15:09Z"):
    return {"run_id": run_id, "started": T(started) if started else None}


class ParseMarker(unittest.TestCase):
    def test_a_real_marker_message(self):
        self.assertEqual(A.parse_start_marker(MSG % (41935293667, "2026-11-04T23:38:26Z") + "\n"),
                         {"run_id": 41935293667, "started": T("2026-11-04T23:38:26Z")})

    def test_a_marker_without_a_time_still_names_the_run(self):
        self.assertEqual(A.parse_start_marker("arm-a run-start run=77 attempt=2"), {"run_id": 77, "started": None})

    def test_anything_else_is_not_a_marker(self):
        for msg in (None, "", "status heartbeat 2026-11-05T14:55:39Z", "rerun=5", "run=abc"):
            self.assertIsNone(A.parse_start_marker(msg), msg)


@unittest.skipUnless(shutil.which("git"), "needs git")
class ReadMarkerFromGit(unittest.TestCase):
    """The real git_start_marker against a local bare repo that stands in for GitHub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin, self.work = os.path.join(self.tmp, "o.git"), os.path.join(self.tmp, "w")
        self.git("init", "-q", "--bare", self.origin, cwd=self.tmp)
        self.git("clone", "-q", self.origin, self.work, cwd=self.tmp)
        open(os.path.join(self.work, "f"), "w").write("x")
        self.git("add", "f", cwd=self.work)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "i", cwd=self.work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)                  # so a second clone has a HEAD to work from
        self.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)                      # a bare repo's HEAD may still name master
        old = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, old)

    def git(self, *a, cwd):
        r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, (a, r.stderr))
        return r.stdout.strip()

    def push_marker(self, run_id, cwd=None):
        cwd = cwd or self.work
        c = self.git("-c", "user.name=a", "-c", "user.email=a@a", "commit-tree", self.git("rev-parse", "HEAD^{tree}", cwd=cwd),
                     "-m", MSG % (run_id, "2026-11-05T14:00:00Z"), cwd=cwd)
        self.git("push", "-q", "--force", "origin", "%s:refs/heads/arm-a-start" % c, cwd=cwd)

    def test_it_reads_the_newest_marker_through_git(self):
        self.push_marker(100)
        self.assertEqual(A.git_start_marker()["run_id"], 100)
        self.push_marker(101)                                              # force-push: the branch holds only the newest
        self.assertEqual(A.git_start_marker()["run_id"], 101)

    def test_no_branch_yet_is_none_not_an_error(self):
        self.assertIsNone(A.git_start_marker())

    def test_an_unreachable_remote_is_none(self):
        self.push_marker(100)
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"), cwd=self.work)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(A.git_start_marker())

    def test_without_a_fetch_it_reads_what_was_already_fetched(self):
        self.push_marker(100)
        self.assertEqual(A.git_start_marker(fetch=True)["run_id"], 100)
        other = os.path.join(self.tmp, "w2")                                  # a SECOND clone pushes: git updates the pusher's own tracking ref,
        self.git("clone", "-q", self.origin, other, cwd=self.tmp)             # so only a push from elsewhere leaves this clone's ref behind
        self.push_marker(101, cwd=other)
        self.assertEqual(A.git_start_marker(fetch=False)["run_id"], 100)      # the local tracking ref, not refreshed
        self.assertEqual(A.git_start_marker(fetch=True)["run_id"], 101)       # and a fetch picks the new one up


class MarkerProves(unittest.TestCase):
    NOW = T("2026-11-05T15:10:00Z")
    RUN = {"id": 500, "status": "queued", "created_at": T("2026-11-05T15:00:08Z")}

    def test_a_marker_for_this_run_or_a_later_one_proves_it_started(self):
        for rid in (500, 501, 9999):
            self.assertTrue(A.marker_proves(marker(rid, "2026-11-05T15:00:10Z"), self.RUN, self.NOW), rid)

    def test_a_marker_for_an_EARLIER_run_proves_nothing_about_this_one(self):
        self.assertFalse(A.marker_proves(marker(499, "2026-11-05T14:45:09Z"), self.RUN, self.NOW))

    def test_missing_or_inconsistent_data_is_no_proof(self):
        self.assertFalse(A.marker_proves(None, self.RUN, self.NOW))
        self.assertFalse(A.marker_proves({}, self.RUN, self.NOW))
        self.assertFalse(A.marker_proves(marker(500), dict(self.RUN, id=None), self.NOW))
        self.assertFalse(A.marker_proves({"run_id": "x", "started": None}, self.RUN, self.NOW))
        self.assertFalse(A.marker_proves(marker(500, "2026-11-05T16:00:00Z"), self.RUN, self.NOW))      # a start stamp in the future

    def test_a_marker_with_no_time_still_proves_by_id(self):
        self.assertTrue(A.marker_proves(marker(500, None), self.RUN, self.NOW))


class CorroborateWithMarker(unittest.TestCase):
    NOW = T("2026-11-05T15:09:00Z")
    OPEN = T("2026-11-05T14:45:00Z")
    COMMIT = T("2026-11-05T14:55:30Z")
    STATUS = {"ts": "2026-11-05T09:45 ET"}                                  # the PREVIOUS pass (14:45Z): proves nothing about the 15:00 run

    def rows(self, minutes=9.0, rid=500):
        return [{"id": rid, "status": "queued", "created_at": self.NOW - timedelta(minutes=minutes)}]

    def test_the_marker_corrects_a_mid_pass_wait_that_status_json_cannot(self):
        out, corrected = A.corroborate(self.rows(), self.STATUS, self.COMMIT, self.NOW, marker(500))
        self.assertEqual([(r["id"], r["proof"], r["status"]) for r in corrected], [(500, "start marker", "in_progress")])
        self.assertEqual(out[0]["row_status"], "queued")

    def test_control_without_a_marker_the_same_run_stays_counted(self):
        out, corrected = A.corroborate(self.rows(), self.STATUS, self.COMMIT, self.NOW, None)
        self.assertEqual(corrected, [])
        self.assertEqual(out[0]["status"], "queued")

    def test_an_earlier_runs_marker_does_not_correct_it(self):
        self.assertEqual(A.corroborate(self.rows(), self.STATUS, self.COMMIT, self.NOW, marker(499))[1], [])

    def test_status_json_proof_is_labelled_first_when_both_exist(self):
        rows = [{"id": 500, "status": "queued", "created_at": T("2026-11-05T14:45:30Z")}]      # its own pass (ts 14:45) is already committed
        _, corrected = A.corroborate(rows, {"ts": "2026-11-05T09:45 ET"}, T("2026-11-05T14:56:00Z"), self.NOW, marker(500))
        self.assertEqual(corrected[0]["proof"], "status.json")

    def test_it_is_first_level_only_an_urgent_wait_is_still_judged_on_the_raw_runs(self):
        sig, shown, corrected = A.measure_with_git(self.NOW, self.OPEN, self.COMMIT, self.STATUS, self.rows(minutes=21.0), marker(500))
        self.assertAlmostEqual(sig["queued"], 21.0, places=1)
        self.assertEqual(shown[0]["status"], "queued")
        sig, _, _ = A.measure_with_git(self.NOW, self.OPEN, self.COMMIT, self.STATUS, self.rows(minutes=9.0), marker(500))
        self.assertEqual(sig["queued"], 0.0)


class MarkerReaderRule(unittest.TestCase):
    def test_the_real_reader_is_used_only_when_the_run_list_and_commit_time_are_real(self):
        stub = lambda *a, **k: None
        self.assertIs(A.marker_reader(None, A.fetch_runs, A.git_commit_time), A.git_start_marker)
        self.assertIsNone(A.marker_reader(None, stub, A.git_commit_time))
        self.assertIsNone(A.marker_reader(None, A.fetch_runs, stub))
        mine = lambda: None
        self.assertIs(A.marker_reader(mine, stub, stub), mine)


class RunOnceWithMarker(Harness):
    def test_a_stubbed_run_list_never_touches_git_for_a_marker(self):
        """So no test meets today's real marker (a real run id is larger than any a test uses)."""
        with mock.patch.object(A, "git_start_marker", side_effect=AssertionError("a test reached the real marker")):
            res = self.check(ROWS["10-26 a"])
            self.assertEqual(len(res["sent"]), 1)

    def test_the_five_10_22_instants_are_suppressed_by_the_marker_even_with_the_jobs_endpoint_unreadable(self):
        """The class git alone could not reach (a stale read while the pass was still running). SYNTHETIC markers on the recorded rows."""
        rows = [r for r in R.ROWS if r["id"].startswith("10-22")]
        self.assertEqual(len(rows), 5)
        for row in rows:
            run = waiting_run(row)
            with self.subTest(row=row["id"]):
                res = self.check(row, jobs="unreadable", marker=marker(run["id"], run["created_at"][:19] + "Z"))
                self.assertEqual(res["bodies"], [], res["line"])
                self.assertIn("start marker for run %d" % run["id"], res["line"])
                self.assertIn("not counted", res["line"])                       # LAPTOP's log gate matches this phrase

    def test_control_the_same_instants_alert_when_the_marker_is_for_an_earlier_run(self):
        for row in (r for r in R.ROWS if r["id"].startswith("10-22")):
            run = waiting_run(row)
            with self.subTest(row=row["id"]):
                res = self.check(row, jobs="unreadable", marker=marker(run["id"] - 1))
                self.assertEqual(len(self.queued_alerts(res)), 1, res["line"])

    def test_control_and_with_no_marker_at_all(self):
        for row in (r for r in R.ROWS if r["id"].startswith("10-22")):
            with self.subTest(row=row["id"]):
                self.assertEqual(len(self.queued_alerts(self.check(row, jobs="unreadable", marker=None))), 1)

    def test_the_true_10_26_stuck_runs_still_alert_because_a_run_that_never_started_leaves_no_marker(self):
        """The marker of the PREVIOUS run (the newest one that exists while this run waits) is an earlier id, so it proves nothing about the stuck run."""
        for rid in ("10-26 a", "10-26 b", "10-26 c", "10-26 c-after"):
            row = ROWS[rid]
            run = waiting_run(row)
            previous = max(r["id"] for r in row["runs"] if r["id"] < run["id"])
            with self.subTest(row=rid):
                res = self.check(row, marker=marker(previous))
                self.assertEqual(len(self.queued_alerts(res)), 1, res["line"])
                self.assertNotIn("start marker for run", res["line"])

    def test_11_04_is_suppressed_by_the_marker_alone_when_status_json_cannot_help(self):
        row = ROWS["11-04 11:11:14"]
        res = self.check(row, jobs={"41863263764": "stale_queued"}, commit=T("2026-11-04T15:55:27Z"), status_ts="2026-11-04T10:45 ET",
                         marker=marker(41863263764, "2026-11-04T16:00:14Z"))
        self.assertEqual(res["bodies"], [], res["line"])
        self.assertIn("run-start marker shows they had already started", res["line"])
        self.assertNotIn("git shows their pass had already committed", res["line"])      # the status.json sentence is for status.json proofs only

    def test_an_urgent_wait_is_reported_even_with_a_marker_and_says_so(self):
        row = ROWS["11-04 11:11:14"]
        now = T(row["now"])
        runs = [{"id": 41863263764, "status": "queued", "created_at": (now - timedelta(minutes=21)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        res = self.check(row, runs=runs, jobs="unreadable", commit=T("2026-11-04T15:55:27Z"), status_ts="2026-11-04T10:45 ET",
                         marker=marker(41863263764, "2026-11-04T15:50:10Z"))
        urgent = [s for s in res["sent"] if s[2]]
        self.assertEqual(len(urgent), 1, res["line"])
        self.assertIn("The run-start marker shows run(s) 41863263764 already started", urgent[0][1])
        self.assertIn("Reported anyway because the wait reached the urgent level", urgent[0][1])
        self.assertNotIn("nothing could corroborate", urgent[0][1])                      # the marker DID corroborate: that sentence would be false

    def test_a_marker_that_cannot_be_read_is_no_proof_and_never_an_error(self):
        row = ROWS["10-26 a"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc, line, sent = A.run_once(
                now=T(row["now"]), fired={}, deliver=lambda s, b, u: True, get_commit_time=lambda ref: T(row["commit"]),
                get_status=lambda ref: {"ts": row["status_ts"]},
                get_runs=lambda: [dict(r, created_at=T(r["created_at"]), conclusion=None) for r in row["runs"]],
                get_job_state=lambda rid: None, get_marker=mock.Mock(side_effect=OSError("git down")))
        self.assertEqual(len(sent), 1)                                                   # alerted exactly as before
        self.assertIn("start marker read failed", out.getvalue())

    def test_the_evidence_says_what_the_marker_showed(self):
        row = ROWS["10-26 a"]
        run = waiting_run(row)
        got = self.check(row, marker=marker(run["id"] - 1, "2026-10-26T14:00:12Z"))["bodies"][0]
        self.assertIn("Run-start marker (git branch arm-a-start): run %d started 14:00:12 UTC." % (run["id"] - 1), got)
        none = self.check(row, marker=None)["bodies"][0]
        self.assertIn("Run-start marker (git branch arm-a-start): none readable.", none)
        unchecked = self.check(row)["bodies"][0]                                          # marker not read at all (a stubbed run list)
        self.assertNotIn("Run-start marker", unchecked)
        for text in (got, none, unchecked):
            text.encode("ascii")


if __name__ == "__main__":
    unittest.main()
