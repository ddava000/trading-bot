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


if __name__ == "__main__":
    unittest.main()
