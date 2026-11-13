"""arm_b_downtime.py: the rule behind experiment.json's Arm B downtime figure, pinned.

2026-10-08: the published figure (1484 blind market-minutes = 852 degraded + 632 no-push, 2026-08-24 to 2026-09-22) had no
runnable method, so counting a new crash meant reverse-engineering it. The module now IS the rule and reproduces 852 / 632
exactly. These tests pin each clause of the rule on synthetic snapshots, and pin the published figure itself against the
real committed history (skipped when the checkout is too shallow to hold it).

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

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


class PerDayTests(unittest.TestCase):
    """The 10-27 quota re-measure asks 'degraded minutes per session, with and without the login-expiry day'. One command."""

    def snaps(self):
        rows = []
        for day in ("2026-10-12", "2026-10-13", "2026-10-14"):                       # Mon, Tue, Wed
            t = dt("%s 09:45" % day)
            while t <= dt("%s 15:55" % day):
                rows.append((t.strftime("%Y-%m-%d %H:%M"), "heartbeat"))
                t = t + timedelta(minutes=15)
        return [(dt(t), k) for t, k in rows]

    def test_a_clean_period_has_one_row_per_session_and_zero_blind_minutes(self):
        rows = D.per_day(self.snaps(), dt("2026-10-12 00:00"), dt("2026-10-14 23:59"), HOL)
        self.assertEqual([r["day"] for r in rows], ["2026-10-12", "2026-10-13", "2026-10-14"])
        self.assertTrue(all(r["trading"] == 370 for r in rows))
        self.assertTrue(all(r["blind"] == 0 and r["degraded"] == 0 for r in rows), rows)

    def test_weekends_and_holidays_get_no_row(self):
        rows = D.per_day([(dt("2026-10-09 10:00"), "heartbeat")], dt("2026-10-09 00:00"), dt("2026-10-13 23:59"), HOL)
        self.assertEqual([r["day"] for r in rows], ["2026-10-09", "2026-10-12", "2026-10-13"])           # Fri, Mon, Tue (Sat, Sun skipped)

    def test_a_degraded_stretch_lands_on_its_own_day_only(self):
        snaps = [(t, k) for t, k in [(x[0].strftime("%Y-%m-%d %H:%M"), x[1]) for x in self.snaps()]]
        snaps = [(dt(t), ("degraded" if "2026-10-13 11:" in t or "2026-10-13 12:00" in t else k)) for t, k in snaps]
        rows = {r["day"]: r for r in D.per_day(snaps, dt("2026-10-12 00:00"), dt("2026-10-14 23:59"), HOL)}
        self.assertGreater(rows["2026-10-13"]["degraded"], 60)
        self.assertEqual(rows["2026-10-12"]["degraded"], 0)
        self.assertEqual(rows["2026-10-14"]["degraded"], 0)

    def test_the_summary_can_leave_a_named_day_out(self):
        rows = [{"day": "d1", "degraded": 170.0, "blind": 170.0}, {"day": "d2", "degraded": 30.0, "blind": 30.0},
                {"day": "d3", "degraded": 0.0, "blind": 0.0}]
        allr = D.summarize_days(rows)
        self.assertEqual((allr["sessions"], round(allr["degraded_per_session"], 1)), (3, 66.7))
        left = D.summarize_days(rows, exclude=("d2",))
        self.assertEqual((left["sessions"], left["degraded_per_session"], left["excluded"]), (2, 85.0, ["d2"]))

    def test_an_empty_summary_is_none_not_a_zero_or_a_crash(self):
        s = D.summarize_days([])
        self.assertEqual((s["sessions"], s["degraded_per_session"], s["blind_per_session"]), (0, None, None))
        s = D.summarize_days([{"day": "d1", "degraded": 5.0, "blind": 5.0}], exclude=("d1",))
        self.assertEqual((s["sessions"], s["degraded_per_session"]), (0, None))

    def test_an_until_date_at_midnight_does_not_add_an_empty_session_that_dilutes_the_mean(self):
        """2026-10-28: --until 2026-10-28 listed 10-28 as a session with 0 minutes, so 201 degraded minutes over 10 sessions read 18.3, not 20.1."""
        rows = D.per_day(self.snaps(), dt("2026-10-12 00:00"), dt("2026-10-14 00:00"), HOL)
        self.assertEqual([r["day"] for r in rows], ["2026-10-12", "2026-10-13"])
        self.assertTrue(all(r["trading"] > 0 for r in rows))

    def test_the_published_baseline_is_86_minutes_per_session(self):
        self.assertEqual(D.BASELINE_DEGRADED_PER_SESSION, 86)

    def test_the_command_line_prints_the_table_and_both_means(self):
        lines = ["rh bot %sT%s ET (heartbeat)" % (t[:10], t[11:]) for t, _ in [(x[0].strftime("%Y-%m-%d %H:%M"), 0) for x in self.snaps()]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(D, "git_subjects", lambda since="": lines):
            rc = D.main(["--since", "2026-10-12", "--per-day", "--exclude", "2026-10-13"])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("2026-10-13", text)
        self.assertIn("(excluded below)", text)
        self.assertIn("all sessions", text)
        self.assertIn("without the excluded days", text)
        self.assertIn("baseline 86", text)

    def test_without_the_flag_the_output_is_the_old_one(self):
        lines = ["rh bot %sT%s ET (heartbeat)" % (x[0].strftime("%Y-%m-%d"), x[0].strftime("%H:%M")) for x in self.snaps()]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(D, "git_subjects", lambda since="": lines):
            D.main(["--since", "2026-10-12"])
        self.assertNotIn("min/session", out.getvalue())
        self.assertIn("HEADLINE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
