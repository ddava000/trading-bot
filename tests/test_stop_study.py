"""stop_study.py: how far past the documented -7% stop Arm A's stop_loss exits filled. The arithmetic is pinned on a worked example, and the
report is checked to say plainly what it cannot show, because the whole point is a number that does not look more certain than it is.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import stop_study as S  # noqa: E402


def lot(sale, symbol, basis, proceeds, qty=1.0):
    return {"sale": sale, "symbol": symbol, "basis": basis, "proceeds": proceeds, "gain": proceeds - basis, "qty": qty,
            "open_date": "2026-09-01", "close_date": "2026-09-10"}


def sell(order_id, live, **flags):
    r = {"side": "sell", "order_id": order_id, "live": live}
    r.update(flags)
    return r


REAL = {"closed": [lot("o1", "AAA", 100.0, 90.0),      # -10%: 3 points past the level
                   lot("o2", "BBB", 200.0, 188.0),     # -6%: better than the level (a gap up or a ratchet)
                   lot("o3", "CCC", 100.0, 75.0),      # -25%: a hold stop, not a stop_loss
                   lot("o4", "DDD", 100.0, 110.0),     # take profit
                   lot("o5", "EEE", 100.0, 99.0),      # sell with no reason flag
                   lot("zz", "FFF", 100.0, 80.0)]}     # no logged sell at all
LOG = [sell("o1", 91.0, stop_loss=True), sell("o2", 190.0, stop_loss=True), sell("o3", 76.0, hold_stop=True),
       sell("o4", 110.0, take_profit=True), sell("o5", 99.0), {"side": "buy", "order_id": "b1", "stop_loss": True}]


class Join(unittest.TestCase):
    def rows(self):
        return {r["symbol"]: r for r in S.join(REAL, LOG)}

    def test_each_lot_gets_the_reason_on_its_sell(self):
        got = {k: v["reason"] for k, v in self.rows().items()}
        self.assertEqual(got, {"AAA": "stop_loss", "BBB": "stop_loss", "CCC": "hold_stop", "DDD": "take_profit",
                               "EEE": "other", "FFF": "unmatched"})

    def test_return_is_gain_over_basis(self):
        self.assertAlmostEqual(self.rows()["AAA"]["ret"], -0.10)
        self.assertAlmostEqual(self.rows()["CCC"]["ret"], -0.25)

    def test_fill_is_proceeds_per_share_and_decision_is_the_logged_live_price(self):
        r = S.join({"closed": [lot("o1", "AAA", 100.0, 90.0, qty=2.0)]}, [sell("o1", 46.0, stop_loss=True)])[0]
        self.assertAlmostEqual(r["fill"], 45.0)
        self.assertEqual(r["decision"], 46.0)

    def test_a_buy_row_with_a_matching_id_is_never_a_sale(self):
        r = S.join({"closed": [lot("b1", "AAA", 100.0, 90.0)]}, [{"side": "buy", "order_id": "b1", "stop_loss": True}])[0]
        self.assertEqual(r["reason"], "unmatched")

    def test_zero_basis_or_qty_is_not_divided_by(self):
        r = S.join({"closed": [lot("o1", "AAA", 0.0, 5.0, qty=0.0)]}, [sell("o1", 1.0, stop_loss=True)])[0]
        self.assertIsNone(r["ret"])
        self.assertIsNone(r["fill"])

    def test_the_first_set_flag_in_the_documented_order_wins(self):
        r = S.join({"closed": [lot("o1", "AAA", 100.0, 90.0)]}, [sell("o1", 1.0, time_stop=True, stop_loss=True)])[0]
        self.assertEqual(r["reason"], "stop_loss")


class Study(unittest.TestCase):
    def st(self):
        return S.study(S.join(REAL, LOG))

    def test_only_stop_loss_exits_are_measured_against_the_level(self):
        st = self.st()
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["worse"], 1)                                     # AAA only; BBB filled better than -7%
        self.assertAlmostEqual(st["worst"], -0.10)
        self.assertAlmostEqual(st["mean_overshoot_pts"], 3.0)                # (-7% - -10%) x 100, over the one that overshot

    def test_dollars_compare_actual_with_a_perfect_fill_at_the_level(self):
        st = self.st()
        self.assertAlmostEqual(st["actual"], -10.0 + -12.0)
        self.assertAlmostEqual(st["perfect"], -0.07 * 300.0)                 # -21.00
        self.assertAlmostEqual(st["cost"], -22.0 - -21.0)
        self.assertAlmostEqual(st["basis"], 300.0)

    def test_fill_versus_decision_is_the_median_of_the_per_lot_ratios(self):
        # AAA fills 90 against a decision price of 91 (-1.0989%); BBB 188 against 190 (-1.0526%); the median of two is their mean
        want = ((90.0 / 91.0 - 1.0) + (188.0 / 190.0 - 1.0)) / 2.0 * 100.0
        self.assertAlmostEqual(self.st()["median_fill_vs_decision_pct"], want)

    def test_the_hold_stop_is_reported_in_the_table_but_not_counted_as_a_stop_loss(self):
        t = self.st()["table"]
        self.assertEqual(t["hold_stop"]["lots"], 1)
        self.assertAlmostEqual(t["hold_stop"]["worst"], -0.25)
        self.assertEqual(t["stop_loss"]["lots"], 2)

    def test_the_largest_overshoots_come_worst_first(self):
        self.assertEqual([r["symbol"] for r in self.st()["biggest"]], ["AAA", "BBB"])

    def test_nothing_to_measure_is_said_not_zero(self):
        st = S.study(S.join({"closed": [lot("o4", "DDD", 100.0, 110.0)]}, [sell("o4", 1.0, take_profit=True)]))
        self.assertEqual(st["n"], 0)
        self.assertIn("no stop_loss exits in the ledger", S.report(st))

    def test_a_different_level_moves_the_comparison(self):
        st = S.study(S.join(REAL, LOG), level=-0.05)
        self.assertEqual(st["worse"], 2)                                     # now both are past the level


class Report(unittest.TestCase):
    def text(self):
        return S.report(S.study(S.join(REAL, LOG)))

    def test_the_headline_numbers_are_printed(self):
        t = self.text()
        self.assertIn("2 stop_loss exits: 1 filled below the level (mean overshoot 3.00 points), worst -10.00%", t)
        self.assertIn("the overshoot cost -1.00 on $300.00 of positions", t)

    def test_it_says_what_it_cannot_show(self):
        t = self.text()
        self.assertIn("NOT SHOWN", t)
        self.assertIn("whipsaws", t)
        self.assertIn("not a recommendation", t)

    def test_the_table_has_a_row_per_reason_with_a_minus_sign_on_losses(self):
        t = self.text()
        self.assertIn("hold_stop", t)
        self.assertIn("-25.00", t)

    def test_no_em_dashes_in_what_it_prints(self):
        self.assertNotIn("—", self.text())

    def test_exact_lines_for_the_header_the_table_the_fill_and_the_overshoots(self):
        t = self.text()
        self.assertIn("ARM A STOP-LOSS FILLS vs the documented -7% level", t)
        self.assertIn("stop_loss        2     -8.00    -10.00    -22.00    300.00", t)    # median of -10% and -6%
        self.assertIn("hold_stop        1    -25.00    -25.00    -25.00    100.00", t)
        self.assertIn("median fill vs the price at decision: -1.08%", t)
        self.assertIn("largest overshoots: AAA 2026-09-01->2026-09-10 -10.00%; BBB 2026-09-01->2026-09-10 -6.00%", t)

    def test_a_lot_exactly_at_the_level_is_not_counted_as_past_it(self):
        st = S.study(S.join({"closed": [lot("o1", "AAA", 100.0, 93.0)]}, [sell("o1", 1.0, stop_loss=True)]))
        self.assertEqual(st["worse"], 0)
        self.assertEqual(st["mean_overshoot_pts"], 0.0)

    def test_only_the_five_largest_overshoots_are_listed(self):
        real = {"closed": [lot("o%d" % i, "S%d" % i, 100.0, 90.0 - i) for i in range(7)]}
        log = [sell("o%d" % i, 1.0, stop_loss=True) for i in range(7)]
        self.assertEqual(len(S.study(S.join(real, log))["biggest"]), 5)

    def test_slippage_needs_both_prices_and_a_line_is_skipped_without_them(self):
        st = S.study(S.join({"closed": [lot("o1", "AAA", 100.0, 90.0)]}, [{"side": "sell", "order_id": "o1", "stop_loss": True}]))
        self.assertIsNone(st["median_fill_vs_decision_pct"])
        self.assertNotIn("median fill vs the price at decision", S.report(st))
        st = S.study(S.join({"closed": [lot("o1", "AAA", 100.0, 90.0, qty=0.0)]}, [sell("o1", 91.0, stop_loss=True)]))
        self.assertIsNone(st["median_fill_vs_decision_pct"])

    def test_a_lot_with_no_qty_does_not_break_the_row(self):
        r = S.join({"closed": [{"sale": "o1", "symbol": "AAA", "basis": 100.0, "proceeds": 90.0, "gain": -10.0}]}, [sell("o1", 1.0, stop_loss=True)])[0]
        self.assertIsNone(r["fill"])
        self.assertAlmostEqual(r["ret"], -0.10)


class Main(unittest.TestCase):
    def test_main_reads_the_two_files_from_the_working_directory_and_prints(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "realized_a.json"), "w", encoding="utf-8") as f:
                json.dump(REAL, f)
            with open(os.path.join(d, "trade_log.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r) for r in LOG) + "\n\n")
            out = io.StringIO()
            cwd = os.getcwd()
            try:
                os.chdir(d)
                with contextlib.redirect_stdout(out):
                    rc = S.main([])
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 0)
        self.assertIn("2 stop_loss exits", out.getvalue())

    def test_it_never_writes_anything(self):
        with mock.patch("builtins.open", wraps=open) as op:
            with mock.patch.object(S, "load", return_value=(REAL, LOG)), contextlib.redirect_stdout(io.StringIO()):
                S.main([])
        for call in op.call_args_list:
            mode = (call.args[1:2] or [call.kwargs.get("mode", "r")])[0]
            self.assertNotIn("w", mode)
            self.assertNotIn("a", mode)


if __name__ == "__main__":
    unittest.main()
