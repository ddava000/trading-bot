"""arm_a_downtime.py: Arm A's side of the experiment's downtime comparison, pinned.

2026-10-08: experiment.json had a downtime figure for Arm B and none for Arm A, so Arm A read as zero by omission. The tool measures
how much of the 09:45-15:55 ET window had a successful Arm A run in progress, how much of the rest is the built-in 15-minute
cadence, and how much is an unplanned gap. These tests pin each clause on synthetic runs; the 10-05 case (two runs that ended
"failure" after 15 minutes while Arm A had no stop checks) is a test of its own.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arm_a_downtime as A  # noqa: E402
import arm_b_downtime as B  # noqa: E402

HOL = B.holidays()


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M" if len(s) == 16 else "%Y-%m-%d %H:%M:%S")


def ivs(*pairs):
    return [(dt(a), dt(b)) for a, b in pairs]


def run(*pairs, start="2026-10-07 00:00", end="2026-10-08 23:59", **kw):
    return A.measure(ivs(*pairs), dt(start), dt(end), HOL, **kw)


class IntervalTests(unittest.TestCase):
    def test_only_completed_successful_runs_count(self):
        runs = [
            {"status": "completed", "conclusion": "success", "run_started_at": "2026-10-07T13:45:06Z", "updated_at": "2026-10-07T13:55:48Z"},
            {"status": "completed", "conclusion": "failure", "run_started_at": "2026-10-05T19:30:09Z", "updated_at": "2026-10-05T19:45:12Z"},
            {"status": "completed", "conclusion": "cancelled", "run_started_at": "2026-10-05T19:50:00Z", "updated_at": "2026-10-05T21:00:00Z"},
            {"status": "in_progress", "conclusion": None, "run_started_at": "2026-10-08T14:00:09Z", "updated_at": "2026-10-08T14:00:16Z"},
            {"status": "completed", "conclusion": "success", "run_started_at": None, "updated_at": "2026-10-08T14:00:16Z"},
        ]
        got = A.intervals(runs)
        self.assertEqual(len(got), 1)

    def test_utc_is_converted_to_eastern_with_daylight_time(self):
        """13:45Z in October is 09:45 EDT; in December it would be 08:45 EST."""
        r = [{"status": "completed", "conclusion": "success", "run_started_at": "2026-10-07T13:45:06Z", "updated_at": "2026-10-07T13:55:48Z"}]
        self.assertEqual(A.intervals(r)[0][0].strftime("%H:%M"), "09:45")
        r = [{"status": "completed", "conclusion": "success", "run_started_at": "2026-12-07T13:45:06Z", "updated_at": "2026-12-07T13:55:48Z"}]
        self.assertEqual(A.intervals(r)[0][0].strftime("%H:%M"), "08:45")

    def test_overlapping_runs_are_not_counted_twice(self):
        self.assertEqual(A.merge(ivs(("2026-10-07 10:00", "2026-10-07 10:12"), ("2026-10-07 10:10", "2026-10-07 10:20"))),
                         ivs(("2026-10-07 10:00", "2026-10-07 10:20")))


class MeasureTests(unittest.TestCase):
    def test_the_normal_cadence_is_covered_plus_by_design_and_nothing_unplanned(self):
        r = run(("2026-10-07 10:00", "2026-10-07 10:10"), ("2026-10-07 10:15", "2026-10-07 10:25"), ("2026-10-07 10:30", "2026-10-07 10:40"))
        self.assertEqual((r["covered"], r["by_design"], r["unplanned"], r["trading"]), (30, 10, 0, 40))

    def test_a_lost_run_is_an_unplanned_episode(self):
        r = run(("2026-10-07 10:00", "2026-10-07 10:10"), ("2026-10-07 10:30", "2026-10-07 10:40"))
        self.assertEqual((r["unplanned"], r["by_design"]), (20, 0))
        self.assertEqual(len(r["episodes"]), 1)

    def test_the_gap_threshold_is_strictly_greater_than_10(self):
        ten = run(("2026-10-07 10:00", "2026-10-07 10:10"), ("2026-10-07 10:20", "2026-10-07 10:30"))
        self.assertEqual((ten["unplanned"], ten["by_design"]), (0, 10))
        eleven = run(("2026-10-07 10:00", "2026-10-07 10:10"), ("2026-10-07 10:21", "2026-10-07 10:30"))
        self.assertEqual((eleven["unplanned"], eleven["by_design"]), (11, 0))

    def test_failed_runs_that_lasted_15_minutes_are_a_hole_not_coverage(self):
        """2026-10-05: the 15:30 and 15:45 ET runs both ended 'failure' after 15 minutes. Arm A had no stop checks."""
        good = {"status": "completed", "conclusion": "success"}
        runs = [dict(good, run_started_at="2026-10-05T19:15:07Z", updated_at="2026-10-05T19:26:58Z"),
                dict(good, conclusion="failure", run_started_at="2026-10-05T19:30:09Z", updated_at="2026-10-05T19:45:12Z"),
                dict(good, conclusion="failure", run_started_at="2026-10-05T19:45:07Z", updated_at="2026-10-05T20:00:22Z"),
                dict(good, run_started_at="2026-10-06T13:45:06Z", updated_at="2026-10-06T13:55:48Z")]
        r = A.measure(A.intervals(runs), dt("2026-10-05 00:00"), dt("2026-10-06 23:59"), HOL)
        self.assertGreaterEqual(r["unplanned"], 28)                 # 15:26 to the 15:55 close, minus the seconds after

    def test_a_sliver_under_a_minute_is_cadence_not_an_episode(self):
        """A run that starts 7 seconds after 09:45 must not become a lost-run episode."""
        r = run(("2026-10-07 09:30:00", "2026-10-07 09:30:15"), ("2026-10-07 09:45:07", "2026-10-07 09:55:00"))
        self.assertEqual((r["unplanned"], r["episodes"]), (0, []))

    def test_a_weekend_costs_only_the_window_edges(self):
        r = run(("2026-10-02 15:20", "2026-10-02 15:30"), ("2026-10-05 09:46", "2026-10-05 09:56"), start="2026-10-02 00:00", end="2026-10-05 23:59")
        self.assertEqual(r["unplanned"], 25 + 1)

    def test_the_span_is_clipped_to_the_data_so_unobserved_time_is_not_charged(self):
        r = run(("2026-10-07 10:00", "2026-10-07 10:10"), start="2026-10-01 00:00", end="2026-10-31 23:59")
        self.assertEqual((r["trading"], r["unplanned"]), (10, 0))

    def test_the_three_numbers_add_up_to_the_trading_minutes(self):
        r = run(("2026-10-07 10:00", "2026-10-07 10:10"), ("2026-10-07 10:15", "2026-10-07 10:25"), ("2026-10-07 10:50", "2026-10-07 11:00"),
                ("2026-10-07 11:04", "2026-10-07 11:14"))
        self.assertAlmostEqual(r["covered"] + r["by_design"] + r["unplanned"], r["trading"])


class JobSpanTests(unittest.TestCase):
    """2026-10-26: seven runs waited 4 to 13 minutes before their JOB existed; the run span counted that wait as watched."""

    @staticmethod
    def job(created, started, completed):
        return {"created_at": created, "started_at": started, "completed_at": completed}

    def run_row(self, rid=1, created="2026-10-26T14:15:06Z", updated="2026-10-26T14:38:08Z", conclusion="success"):
        return {"id": rid, "status": "completed", "conclusion": conclusion, "created_at": created, "run_started_at": created, "updated_at": updated}

    def test_the_span_is_the_jobs_start_and_end_and_the_wait_is_run_created_to_job_created(self):
        run = self.run_row()
        sp = A.job_span(run, fetch=lambda r: [self.job("2026-10-26T14:27:18Z", "2026-10-26T14:27:20Z", "2026-10-26T14:38:08Z")])
        self.assertEqual(sp["start"], dt("2026-10-26 10:27:20"))
        self.assertEqual(sp["end"], dt("2026-10-26 10:38:08"))
        self.assertAlmostEqual(sp["wait_min"], 12.2, places=1)

    def test_the_wait_is_for_the_job_to_EXIST_not_for_a_machine_once_it_does(self):
        """Job created 12 minutes after the run but only started 2 minutes after that: the wait the signal cares about is 12, not 14."""
        sp = A.job_span(self.run_row(), fetch=lambda r: [self.job("2026-10-26T14:27:06Z", "2026-10-26T14:29:06Z", "2026-10-26T14:40:00Z")])
        self.assertAlmostEqual(sp["wait_min"], 12.0, places=1)

    def test_several_jobs_take_the_first_start_and_the_last_end(self):
        sp = A.job_span(self.run_row(), fetch=lambda r: [self.job("2026-10-26T14:15:08Z", "2026-10-26T14:15:10Z", "2026-10-26T14:20:00Z"),
                                                        self.job("2026-10-26T14:15:08Z", "2026-10-26T14:16:00Z", "2026-10-26T14:26:00Z")])
        self.assertEqual((sp["start"], sp["end"]), (dt("2026-10-26 10:15:10"), dt("2026-10-26 10:26:00")))

    def test_a_job_that_never_started_or_finished_or_a_failed_read_is_none_not_a_guess(self):
        run = self.run_row()
        self.assertIsNone(A.job_span(run, fetch=lambda r: []))
        self.assertIsNone(A.job_span(run, fetch=lambda r: [{"created_at": "2026-10-26T14:15:08Z", "started_at": None, "completed_at": None}]))

        def boom(r):
            raise OSError("api down")
        self.assertIsNone(A.job_span(run, fetch=boom))

    def test_only_successful_completed_runs_are_read(self):
        seen = []

        def fetch(r):
            seen.append(r["id"])
            return [self.job("2026-10-26T14:15:08Z", "2026-10-26T14:15:10Z", "2026-10-26T14:26:00Z")]
        rows = [self.run_row(1), self.run_row(2, conclusion="failure"), dict(self.run_row(3), status="in_progress"), self.run_row(4)]
        out = A.job_spans(rows, fetch=fetch)
        self.assertEqual(sorted(seen), [1, 4])
        self.assertEqual(sorted(out), [1, 4])

    def test_intervals_use_the_job_span_when_there_is_one_and_the_run_span_when_there_is_not_and_say_which(self):
        a, b = self.run_row(1), self.run_row(2, created="2026-10-26T14:30:09Z", updated="2026-10-26T14:41:00Z")
        spans = {1: {"start": dt("2026-10-26 10:27:20"), "end": dt("2026-10-26 10:38:08"), "wait_min": 12.2}}
        stats = {}
        got = A.intervals([a, b], spans, stats)
        self.assertEqual(got[0], (dt("2026-10-26 10:27:20"), dt("2026-10-26 10:38:08")))
        self.assertEqual(got[1], (dt("2026-10-26 10:30:09"), dt("2026-10-26 10:41:00")))
        self.assertEqual(stats, {"job": 1, "fallback": 1})

    def test_without_spans_intervals_are_exactly_the_old_run_spans(self):
        a = self.run_row(1)
        self.assertEqual(A.intervals([a]), [(dt("2026-10-26 10:15:06"), dt("2026-10-26 10:38:08"))])

    def test_the_delayed_morning_is_invisible_by_run_span_and_found_by_job_span(self):
        """The control and the fix on the same data: run 10:00 ends 10:10, the 10:15 run's job only starts at 10:27."""
        rows = [self.run_row(1, "2026-10-26T14:00:08Z", "2026-10-26T14:10:40Z"), self.run_row(2, "2026-10-26T14:15:06Z", "2026-10-26T14:38:08Z"),
                self.run_row(3, "2026-10-26T14:30:09Z", "2026-10-26T14:41:00Z")]
        spans = {1: {"start": dt("2026-10-26 10:00:10"), "end": dt("2026-10-26 10:10:40"), "wait_min": 0.0},
                 2: {"start": dt("2026-10-26 10:27:20"), "end": dt("2026-10-26 10:38:08"), "wait_min": 12.2},
                 3: {"start": dt("2026-10-26 10:38:10"), "end": dt("2026-10-26 10:49:00"), "wait_min": 8.0}}
        by_run = A.measure(A.intervals(rows), dt("2026-10-26 09:45"), dt("2026-10-26 15:55"), HOL)
        by_job = A.measure(A.intervals(rows, spans), dt("2026-10-26 09:45"), dt("2026-10-26 15:55"), HOL)
        self.assertEqual(by_run["unplanned"], 0.0)
        self.assertGreaterEqual(by_job["unplanned"], 16.0)
        self.assertEqual(by_job["episodes"][0]["start"], dt("2026-10-26 10:10:40"))

    def test_the_command_line_says_how_many_runs_were_measured_by_job_and_how_many_were_slow(self):
        import contextlib
        import io
        from unittest import mock
        rows = [dict(self.run_row(1, "2026-10-26T14:00:08Z", "2026-10-26T14:10:40Z")), dict(self.run_row(2, "2026-10-26T14:15:06Z", "2026-10-26T14:38:08Z"))]
        spans = {1: {"start": dt("2026-10-26 10:00:10"), "end": dt("2026-10-26 10:10:40"), "wait_min": 0.0},
                 2: {"start": dt("2026-10-26 10:27:20"), "end": dt("2026-10-26 10:38:08"), "wait_min": 12.2}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(A, "fetch_runs", lambda: rows), mock.patch.object(A, "job_spans", lambda r: spans):
            rc = A.main(["--since", "2026-10-26", "--until", "2026-10-27", "--by-jobs", "--episodes"])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("2 runs by job, 0 by run span", text)
        self.assertIn("took 4+ minutes to start: 1 (worst 12 min)", text)
        self.assertIn("unplanned 10-26 10:10", text)

    def test_without_the_flag_the_command_line_reads_nothing_extra(self):
        import contextlib
        import io
        from unittest import mock
        rows = [self.run_row(1, "2026-10-26T14:00:08Z", "2026-10-26T14:10:40Z")]
        called = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(A, "fetch_runs", lambda: rows), mock.patch.object(A, "job_spans", lambda r: called.append(1) or {}):
            A.main(["--since", "2026-10-26", "--until", "2026-10-27"])
        self.assertEqual(called, [])
        self.assertNotIn("measured from each run's JOB", out.getvalue())


if __name__ == "__main__":
    unittest.main()
