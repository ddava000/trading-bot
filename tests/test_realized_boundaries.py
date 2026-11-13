"""realized.py: the behaviours a MUTATION TEST showed nothing pinned (2026-11-04).

253 small mutations of realized.py (a comparison flipped, an operator swapped, a constant moved by one) were run against the realized tests; 78
survived, meaning a tax-relevant behaviour could change without any test noticing. Each test below pins one of those, with an expected value
worked out independently of the code (plain arithmetic or decimal rounding), not read back from it. After these tests 235 of 253 are killed and
the 18 that survive are exactly the equivalent ones listed next (checked against the list, not just the count).

NOT pinned, on purpose, because the mutant is EQUIVALENT (no input can tell it from the original): `>` vs `>=` against EPS on float
quantities, `_ET is not None` (differs only when the timezone database is missing), `len(s) == 10` in trade_date (the other branch parses a
bare date identically), a lot popped at exactly EPS, PRIOR_YEAR_THROUGH's day 31 vs 32 (no month has a 32nd day), and the cent rounding of
sums that are already whole cents (cross_totals, summarize_sales' second pass).

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import realized as R  # noqa: E402


def F(t, sym, side, qty, price, order=None):
    d = {"t": t, "symbol": sym, "side": side, "qty": qty, "price": price}
    if order:
        d["order"] = order
    return d


class WashWatchBoundaries(unittest.TestCase):
    def loss_sale(self):
        return [F("2026-08-01T14:00:00Z", "W", "buy", 10, 10.0), F("2026-08-10T14:00:00Z", "W", "sell", 10, 9.0, "s")]

    def test_a_replacement_30_days_after_is_flagged_and_31_days_after_is_not(self):
        base = self.loss_sale()
        for day, flagged in (("2026-09-09", True), ("2026-09-10", False)):          # 08-10 + 30 days, + 31 days
            fills = base + [F(day + "T14:00:00Z", "W", "buy", 10, 9.0)]
            self.assertEqual(bool(R.wash_watch(R.fifo(fills)["closed"], fills)), flagged, day)

    def test_a_replacement_30_days_before_is_flagged_and_31_days_before_is_not(self):
        closed = R.fifo(self.loss_sale())["closed"]
        for day, flagged in (("2026-07-11", True), ("2026-07-10", False)):          # 08-10 - 30 days, - 31 days
            other = [{"t": day, "symbol": "W", "side": "buy", "qty": 10}]
            self.assertEqual(bool(R.wash_watch(closed, [], other)), flagged, day)

    def test_a_small_gain_lot_and_a_zero_gain_lot_are_not_counted_as_losing_lots(self):
        fills = [F("2026-08-01T14:00:00Z", "M", "buy", 1, 10.0),      # sold at 9.00: -1.00
                 F("2026-08-02T14:00:00Z", "M", "buy", 1, 8.5),       # +0.50
                 F("2026-08-03T14:00:00Z", "M", "buy", 1, 9.0),       # 0.00
                 F("2026-08-10T14:00:00Z", "M", "sell", 3, 9.0, "s"),
                 F("2026-08-20T14:00:00Z", "M", "buy", 1, 9.0)]
        w = R.wash_watch(R.fifo(fills)["closed"], fills)
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["loss"], w[0]["sold_qty"], w[0]["upper_bound"]), (-1.0, 1.0, -1.0))   # one losing share, fully replaced

    def test_a_replacement_larger_than_the_losing_lot_never_exceeds_the_loss(self):
        fills = self.loss_sale() + [F("2026-08-20T14:00:00Z", "W", "buy", 25, 9.0)]
        w = R.wash_watch(R.fifo(fills)["closed"], fills)
        self.assertEqual((w[0]["replacement_qty"], w[0]["upper_bound"]), (25.0, -10.0))

    def test_the_bound_is_in_cents_and_quantities_in_six_places(self):
        # a loss of 1.00 on one share, replaced by a third of a share: 1.00 x 0.333333333 = 0.3333 -> -0.33, replacement 0.333333 (not 0.3333333)
        fills = [F("2026-08-01T14:00:00Z", "T", "buy", 1, 10.0), F("2026-08-10T14:00:00Z", "T", "sell", 1, 9.0, "s"),
                 F("2026-08-20T14:00:00Z", "T", "buy", 0.333333333, 9.0)]
        w = R.wash_watch(R.fifo(fills)["closed"], fills)
        self.assertEqual((w[0]["upper_bound"], w[0]["replacement_qty"]), (-0.33, 0.333333))
        # a sold quantity with seven decimals is shown to six
        fills = [F("2026-08-01T14:00:00Z", "S", "buy", 0.1234567, 10.0), F("2026-08-10T14:00:00Z", "S", "sell", 0.1234567, 9.0, "s"),
                 F("2026-08-20T14:00:00Z", "S", "buy", 1, 9.0)]
        w = R.wash_watch(R.fifo(fills)["closed"], fills)
        self.assertEqual((w[0]["sold_qty"], w[0]["loss"]), (0.123457, -0.12))

    def test_a_loss_that_rounds_to_zero_cents_is_not_watched_and_one_cent_is(self):
        for price, flagged in ((9.996, False), (9.99, True)):                          # -0.004 and -0.01
            fills = [F("2026-08-01T14:00:00Z", "C", "buy", 1, 10.0), F("2026-08-10T14:00:00Z", "C", "sell", 1, price, "s"),
                     F("2026-08-20T14:00:00Z", "C", "buy", 1, 9.0)]
            self.assertEqual(bool(R.wash_watch(R.fifo(fills)["closed"], fills)), flagged, price)


class FifoPrecision(unittest.TestCase):
    Q, BUY, SELL = 0.1234567891234, 10.1234567, 11.7654327       # worked out with decimal arithmetic: see the expected values below

    def test_a_closed_lot_is_rounded_to_six_places_and_its_quantity_to_nine(self):
        fills = [F("2026-08-01T14:00:00Z", "P", "buy", self.Q, self.BUY), F("2026-08-10T14:00:00Z", "P", "sell", self.Q, self.SELL)]
        lot = R.fifo(fills)["closed"][0]
        self.assertEqual((lot["qty"], lot["basis"], lot["proceeds"], lot["gain"]), (0.123456789, 1.249809, 1.452523, 0.202713))

    def test_an_open_position_is_rounded_the_same_way(self):
        res = R.fifo([F("2026-08-01T14:00:00Z", "P", "buy", self.Q, self.BUY)])
        self.assertEqual((res["open"]["P"], res["open_basis"]["P"]), (0.123456789, 1.249809))

    def test_a_sell_beyond_what_was_bought_is_unmatched_by_exactly_the_excess(self):
        res = R.fifo([F("2026-08-01T14:00:00Z", "U", "buy", 1, 10.0), F("2026-08-10T14:00:00Z", "U", "sell", 1.5, 11.0)])
        self.assertEqual([(u["symbol"], u["qty"]) for u in res["unmatched"]], [("U", 0.5)])
        self.assertEqual(res["closed"][0]["qty"], 1.0)

    def test_a_zero_quantity_buy_is_ignored_and_never_becomes_a_phantom_lot(self):
        fills = [F("2026-08-01T14:00:00Z", "Z", "buy", 0, 10.0), F("2026-08-02T14:00:00Z", "Z", "buy", 1, 10.0),
                 F("2026-08-10T14:00:00Z", "Z", "sell", 1, 11.0)]
        res = R.fifo(fills)
        self.assertEqual([(l["qty"], l["gain"]) for l in res["closed"]], [(1.0, 1.0)])     # one lot, not a zero-quantity one first

    def test_an_unmatched_quantity_is_rounded_to_nine_places(self):
        res = R.fifo([F("2026-08-01T14:00:00Z", "U", "buy", 1, 10.0), F("2026-08-10T14:00:00Z", "U", "sell", 1.1234567891234, 11.0)])
        self.assertEqual(res["unmatched"][0]["qty"], 0.123456789)
        res = R.fifo([F("2026-08-10T14:00:00Z", "V", "sell_short", self.Q, 11.0)])
        self.assertEqual((res["unmatched"][0]["qty"], res["unmatched"][0]["side"]), (0.123456789, "sell_short"))


class SummaryArithmetic(unittest.TestCase):
    def test_short_and_long_term_add_into_one_net_and_one_sales_count(self):
        s = R.summarize_sales([("ST", 10.0), ("LT", 5.0)])
        self.assertEqual((s["net"], s["sales"]), (15.0, 2))
        self.assertEqual(s["st"], {"gains": 10.0, "losses": 0.0, "net": 10.0, "sales": 1})
        self.assertEqual(s["lt"], {"gains": 5.0, "losses": 0.0, "net": 5.0, "sales": 1})

    def test_a_small_gain_is_a_gain_and_a_small_loss_is_a_loss(self):
        s = R.summarize_sales([("ST", 0.5), ("ST", -0.5)])
        self.assertEqual((s["st"]["gains"], s["st"]["losses"], s["st"]["net"], s["st"]["sales"]), (0.5, -0.5, 0.0, 2))

    def test_each_sale_is_rounded_to_cents_before_it_is_added(self):
        """Like a broker's rows: three sales of +0.004 are three rows of +0.00, not one of +0.01."""
        s = R.summarize_sales([("ST", 0.004)] * 3)
        self.assertEqual((s["st"]["gains"], s["st"]["net"], s["st"]["sales"]), (0.0, 0.0, 3))

    def test_combine_adds_every_field_and_a_missing_one_counts_as_zero(self):
        a = {"st": {"gains": 2.0, "losses": -1.0, "net": 1.0, "sales": 2}, "lt": {"gains": 3.0, "losses": 0.0, "net": 3.0, "sales": 1}}
        b = {"st": {"gains": 1.0, "net": 1.0}}                      # no losses, no sales, no lt block at all
        t = R.combine(a, b)
        self.assertEqual(t["st"], {"gains": 3.0, "losses": -1.0, "net": 2.0, "sales": 2})
        self.assertEqual(t["lt"], {"gains": 3.0, "losses": 0.0, "net": 3.0, "sales": 1})
        self.assertEqual((t["net"], t["sales"]), (5.0, 3))

    def test_combining_nothing_is_exactly_zero(self):
        z = {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0}
        self.assertEqual(R.combine(), {"st": z, "lt": z, "net": 0.0, "sales": 0})

    def test_combine_rounds_to_cents(self):
        t = R.combine({"st": {"gains": 0.004, "net": 0.004}}, {"st": {"gains": 0.004, "net": 0.004}})
        self.assertEqual((t["st"]["gains"], t["st"]["net"], t["net"]), (0.01, 0.01, 0.01))

    def test_wash_totals_of_nothing_is_zero_and_counts_and_rounds_per_scope(self):
        self.assertEqual(R.wash_totals([]), {"same_account": {"sales": 0, "upper_bound": 0.0}, "cross_account": {"sales": 0, "upper_bound": 0.0}})
        w = [{"scope": "same-account", "upper_bound": -0.006}, {"scope": "same-account", "upper_bound": -0.006},
             {"scope": "cross-account", "upper_bound": -1.5}]
        t = R.wash_totals(w)
        self.assertEqual(t["same_account"], {"sales": 2, "upper_bound": -0.02})      # rounded to cents at each step, as the code states
        self.assertEqual(t["cross_account"], {"sales": 1, "upper_bound": -1.5})


