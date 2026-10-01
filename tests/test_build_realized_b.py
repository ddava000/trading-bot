"""build_realized_b.py: the builder behind Arm B's realized figure, which had no tests.

Arm B's number is the BROKER's own per-sale realized gain, cross-checked by an independent FIFO
over the order history. These cases pin that cross-check: it must say ok when the two agree, and
unverified (never a quietly wrong figure) when they do not. Each guard is shown able to fail.

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_realized_b as B  # noqa: E402

NO_LOG = os.path.join(tempfile.gettempdir(), "no_such_rh_trade_log_for_tests.jsonl")


def order(oid, sym, side, qty, price, ts, state="filled", agent=None):
    """An order shaped like get_equity_orders: strings for numbers, executions as a list."""
    ex = [{"timestamp": ts, "quantity": str(qty), "price": str(price)}] if qty else []
    return {"id": oid, "symbol": sym, "side": side, "state": state, "executions": ex,
            "placed_agent": agent}


def row(sym, qty, gain, ts):
    return {"symbol": sym, "quantity": str(qty), "realized_gain": str(gain), "timestamp": ts}


BUY = order("b1", "SPY", "buy", 1, 100.0, "2026-08-24T14:00:00Z")
SELL = order("s1", "SPY", "sell", 1, 90.0, "2026-09-10T14:00:00Z")
ROW = row("SPY", 1, "-10.00", "2026-09-10T14:00:01Z")


class AgreementTests(unittest.TestCase):
    def test_matching_broker_and_fifo_is_ok_and_publishes_the_brokers_figure(self):
        led = B.build([BUY, SELL], [ROW], None, NO_LOG)
        s = led["summary"]
        self.assertEqual(s["state"], "ok")
        self.assertEqual(s["st"]["net"], -10.0)
        self.assertEqual(s["st"]["sales"], 1)
        self.assertEqual(s["verification"]["sales_matched"], 1)
        self.assertEqual(led["closed"][0]["symbol"], "SPY")

    def test_the_ledger_carries_the_disclaimer_and_the_buys_for_the_cross_account_watch(self):
        led = B.build([BUY, SELL], [ROW], None, NO_LOG)
        self.assertIn("NOT a tax document", led["note"])
        self.assertEqual([(b["symbol"], b["qty"]) for b in led["buys"]], [("SPY", 1.0)])

    def test_a_one_cent_rounding_difference_is_still_ok(self):
        led = B.build([BUY, SELL], [row("SPY", 1, "-10.01", "2026-09-10T14:00:01Z")], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "ok")
        self.assertEqual(led["summary"]["st"]["net"], -10.01)        # the BROKER's figure wins

    def test_a_larger_difference_is_unverified_not_silently_published(self):
        led = B.build([BUY, SELL], [row("SPY", 1, "-9.50", "2026-09-10T14:00:01Z")], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "unverified")
        self.assertIn("differs from the broker", led["summary"]["reason"])


class WhatCountsAsASaleTests(unittest.TestCase):
    def test_a_sell_cancelled_after_a_partial_fill_still_sold_shares(self):
        """The widening: state is not what makes an order real, executions are."""
        partial = order("s2", "SPY", "sell", 0.4, 90.0, "2026-09-10T14:00:00Z", state="cancelled")
        led = B.build([BUY, partial], [row("SPY", 0.4, "-4.00", "2026-09-10T14:00:01Z")], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "ok")
        self.assertEqual(len(led["closed"]), 1)
        self.assertAlmostEqual(led["closed"][0]["qty"], 0.4)
        self.assertAlmostEqual(led["open"]["SPY"], 0.6)

    def test_an_order_that_never_traded_is_ignored(self):
        """Positive control for the above: no executions means no lots, whatever the state."""
        never = order("s3", "SPY", "sell", 0, 0, "2026-09-10T14:00:00Z", state="cancelled")
        led = B.build([BUY, never], [], None, NO_LOG)
        self.assertEqual(led["closed"], [])
        self.assertAlmostEqual(led["open"]["SPY"], 1.0)
        self.assertEqual(led["summary"]["state"], "ok")

    def test_a_broker_row_with_no_matching_sale_is_unverified(self):
        led = B.build([BUY], [ROW], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "unverified")
        self.assertIn("broker row with no FIFO sale", led["summary"]["reason"])

    def test_a_sale_with_no_broker_row_is_unverified(self):
        led = B.build([BUY, SELL], [], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "unverified")
        self.assertIn("no broker row for SPY", led["summary"]["reason"])

    def test_a_sell_with_no_buy_behind_it_is_unverified(self):
        led = B.build([SELL], [ROW], None, NO_LOG)
        self.assertEqual(led["summary"]["state"], "unverified")


class PositionCheckTests(unittest.TestCase):
    def test_holdings_the_fills_imply_must_match_what_the_daemon_publishes(self):
        buy2 = order("b2", "QQQ", "buy", 2, 50.0, "2026-08-25T14:00:00Z")
        ok = B.build([BUY, buy2], [], {"positions": {"SPY": 1.0, "QQQ": 2.0}}, NO_LOG)
        self.assertEqual(ok["summary"]["state"], "ok")
        bad = B.build([BUY, buy2], [], {"positions": {"SPY": 1.0, "QQQ": 1.5}}, NO_LOG)
        self.assertEqual(bad["summary"]["state"], "unverified")
        self.assertIn("FIFO holdings differ", bad["summary"]["reason"])


class TaxYearTests(unittest.TestCase):
    def test_sales_land_in_the_year_of_the_brokers_new_york_trade_date(self):
        dec = order("s4", "SPY", "sell", 0.5, 90.0, "2026-12-31T20:00:00Z")      # 3pm ET, Dec 31
        jan = order("s5", "SPY", "sell", 0.5, 90.0, "2027-01-04T15:00:00Z")
        rows = [row("SPY", 0.5, "-5.00", "2026-12-31T20:00:01Z"), row("SPY", 0.5, "-5.00", "2027-01-04T15:00:01Z")]
        led = B.build([BUY, dec, jan], rows, None, NO_LOG)
        by = led["summary"]["by_year"]
        self.assertEqual(sorted(by), ["2026", "2027"])
        self.assertEqual(by["2026"]["st"]["net"], -5.0)
        self.assertEqual(by["2027"]["st"]["net"], -5.0)
        self.assertEqual(led["summary"]["st"]["net"], -10.0)


class LogTripwireInputTests(unittest.TestCase):
    def test_the_count_of_sells_the_daemon_logged_is_recorded_for_the_staleness_check(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rh_trade_log.jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                for r in ({"action": "sell", "status": "ok"}, {"action": "buy", "status": "ok"},
                          {"action": "sell", "status": "rejected"}, {"action": "sell", "status": "unknown"}):
                    fh.write(json.dumps(r) + "\n")
            led = B.build([BUY, SELL], [ROW], None, p)
        self.assertEqual(led["summary"]["rh_log_sells_counted"], 2)     # rejected is the only exclusion


if __name__ == "__main__":
    unittest.main()
