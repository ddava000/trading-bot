"""ab_compare.py: the 11-24 scoreboard. Every piece of arithmetic is pinned against numbers worked out by hand, because the one
thing this script must never do is look more certain than the data allows.

First real run, 2026-10-28, found its own data problem: Arm B's first row (2026-08-24) reads 124.00 between a 231.30 start and 239.74 the
next day, which printed a +93% day and a +91% time-weighted return. The glitch filter, the day-zero anchor and their tests exist because of that.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import math
import os
import statistics
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ab_compare as C  # noqa: E402


class DailyReturns(unittest.TestCase):
    def test_no_deposits(self):
        r = C.daily_returns({"2026-10-01": 100.0, "2026-10-02": 110.0, "2026-10-05": 99.0})
        self.assertEqual([d for d, _ in r], ["2026-10-02", "2026-10-05"])
        self.assertAlmostEqual(r[0][1], 0.10)
        self.assertAlmostEqual(r[1][1], -0.10)

    def test_a_deposit_is_not_a_gain_and_the_two_conventions_bracket_the_answer(self):
        eq = {"2026-10-01": 100.0, "2026-10-02": 112.0}
        dep = {"2026-10-01": 0.0, "2026-10-02": 10.0}
        start = C.daily_returns(eq, dep, "start")[0][1]
        end = C.daily_returns(eq, dep, "end")[0][1]
        self.assertAlmostEqual(start, 112.0 / 110.0 - 1.0)
        self.assertAlmostEqual(end, (112.0 - 10.0) / 100.0 - 1.0)
        self.assertLess(start, end)

    def test_a_deposit_that_buys_nothing_extra_is_a_zero_return_not_a_ten_percent_gain(self):
        eq = {"2026-10-01": 100.0, "2026-10-02": 110.0}
        dep = {"2026-10-01": 0.0, "2026-10-02": 10.0}
        self.assertAlmostEqual(C.daily_returns(eq, dep, "start")[0][1], 0.0)
        self.assertAlmostEqual(C.daily_returns(eq, dep, "end")[0][1], 0.0)

    def test_missing_days_and_a_non_positive_prior_give_no_return_instead_of_a_crash(self):
        self.assertEqual(C.daily_returns({}), [])
        self.assertEqual(C.daily_returns({"2026-10-01": 100.0}), [])
        self.assertEqual(C.daily_returns({"2026-10-01": 0.0, "2026-10-02": 5.0}), [])
        self.assertEqual(C.daily_returns({"2026-10-01": None, "2026-10-02": 5.0}), [])


class ChainAndDrawdown(unittest.TestCase):
    R = [("a", 0.10), ("b", -0.20), ("c", 0.05)]

    def test_chain(self):
        self.assertAlmostEqual(C.chain(self.R), 1.1 * 0.8 * 1.05 - 1.0)
        self.assertEqual(C.chain([]), 0.0)

    def test_max_drawdown_is_peak_to_trough_of_the_index(self):
        self.assertAlmostEqual(C.max_drawdown(self.R), 0.88 / 1.1 - 1.0)
        self.assertEqual(C.max_drawdown([("a", 0.1), ("b", 0.1)]), 0.0)
        self.assertEqual(C.max_drawdown([]), 0.0)

    def test_risk_summary(self):
        rs = C.risk_summary(self.R)
        self.assertEqual((rs["days"], rs["worst_day"], rs["best_day"]), (3, -0.20, 0.10))
        self.assertAlmostEqual(rs["up_days"], 2 / 3)
        self.assertAlmostEqual(rs["stdev"], statistics.stdev([0.10, -0.20, 0.05]))
        self.assertEqual(C.risk_summary([])["days"], 0)
        self.assertIsNone(C.risk_summary([("a", 0.1)])["stdev"])


class PairedGap(unittest.TestCase):
    def test_matches_a_hand_computation(self):
        a = [("d1", 0.02), ("d2", 0.00), ("d3", 0.01), ("d4", 0.03)]
        b = [("d1", 0.01), ("d2", -0.03), ("d3", 0.02), ("d4", 0.01)]
        diffs = [0.01, 0.03, -0.01, 0.02]
        g = C.paired_gap(a, b)
        self.assertEqual(g["days"], 4)
        self.assertAlmostEqual(g["mean"], statistics.mean(diffs))
        sd = statistics.stdev(diffs)
        self.assertAlmostEqual(g["se"], sd / 2.0)
        self.assertAlmostEqual(g["t"], statistics.mean(diffs) / (sd / 2.0))
        self.assertEqual(g["days_needed"], math.ceil((1.96 * sd / statistics.mean(diffs)) ** 2))

    def test_only_the_days_both_arms_have_are_compared(self):
        g = C.paired_gap([("d1", 0.01), ("d2", 0.02), ("d3", 0.03)], [("d2", 0.0), ("d3", 0.0), ("d9", 0.5)])
        self.assertEqual(g["days"], 2)

    def test_too_little_data_says_so_instead_of_inventing_a_number(self):
        for a, b in (([], []), ([("d1", 0.1)], [("d1", 0.0)]), ([("d1", 0.1)], [("d2", 0.0)])):
            g = C.paired_gap(a, b)
            self.assertIsNone(g["mean"])
            self.assertIsNone(g["t"])

    def test_identical_arms_have_no_gap_and_need_no_days_estimate(self):
        g = C.paired_gap([("d1", 0.01), ("d2", 0.02)], [("d1", 0.01), ("d2", 0.02)])
        self.assertEqual(g["mean"], 0.0)
        self.assertIsNone(g["days_needed"])

    def test_a_bigger_gap_needs_fewer_days(self):
        noise = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
        small = C.paired_gap([("d%d" % i, 0.001 + n) for i, n in enumerate(noise)], [("d%d" % i, 0.0) for i in range(6)])
        big = C.paired_gap([("d%d" % i, 0.01 + n) for i, n in enumerate(noise)], [("d%d" % i, 0.0) for i in range(6)])
        self.assertGreater(small["days_needed"], big["days_needed"])


class GlitchFilter(unittest.TestCase):
    def test_the_real_one_arm_b_08_24_reading_of_124_is_dropped_and_listed(self):
        eq = {"2026-08-24": 124.00, "2026-08-25": 239.74, "2026-08-26": 240.31, "2026-08-27": 241.75}
        keep, dropped = C.drop_glitches(eq)
        self.assertEqual(dropped, [("2026-08-24", 124.0)])
        self.assertNotIn("2026-08-24", keep)

    def test_an_ordinary_bad_day_is_not_a_glitch(self):
        eq = {"d1": 100.0, "d2": 96.0, "d3": 92.0, "d4": 90.0, "d5": 91.0}
        keep, dropped = C.drop_glitches(eq)
        self.assertEqual(dropped, [])
        self.assertEqual(len(keep), 5)

    def test_a_spike_in_the_middle_is_dropped(self):
        keep, dropped = C.drop_glitches({"d1": 100.0, "d2": 101.0, "d3": 160.0, "d4": 102.0, "d5": 103.0})
        self.assertEqual(dropped, [("d3", 160.0)])

    def test_two_points_cannot_say_which_is_wrong_so_nothing_is_dropped(self):
        keep, dropped = C.drop_glitches({"d1": 100.0, "d2": 160.0})
        self.assertEqual(dropped, [])

    def test_a_genuine_level_shift_is_not_erased(self):
        keep, dropped = C.drop_glitches({"d1": 100.0, "d2": 101.0, "d3": 99.0, "d4": 100.0})
        self.assertEqual(dropped, [])

    def test_anchoring_adds_the_declared_basis_as_day_zero(self):
        a = C.anchored({"2026-08-24": 246.31}, 247.91, "2026-08-23")
        self.assertEqual(a["2026-08-23"], 247.91)
        r = C.daily_returns(a)
        self.assertAlmostEqual(r[0][1], 246.31 / 247.91 - 1.0)


def rows(*spec):
    """spec: (date, a_equity, b_equity, b_deposits_cum)."""
    out = []
    for d, a, b, dep in spec:
        out.append({"date": d, "a_equity": a, "b_equity": b, "b_deposits": dep,
                    "a_pl": None if a is None else a - 200.0, "b_pl": None if b is None else b - 300.0 - dep})
    return out


class Score(unittest.TestCase):
    ROWS = rows(("2026-10-05", 198.0, 301.0, 0.0), ("2026-10-06", 196.0, 302.0, 0.0), ("2026-10-07", 200.0, 314.0, 10.0),
                ("2026-10-08", 202.0, 315.0, 10.0), ("2026-10-09", 201.0, 316.0, 10.0))

    def test_dollars_and_percents_follow_the_experiment_rule(self):
        s = C.score(self.ROWS, 200.0, 300.0, None)
        self.assertEqual((s["first"], s["last"], s["rows"]), ("2026-10-05", "2026-10-09", 5))
        self.assertAlmostEqual(s["a_pl"], 1.0)
        self.assertAlmostEqual(s["a_pct"], 100.0 * 1.0 / 200.0)
        self.assertAlmostEqual(s["b_pl"], 316.0 - 300.0 - 10.0)
        self.assertAlmostEqual(s["b_basis"], 310.0)
        self.assertAlmostEqual(s["b_pct"], 100.0 * 6.0 / 310.0)

    def test_the_time_weighted_return_starts_from_the_declared_basis_and_brackets_arm_bs_deposit_timing(self):
        s = C.score(self.ROWS, 200.0, 300.0, None)
        self.assertAlmostEqual(s["twr_a"], 100.0 * (198 / 200 * 196 / 198 * 200 / 196 * 202 / 200 * 201 / 202 - 1.0))
        self.assertLess(s["twr_b_start"], s["twr_b_end"])
        self.assertIsNone(s["spy_twr"])

    def test_spy_is_aligned_to_the_window_and_uses_the_close_before_the_first_day(self):
        spy = {"2026-10-02": 100.0, "2026-10-05": 101.0, "2026-10-06": 102.01, "2026-10-07": 102.01, "2026-10-08": 103.0, "2026-10-09": 104.0, "2026-10-12": 90.0}
        s = C.score(self.ROWS, 200.0, 300.0, spy)
        self.assertAlmostEqual(s["spy_twr"], 100.0 * (104.0 / 100.0 - 1.0), places=6)      # 10-12 is after the window and ignored

    def test_a_glitch_row_is_reported_by_arm_and_does_not_poison_the_return(self):
        bad = rows(("2026-10-05", 198.0, 120.0, 0.0), ("2026-10-06", 196.0, 302.0, 0.0), ("2026-10-07", 200.0, 303.0, 0.0), ("2026-10-08", 202.0, 304.0, 0.0))
        s = C.score(bad, 200.0, 300.0, None)
        self.assertEqual(s["glitches"]["Arm B"], [("2026-10-05", 120.0)])
        self.assertEqual(s["glitches"]["Arm A"], [])
        self.assertLess(abs(s["twr_b_start"]), 5.0)

    def test_an_empty_window_does_not_crash(self):
        s = C.score([], 200.0, 300.0, None)
        self.assertEqual(s["rows"], 0)
        self.assertIsNone(s["a_pl"])


class SpyFetch(unittest.TestCase):
    @staticmethod
    def payload():
        t = lambda d: int(datetime(*d, 15, 0, tzinfo=timezone.utc).timestamp())
        return {"chart": {"result": [{"timestamp": [t((2026, 10, 1)), t((2026, 10, 2)), t((2026, 10, 5))],
                                      "indicators": {"quote": [{"close": [100.0, None, 102.0]}]}}]}}

    def test_closes_are_keyed_by_the_eastern_date_and_missing_ones_skipped(self):
        got = C.fetch_spy("2026-10-01", fetch=self.payload)
        self.assertEqual(got, {"2026-10-01": 100.0, "2026-10-05": 102.0})

    def test_a_failed_read_is_none_never_an_empty_benchmark(self):
        def boom():
            raise OSError("down")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(C.fetch_spy("2026-10-01", fetch=boom))
            self.assertIsNone(C.fetch_spy("2026-10-01", fetch=lambda: {"chart": {"result": None}}))

    def test_spy_returns(self):
        r = C.spy_returns({"d1": 100.0, "d2": 110.0, "d3": 99.0})
        self.assertAlmostEqual(r[0][1], 0.10)
        self.assertAlmostEqual(r[1][1], -0.10)


class TheMemo(unittest.TestCase):
    def text(self, spy=None, downtime=None):
        s = C.score(Score.ROWS, 200.0, 300.0, spy)
        return C.memo(s, downtime)

    def test_it_is_ascii_and_has_no_em_dash(self):
        t = self.text()
        t.encode("ascii")
        self.assertNotIn("—", t)

    def test_it_never_names_a_winner(self):
        t = self.text().lower()
        for phrase in ("recommend", "should keep", "winner", "switch to"):
            self.assertNotIn(phrase, t)

    def test_with_too_little_data_it_says_not_distinguishable_from_luck(self):
        self.assertIn("NOT distinguishable from luck", self.text())

    def test_it_carries_its_standing_caveats_and_the_bundle_reading(self):
        t = self.text(downtime={"Arm B blind minutes": "5.4%"})
        self.assertIn("WHAT THIS CANNOT SHOW", t)
        self.assertIn("BUNDLE", t)
        self.assertIn("NOT adjusted for", t)
        self.assertIn("not tax advice", t)
        self.assertIn("a verdict", t)

    def test_a_missing_spy_is_called_out_because_the_rule_requires_it(self):
        self.assertIn("do not decide without it", self.text())
        self.assertIn("SPY over the same days", self.text(spy={"2026-10-02": 100.0, "2026-10-05": 101.0, "2026-10-09": 104.0}))

    def test_the_end_to_end_command_prints_the_memo_offline(self):
        fake = {"rows": Score.ROWS, "a_base": 200.0, "b_start": 300.0, "events": [], "window": {}, "b_dropped": 0}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(C, "load", lambda: fake):
            rc = C.main(["--no-spy"])
        self.assertEqual(rc, 0)
        self.assertIn("A/B SCOREBOARD", out.getvalue())


class ExactNumbers(unittest.TestCase):
    """The first mutation run found the memo's formatting and scaling unpinned: a wrong factor of 100 or a lost minus sign would have printed
    a plausible number. These pin the exact lines for a worked example."""

    ROWS = Score.ROWS

    def memo(self, spy=None):
        return C.memo(C.score(self.ROWS, 200.0, 300.0, spy))

    def test_arm_b_time_weighted_is_pinned_for_both_deposit_conventions(self):
        s = C.score(self.ROWS, 200.0, 300.0, None)
        b = [301.0, 302.0, 314.0, 315.0, 316.0]
        start = (301.0 / 300.0) * (302.0 / 301.0) * (314.0 / (302.0 + 10.0)) * (315.0 / 314.0) * (316.0 / 315.0) - 1.0
        end = (301.0 / 300.0) * (302.0 / 301.0) * ((314.0 - 10.0) / 302.0) * (315.0 / 314.0) * (316.0 / 315.0) - 1.0
        self.assertAlmostEqual(s["twr_b_start"], 100.0 * start, places=9)
        self.assertAlmostEqual(s["twr_b_end"], 100.0 * end, places=9)

    def test_a_deposit_booked_on_the_very_first_day_is_a_flow_not_a_gain(self):
        rs = rows(("2026-10-05", 310.0, 310.0, 10.0), ("2026-10-06", 310.0, 310.0, 10.0))
        s = C.score(rs, 300.0, 300.0, None)
        self.assertAlmostEqual(s["twr_b_start"], 0.0, places=9)         # 310 / (300 + 10) = flat, not +3.3%
        self.assertAlmostEqual(s["twr_a"], 100.0 * (310.0 / 300.0 - 1.0), places=9)

    def test_the_rule_lines_of_the_memo_are_exact(self):
        t = self.memo()
        self.assertIn("Arm A: +$1.00 (+0.50% of its $200.00 start)", t)
        self.assertIn("Arm B: +$6.00 (+1.94% of its $310.00 basis: start plus deposits)", t)

    def test_a_loss_prints_a_minus_sign_in_front_of_the_dollar_sign(self):
        self.assertEqual(C._usd(-22.46), "-$22.46")
        self.assertEqual(C._usd(3.5), "+$3.50")
        self.assertEqual(C._usd(None), "n/a")
        self.assertEqual(C._frac(-0.0279), "-2.79%")
        self.assertEqual(C._frac(0.0153, 3), "+1.530%")
        self.assertEqual(C._pct(-9.06), "-9.06%")

    def test_the_risk_and_gap_lines_are_exact(self):
        t = self.memo()
        a = [198 / 200 - 1, 196 / 198 - 1, 200 / 196 - 1, 202 / 200 - 1, 201 / 202 - 1]
        self.assertIn("Arm A: worst day %s, best day %s" % (C._frac(min(a)), C._frac(max(a))), t)
        self.assertIn("up on 40% of 5 days", t)
        g = C.paired_gap(C.daily_returns({r["date"]: r["a_equity"] for r in self.ROWS} | {"2026-10-04": 200.0}),
                         C.daily_returns({r["date"]: r["b_equity"] for r in self.ROWS} | {"2026-10-04": 300.0},
                                         {r["date"]: r["b_deposits"] for r in self.ROWS} | {"2026-10-04": 0.0}, "start"))
        self.assertIn("mean gap %s per day, standard error %s, t = %.2f over %d days" % (C._frac(g["mean"], 3), C._frac(g["se"], 3), g["t"], g["days"]), t)

    def test_the_excess_over_spy_is_the_difference_of_the_two_time_weighted_figures(self):
        spy = {"2026-10-02": 100.0, "2026-10-05": 100.0, "2026-10-06": 100.0, "2026-10-07": 100.0, "2026-10-08": 100.0, "2026-10-09": 101.0}
        s = C.score(self.ROWS, 200.0, 300.0, spy)
        t = C.memo(s)
        self.assertAlmostEqual(s["spy_twr"], 1.0, places=9)
        self.assertIn("Arm A %+.2f, Arm B %+.2f" % (s["twr_a"] - 1.0, s["twr_b_start"] - 1.0), t)
        self.assertIn("SPY over the same days: +1.00%", t)

    def test_a_clear_gap_is_called_larger_than_luck_and_a_noisy_one_is_not(self):
        steady = [("d%02d" % i, 0.010 + (0.0001 if i % 2 else -0.0001)) for i in range(30)]
        zero = [("d%02d" % i, 0.0) for i in range(30)]
        g = C.paired_gap(steady, zero)
        self.assertGreater(abs(g["t"]), C.Z95)
        s = {"first": "a", "last": "b", "rows": 30, "a_pl": 1.0, "b_pl": 0.0, "a_pct": 1.0, "b_pct": 0.0, "b_basis": 100.0, "a_base": 100.0,
             "glitches": {}, "twr_a": 30.0, "twr_b_start": 0.0, "twr_b_end": 0.0, "spy_twr": None,
             "risk_a": C.risk_summary(steady), "risk_b": C.risk_summary(zero), "gap": g}
        self.assertIn("larger than luck at 95%, in this window only", C.memo(s))
        self.assertNotIn("NOT distinguishable", C.memo(s))
        s["gap"] = C.paired_gap([("d1", 0.05), ("d2", -0.05), ("d3", 0.04)], [("d1", 0.0), ("d2", 0.0), ("d3", 0.0)])
        self.assertIn("NOT distinguishable from luck", C.memo(s))

    def test_exactly_two_overlapping_days_is_enough_for_a_number(self):
        self.assertIsNotNone(C.paired_gap([("d1", 0.1), ("d2", 0.2)], [("d1", 0.0), ("d2", 0.1)])["mean"])

    def test_a_zero_close_is_skipped_not_divided_by(self):
        got = C.spy_returns({"d1": 0.0, "d2": 5.0, "d3": 6.0})
        self.assertEqual([d for d, _ in got], ["d3"])
        self.assertAlmostEqual(got[0][1], 0.2)

    def test_the_spy_lookback_is_seven_days(self):
        payload = {"chart": {"result": [{"timestamp": [int(datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc).timestamp()),
                                                       int(datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc).timestamp())],
                                         "indicators": {"quote": [{"close": [1.0, 2.0]}]}}]}}
        got = C.fetch_spy("2026-10-01", fetch=lambda: payload)
        self.assertEqual(got, {"2026-09-24": 2.0})                      # 10-01 minus 7 days is 09-24: kept; 09-23 is not

    def test_the_day_zero_anchor_is_the_day_before_the_first_row(self):
        s = C.score(self.ROWS, 200.0, 300.0, None)
        a, _, _ = C.series(self.ROWS)
        self.assertEqual(sorted(C.anchored(a, 200.0, "2026-10-04"))[0], "2026-10-04")
        self.assertAlmostEqual(s["twr_a"], 100.0 * (201.0 / 200.0 - 1.0), places=9)

    def test_main_reads_spy_unless_told_not_to(self):
        fake = {"rows": Score.ROWS, "a_base": 200.0, "b_start": 300.0, "events": [], "window": {}, "b_dropped": 0}
        seen = []

        def spy(start):
            seen.append(start)
            return {"2026-10-02": 100.0, "2026-10-05": 100.0, "2026-10-09": 102.0}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(C, "load", lambda: fake), mock.patch.object(C, "fetch_spy", spy):
            C.main([])
        self.assertEqual(seen, ["2026-10-05"])
        self.assertIn("SPY over the same days: +2.00%", out.getvalue())


class Hygiene(unittest.TestCase):
    def test_the_file_is_ascii_and_in_the_ascii_check_list(self):
        with open(os.path.join(ROOT, "ab_compare.py"), "rb") as f:
            self.assertTrue(f.read().isascii())
        with open(os.path.join(ROOT, "check_ascii.py"), encoding="utf-8") as f:
            self.assertIn('"ab_compare.py"', f.read())

    def test_it_places_nothing_and_imports_no_broker_client(self):
        with open(os.path.join(ROOT, "ab_compare.py"), encoding="utf-8") as f:
            src = f.read()
        for word in ("alpaca_bot", "place_order", "submit_order", "robin"):
            self.assertNotIn(word, src.replace("Robinhood", "").replace("robinhood", ""))


if __name__ == "__main__":
    unittest.main()