class PositionVerification(unittest.TestCase):
    def test_a_symbol_known_to_only_one_side_is_compared_with_zero(self):
        self.assertEqual(R.verify_positions({"A": 1.0}, {}), [{"symbol": "A", "from_fills": 1.0, "broker": 0.0}])
        self.assertEqual(R.verify_positions({}, {"B": 2.0}), [{"symbol": "B", "from_fills": 0.0, "broker": 2.0}])

    def test_the_tolerance_is_relative_to_the_broker_quantity(self):
        self.assertEqual(R.verify_positions({"X": 1000000.0}, {"X": 1000000.5}), [])         # 0.5 of a million is inside 1e-6 relative
        self.assertEqual(len(R.verify_positions({"X": 1.0}, {"X": 1.001})), 1)                # 0.001 of one share is not
        self.assertEqual(R.verify_positions({"X": 1.0}, {"X": 1.0 + 1e-9}), [])               # float dust is

    def test_a_mismatch_is_reported_to_six_places(self):
        self.assertEqual(R.verify_positions({"X": 1.2345678}, {"X": 2.2345678}),
                         [{"symbol": "X", "from_fills": 1.234568, "broker": 2.234568}])


class ArmBHoldings(unittest.TestCase):
    """arm_b_block's second tripwire: a published holding BELOW what the ledger says is held can only be a sale the ledger does not know."""

    def block(self, held, positions):
        d = tempfile.mkdtemp()
        p = lambda n: os.path.join(d, n)
        json.dump({"summary": {"state": "ok", "rh_log_sells_counted": 0}, "open": held}, open(p("l.json"), "w"))
        json.dump({"positions": positions}, open(p("s.json"), "w"))
        open(p("log.jsonl"), "w").close()
        return R.arm_b_block(p("l.json"), p("log.jsonl"), p("s.json"))

    def test_a_held_symbol_missing_from_the_published_positions_counts_as_zero(self):
        b = self.block({"SPY": 1.0}, {"QQQ": 5.0})
        self.assertEqual(b["state"], "stale")
        self.assertIn("SPY 0.000000 now vs 1.000000 in the ledger", b["reason"])

    def test_equal_holdings_are_not_a_sale_not_even_with_float_dust(self):
        for now in (1.0, 1.0 - 1e-9):
            self.assertEqual(self.block({"SPY": 1.0}, {"SPY": now})["state"], "ok", now)

    def test_the_tolerance_scales_with_the_quantity(self):
        self.assertEqual(self.block({"X": 0.001}, {"X": 0.0005})["state"], "stale")         # half of a tiny holding is a sale
        self.assertEqual(self.block({"X": 1000000.0}, {"X": 999999.5})["state"], "ok")       # half a share of a million is dust

    def test_a_shortfall_of_exactly_the_tolerance_is_not_a_sale(self):
        tol_floor = 1.0 - 1e-6 - 1e-6 * abs(1.0)                 # the smallest holding that still counts as "not below the ledger"
        self.assertEqual(self.block({"SPY": 1.0}, {"SPY": tol_floor})["state"], "ok")
        self.assertEqual(self.block({"SPY": 1.0}, {"SPY": tol_floor - 1e-9})["state"], "stale")

    def test_only_the_first_three_short_symbols_are_named(self):
        held = {s: 1.0 for s in ("A", "B", "C", "D")}
        b = self.block(held, {"A": 0.5, "B": 0.5, "C": 0.5, "D": 0.5})
        self.assertEqual(b["reason"].count(" now vs "), 3)


