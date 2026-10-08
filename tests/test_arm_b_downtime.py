"""arm_b_downtime.py: the rule behind experiment.json's Arm B downtime figure, pinned.

2026-10-08: the published figure (1484 blind market-minutes = 852 degraded + 632 no-push, 2026-08-24 to 2026-09-22) had no
runnable method, so counting a new crash meant reverse-engineering it. The module now IS the rule and reproduces 852 / 632
exactly. These tests pin each clause of the rule on synthetic snapshots, and pin the published figure itself against the
real committed history (skipped when the checkout is too shallow to hold it).

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arm_b_downtime as D  # noqa: E402

HOL = D.holidays()


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def run(snaps, start, end, **kw):
    return D.measure([(dt(t), k) for t, k in snaps], dt(start), dt(end), HOL, **kw)


class WindowTests(unittest.TestCase):
    def test_a_full_session_is_370_minutes(self):
        self.assertEqual(D.window_minutes(dt("2026-10-07 00:00"), dt("2026-10-07 23:59"), HOL), 370)

    def test_a_weekend_is_nothing(self):
        self.assertEqual(D.window_minutes(dt("2026-10-03 00:00"), dt("2026-10-04 23:59"), HOL), 0)

    def test_a_market_holiday_is_nothing(self):
        """Labor Day 2026-09-07: counting it as a trading day once turned a holiday weekend into a 412-minute outage."""
        self.assertIn("2026-09-07", HOL)
        self.assertEqual(D.window_minutes(dt("2026-09-07 00:00"), dt("2026-09-07 23:59"), HOL), 0)

    def test_only_the_trading_window_counts_not_exchange_hours(self):
        self.assertEqual(D.window_minutes(dt("2026-10-07 09:30"), dt("2026-10-07 16:00"), HOL), 370)


class ParseTests(unittest.TestCase):
    def test_subjects_are_parsed_sorted_and_junk_is_skipped(self):
        rows = D.parse_subjects(["rh bot 2026-10-08T10:43 ET (change)", "unrelated commit", "rh bot 2026-10-07T15:43 ET (heartbeat)",
                                 "rh bot 2026-10-08T10:17 ET (degraded)"])
        self.assertEqual([(t.strftime("%m-%d %H:%M"), k) for t, k in rows],
                         [("10-07 15:43", "heartbeat"), ("10-08 10:17", "degraded"), ("10-08 10:43", "change")])


class RuleTests(unittest.TestCase):
    def test_healthy_heartbeats_are_not_downtime(self):
        r = run([("2026-10-07 10:00", "heartbeat"), ("2026-10-07 10:15", "heartbeat"), ("2026-10-07 10:30", "heartbeat")],
                "2026-10-07 10:00", "2026-10-07 10:30")
        self.assertEqual((r["degraded"], r["no_push"]), (0, 0))

    def test_a_short_gap_after_a_degraded_snapshot_is_degraded_minutes(self):
        r = run([("2026-10-07 14:27", "degraded"), ("2026-10-07 14:32", "degraded"), ("2026-10-07 14:37", "degraded"),
                 ("2026-10-07 14:42", "heartbeat")], "2026-10-07 14:27", "2026-10-07 14:42")
        self.assertEqual((r["degraded"], r["no_push"]), (15, 0))

    def test_the_gap_threshold_is_strictly_greater_than_30(self):
        snaps = lambda gap: [("2026-10-07 11:00", "heartbeat"), ("2026-10-07 %02d:%02d" % divmod(11 * 60 + gap, 60), "heartbeat")]
        self.assertEqual(run(snaps(30), "2026-10-07 11:00", "2026-10-07 12:00")["no_push"], 0)
        self.assertEqual(run(snaps(31), "2026-10-07 11:00", "2026-10-07 12:00")["no_push"], 31)

    def test_a_midday_crash_is_no_push_and_not_edge(self):
        r = run([("2026-10-07 11:00", "heartbeat"), ("2026-10-07 12:00", "change")], "2026-10-07 11:00", "2026-10-07 12:00")
        self.assertEqual((r["no_push"], r["edge"]), (60, 0))

    def test_a_degraded_snapshot_before_a_long_gap_covers_only_its_first_5_minutes(self):
        r = run([("2026-10-07 11:00", "degraded"), ("2026-10-07 12:00", "change")], "2026-10-07 11:00", "2026-10-07 12:00")
        self.assertEqual((r["degraded"], r["no_push"]), (5, 55))

    def test_an_overnight_gap_scores_only_the_two_edges_and_flags_them_as_edge(self):
        r = run([("2026-10-06 15:43", "heartbeat"), ("2026-10-07 09:50", "change")], "2026-10-06 00:00", "2026-10-07 23:59")
        self.assertEqual((r["no_push"], r["edge"]), (17, 17))

    def test_last_nights_crash_is_32_real_minutes_plus_a_12_minute_edge(self):
        """2026-10-07 15:43 -> 2026-10-08 10:17. LAPTOP DAILY CHECK's log said about 31; the pushes say 32 plus the close edge."""
        r = run([("2026-10-07 15:43", "heartbeat"), ("2026-10-08 10:17", "change")], "2026-10-07 00:00", "2026-10-08 23:59")
        self.assertEqual((r["no_push"], r["edge"]), (44, 12))

    def test_a_weekend_and_a_holiday_add_no_window_minutes(self):
        fri = run([("2026-10-02 15:47", "heartbeat"), ("2026-10-05 09:46", "change")], "2026-10-02 00:00", "2026-10-05 23:59")
        self.assertEqual(fri["no_push"], 9)
        labor = run([("2026-09-04 15:46", "heartbeat"), ("2026-09-08 09:50", "change")], "2026-09-04 00:00", "2026-09-08 23:59")
        self.assertEqual(labor["no_push"], 9 + 5)

    def test_the_period_clips_a_gap_that_straddles_its_edge(self):
        r = run([("2026-10-07 11:00", "heartbeat"), ("2026-10-07 13:00", "change")], "2026-10-07 12:00", "2026-10-07 12:30")
        self.assertEqual(r["no_push"], 30)

    def test_trading_minutes_is_the_denominator(self):
        r = run([("2026-10-06 10:00", "heartbeat"), ("2026-10-07 10:00", "heartbeat")], "2026-10-06 00:00", "2026-10-07 23:59")
        self.assertEqual(r["trading"], 740)


class PublishedFigure(unittest.TestCase):
    def test_the_rule_reproduces_the_published_852_and_632_from_real_history(self):
        try:
            snaps = D.parse_subjects(D.git_subjects())
        except Exception as e:   # no git, not a repo
            self.skipTest("no git history: %s" % e)
        if not snaps or snaps[0][0] > dt("2026-08-24 09:00") or snaps[-1][0] < dt("2026-09-22 12:00"):
            self.skipTest("checkout too shallow to hold 2026-08-24 to 2026-09-22")
        r = D.measure(snaps, dt("2026-08-24 00:00"), dt("2026-09-22 11:01"), HOL)
        self.assertEqual((round(r["degraded"]), round(r["no_push"])), (852, 632))
        self.assertEqual(round(r["trading"]), 7476)


if __name__ == "__main__":
    unittest.main()