class NewYorkClock(unittest.TestCase):
    """tax_year and _today_et exist because a runner is on UTC: at 00:30 on January 1 in New York it is still December 31 locally here (Central)
    and in UTC it is already tomorrow. A fake clock that gives DIFFERENT answers for the New York zone and for the local one pins which is used."""

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 1, 0, 30, tzinfo=tz) if tz is not None else datetime(2026, 12, 31, 23, 30)

    def test_the_tax_year_and_the_date_come_from_the_new_york_clock(self):
        with mock.patch.object(R, "datetime", self.FakeDT):
            self.assertEqual(R.tax_year(), 2027)
            self.assertEqual(R._today_et(), date(2027, 1, 1))

    def test_without_a_timezone_database_the_tax_year_falls_back_to_the_local_clock(self):
        with mock.patch.object(R, "datetime", self.FakeDT), mock.patch.object(R, "_ET", None):
            self.assertEqual(R.tax_year(), 2026)


class ReportFallbacks(unittest.TestCase):
    BLOCK = {"state": "ok", "by_year": {"2026": {"st": {"gains": 10.0, "losses": -4.0, "net": 6.0, "sales": 3},
                                                  "lt": {"gains": 5.0, "losses": 0.0, "net": 5.0, "sales": 1}}}}

    def test_the_line_for_a_year_prints_the_blocks_own_numbers(self):
        self.assertEqual(R.report_line("Arm A", self.BLOCK, 2026),
                         "Arm A 2026 realized, short-term: gains $10.00, losses -$4.00, net $6.00 (3 sales); long-term net $5.00")

    def test_a_year_with_no_sales_prints_zero_and_no_long_term_part(self):
        self.assertEqual(R.report_line("Arm A", self.BLOCK, 2027), "Arm A 2027 realized, short-term: gains $0.00, losses $0.00, net $0.00 (0 sales)")

    def test_a_year_block_with_no_sales_count_prints_zero_sales(self):
        blk = {"state": "ok", "by_year": {"2026": {"st": {"gains": 1.0, "losses": 0.0, "net": 1.0}, "lt": {}}}}
        self.assertIn("(0 sales)", R.report_line("Arm A", blk, 2026))

    def test_no_wash_information_prints_no_wash_flag(self):
        self.assertNotIn("wash", R.report_line("Arm A", self.BLOCK, 2026))
        self.assertNotIn("wash", R.report_line("Arm A", dict(self.BLOCK, wash_watch={}), 2026))

    def test_with_no_status_block_arm_a_falls_back_to_its_committed_ledger(self):
        d = tempfile.mkdtemp()
        p = lambda n: os.path.join(d, n)
        json.dump({}, open(p("status.json"), "w"))                                              # no "realized" key
        json.dump({"summary": self.BLOCK, "closed": [], "buys": [], "open": {}}, open(p("a.json"), "w"))
        text = "\n".join(R.repo_report_lines(year=2026, status_path=p("status.json"), a_ledger=p("a.json"), b_ledger=p("none.json"),
                                             log_path=p("none.jsonl"), today=date(2026, 11, 4)))
        self.assertIn("Arm A (Alpaca) 2026 realized, short-term: gains $10.00", text)
        self.assertNotIn("Arm A (Alpaca): realized total NOT AVAILABLE", text)

    def test_cross_totals_needs_both_ledgers(self):
        led = {"closed": [], "buys": []}
        self.assertIsNone(R.cross_totals(None, led))
        self.assertIsNone(R.cross_totals(led, None))
        self.assertIsNotNone(R.cross_totals(led, led))


if __name__ == "__main__":
    unittest.main()
