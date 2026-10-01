"""Regression tests for realized.py, the module behind Devon's running tax number.

Every case here exists because either it caught a real bug or it protects a number that goes
to a tax preparer. Several were built to FAIL: a check that has only ever said "fine" proves
nothing (this repo has been fooled by that repeatedly).

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


class FifoTests(unittest.TestCase):
    def test_two_lots_closed_by_one_sale_net_to_one_gain(self):
        fills = [F("2026-08-12T14:00:00Z", "A", "buy", 10, 10.0),
                 F("2026-08-13T14:00:00Z", "A", "buy", 10, 12.0),
                 F("2026-08-20T14:00:00Z", "A", "sell", 15, 11.0)]   # +10 on lot1, -5 on lot2
        res = R.fifo(fills)
        s = R.summarize(res["closed"])
        self.assertEqual(len(res["closed"]), 2)
        self.assertEqual((s["st"]["gains"], s["st"]["losses"], s["st"]["sales"]), (5.0, 0.0, 1))
        self.assertEqual(res["open"]["A"], 5.0)
        self.assertEqual(res["open_basis"]["A"], 60.0)

    def test_sell_with_no_buy_is_unmatched_never_ignored(self):
        res = R.fifo([F("2026-08-12T14:00:00Z", "B", "sell", 3, 10.0)])
        self.assertEqual(len(res["unmatched"]), 1)

    def test_short_sale_side_is_unmatched(self):
        res = R.fifo([F("2026-08-12T14:00:00Z", "B", "sell_short", 3, 10.0)])
        self.assertEqual(len(res["unmatched"]), 1)

    def test_order_executed_in_two_pieces_is_ONE_sale(self):
        """Robinhood's NOK sale: 1.0 + 0.065359 shares. Counting each execution as a sale
        split one -3.17 loss into -2.97 and -0.19 and disagreed with the broker's own row."""
        fills = [F("2026-06-02T15:41:00Z", "NOK", "buy", 1.0, 16.8957, "b1"),
                 F("2026-06-02T15:41:01Z", "NOK", "buy", 0.065359, 16.8957, "b1"),
                 F("2026-06-09T15:20:00Z", "NOK", "sell", 1.0, 13.9245, "s1"),
                 F("2026-06-09T15:20:01Z", "NOK", "sell", 0.065359, 13.9245, "s1")]
        s = R.summarize(R.fifo(fills)["closed"])
        self.assertEqual(s["st"]["sales"], 1)
        self.assertAlmostEqual(s["st"]["net"], -3.17, places=2)

    def test_gain_half_and_loss_half_of_one_order_net_together(self):
        fills = [F("2026-08-01T14:00:00Z", "X", "buy", 1, 10.0, "a"),
                 F("2026-08-02T14:00:00Z", "X", "buy", 1, 20.0, "b"),
                 F("2026-08-10T14:00:00Z", "X", "sell", 1, 15.0, "s"),
                 F("2026-08-10T14:00:01Z", "X", "sell", 1, 15.0, "s")]
        s = R.summarize(R.fifo(fills)["closed"])
        self.assertEqual((s["st"]["gains"], s["st"]["losses"], s["st"]["sales"]), (0.0, 0.0, 1))

    def test_without_order_ids_each_fill_is_its_own_sale(self):
        fills = [F("2026-08-01T14:00:00Z", "X", "buy", 1, 10.0),
                 F("2026-08-02T14:00:00Z", "X", "buy", 1, 20.0),
                 F("2026-08-10T14:00:00Z", "X", "sell", 1, 15.0),
                 F("2026-08-10T14:00:01Z", "X", "sell", 1, 15.0)]
        s = R.summarize(R.fifo(fills)["closed"])
        self.assertEqual((s["st"]["gains"], s["st"]["losses"], s["st"]["sales"]), (5.0, -5.0, 2))

    def test_accounting_identity_holds_on_a_toy_account(self):
        """cash + market value - contributions == realized + (market value - open cost).
        The bot relies on this to prove its own lot math against broker numbers."""
        fills = [F("2026-08-12T14:00:00Z", "A", "buy", 10, 10.0),
                 F("2026-08-13T14:00:00Z", "A", "sell", 4, 12.0)]
        res = R.fifo(fills)
        cash, mv, contrib = 200 - 100 + 48, 6 * 11, 200
        realized = sum(l["gain"] for l in res["closed"])
        self.assertAlmostEqual(cash + mv - contrib, realized + (mv - sum(res["open_basis"].values())), places=9)


class TermAndDateTests(unittest.TestCase):
    def test_anniversary_itself_is_still_short_term(self):
        self.assertEqual(R.term(date(2025, 8, 12), date(2026, 8, 12)), "ST")
        self.assertEqual(R.term(date(2025, 8, 12), date(2026, 8, 13)), "LT")

    def test_feb_29_purchase(self):
        self.assertEqual(R.term(date(2028, 2, 29), date(2029, 3, 1)), "ST")
        self.assertEqual(R.term(date(2028, 2, 29), date(2029, 3, 2)), "LT")

    def test_nanosecond_timestamps_parse(self):
        self.assertEqual(str(R.trade_date("2026-08-24T13:45:12.123456789Z")), "2026-08-24")

    def test_evening_trade_belongs_to_the_previous_new_york_day(self):
        self.assertEqual(str(R.trade_date("2026-08-25T02:00:00Z")), "2026-08-24")

    def test_refuses_to_guess_the_date_without_a_timezone_database(self):
        """A trade at 8pm New York is already tomorrow in UTC. Guessing could move it across
        a tax-year boundary, so it must raise (the block then reports UNKNOWN)."""
        with mock.patch.object(R, "_ET", None):
            with self.assertRaises(RuntimeError):
                R.trade_date("2026-08-25T02:00:00Z")
            self.assertEqual(str(R.trade_date("2026-08-24")), "2026-08-24")  # bare ET date is fine

    def test_tax_year_uses_the_new_york_clock(self):
        from zoneinfo import ZoneInfo
        self.assertEqual(R.tax_year(), datetime.now(ZoneInfo("America/New_York")).year)


class WashWatchTests(unittest.TestCase):
    def test_replacement_within_30_days_is_flagged_beyond_is_not(self):
        fills = [F("2026-08-01T14:00:00Z", "W", "buy", 10, 10.0),
                 F("2026-08-10T14:00:00Z", "W", "sell", 10, 9.0),     # -10
                 F("2026-08-20T14:00:00Z", "W", "buy", 10, 9.0),      # 10 days later
                 F("2026-08-01T14:00:00Z", "Z", "buy", 10, 10.0),
                 F("2026-08-10T14:00:00Z", "Z", "sell", 10, 9.0),     # -10
                 F("2026-10-01T14:00:00Z", "Z", "buy", 10, 9.0)]      # 52 days later
        w = R.wash_watch(R.fifo(fills)["closed"], fills)
        self.assertEqual([x["symbol"] for x in w], ["W"])
        self.assertEqual(w[0]["upper_bound"], -10.0)

    def test_the_sold_lots_own_purchase_is_not_its_replacement(self):
        fills = [F("2026-08-05T14:00:00Z", "Q", "buy", 10, 10.0),
                 F("2026-08-10T14:00:00Z", "Q", "sell", 10, 9.0)]
        self.assertEqual(R.wash_watch(R.fifo(fills)["closed"], fills), [])

    def test_a_gain_is_never_flagged(self):
        fills = [F("2026-08-05T14:00:00Z", "G", "buy", 10, 10.0),
                 F("2026-08-10T14:00:00Z", "G", "sell", 10, 11.0),
                 F("2026-08-12T14:00:00Z", "G", "buy", 10, 11.0)]
        self.assertEqual(R.wash_watch(R.fifo(fills)["closed"], fills), [])

    def test_cross_account_replacement_partially_covers_the_loss(self):
        x = {"closed": R.fifo([F("2026-08-01T14:00:00Z", "MU", "buy", 1, 10.0, "b"),
                               F("2026-08-24T14:00:00Z", "MU", "sell", 1, 9.0, "s")])["closed"]}
        y = {"buys": [{"t": "2026-09-10", "symbol": "MU", "qty": 0.5}]}
        w = R.cross_watch(x, y)
        self.assertEqual((w[0]["scope"], w[0]["upper_bound"]), ("cross-account", -0.5))
        self.assertEqual(R.cross_watch(x, {"buys": [{"t": "2026-11-10", "symbol": "MU", "qty": 1.0}]}), [])


class SummaryAndReportTests(unittest.TestCase):
    def test_tax_years_are_separate(self):
        fills = [F("2026-12-30T14:00:00Z", "A", "buy", 2, 10.0, "b"),
                 F("2026-12-31T14:00:00Z", "A", "sell", 1, 12.0, "s1"),
                 F("2027-01-04T14:00:00Z", "A", "sell", 1, 9.0, "s2")]
        by = R.summarize_by_year(R.fifo(fills)["closed"])
        self.assertEqual((by["2026"]["st"]["gains"], by["2026"]["st"]["losses"]), (2.0, 0.0))
        self.assertEqual((by["2027"]["st"]["gains"], by["2027"]["st"]["losses"]), (0.0, -1.0))
        blk = {"state": "ok", "by_year": by}
        self.assertIn("gains $2.00", R.report_line("A", blk, 2026))
        line27 = R.report_line("A", blk, 2027)
        self.assertIn("losses -$1.00", line27)
        self.assertNotIn("$2.00", line27)
        self.assertIn("(0 sales)", R.report_line("A", blk, 2028))       # no sales: zero, not an error

    def test_a_missing_or_failed_number_is_never_rendered_as_zero(self):
        self.assertIn("NOT AVAILABLE", R.report_line("A", None))
        self.assertIn("UNKNOWN", R.report_line("A", {"state": "unknown", "reason": "api down"}))
        self.assertIn("api down", R.report_line("A", {"state": "unknown", "reason": "api down"}))

    def test_stale_and_unverified_are_labelled_on_the_line(self):
        by = {"2026": {"st": {"gains": 1.0, "losses": -2.0, "net": -1.0, "sales": 3}, "lt": {"sales": 0}}}
        self.assertIn("STALE", R.report_line("A", {"state": "stale", "by_year": by, "as_of": "x"}, 2026))
        self.assertIn("UNVERIFIED", R.report_line("A", {"state": "unverified", "by_year": by, "reason": "r"}, 2026))

    def test_combined_line_refuses_to_add_an_unknown_arm(self):
        z = lambda g, l, n, s: {"st": {"gains": g, "losses": l, "net": n, "sales": s},
                                "lt": {"gains": 0, "losses": 0, "net": 0, "sales": 0}}
        a = {"state": "ok", "by_year": {"2026": z(0.11, -12.79, -12.68, 25)}}
        b = {"state": "ok", "by_year": {"2026": z(7.20, -45.18, -37.98, 50)}}
        self.assertIn("net -$50.66", R.combined_line(a, b, 2026))
        self.assertIn("NOT AVAILABLE", R.combined_line(a, None, 2026))
        self.assertIn("component not confirmed: B=stale", R.combined_line(a, dict(b, state="stale"), 2026))


class VerifyPositionsTests(unittest.TestCase):
    def test_matching_passes_and_missing_or_extra_shares_are_caught(self):
        self.assertEqual(R.verify_positions({"A": 5.0}, {"A": 5.0}), [])
        self.assertEqual(len(R.verify_positions({"A": 5.0}, {"A": 6.0})), 1)
        self.assertEqual(len(R.verify_positions({}, {"B": 1.0})), 1)     # broker holds what fills never bought


class ArmBTripwireTests(unittest.TestCase):
    """The Arm B ledger comes from the broker and only the laptop can refresh it, so it can go
    stale silently. Two independent tripwires, each added after a real gap was found."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.log = os.path.join(self.d, "log.jsonl")
        self.led = os.path.join(self.d, "realized_b.json")
        self.st = os.path.join(self.d, "rh_status.json")
        self._write(self.led, {"summary": {"state": "ok", "rh_log_sells_counted": 2},
                               "open": {"SPY": 1.0, "QQQ": 2.0}})
        self._write(self.st, {"positions": {"SPY": 1.5, "QQQ": 2.0}})    # buys only ever raise holdings
        self._log(self._sells(2))

    @staticmethod
    def _write(path, obj):
        with open(path, "w") as f:
            json.dump(obj, f)

    def _log(self, rows):
        with open(self.log, "w") as f:
            f.write("\n".join(json.dumps(x) for x in rows))

    @staticmethod
    def _sells(n, status="ok"):
        return [{"action": "sell", "status": status} for _ in range(n)]

    def block(self):
        return R.arm_b_block(self.led, self.log, self.st)

    def test_baseline_is_ok(self):
        self.assertEqual(self.block()["state"], "ok")

    def test_more_sells_logged_than_counted_is_stale(self):
        self._log(self._sells(3))
        b = self.block()
        self.assertEqual(b["state"], "stale")
        self.assertIn("1 sell(s) logged since", b["reason"])

    def test_FEWER_sells_logged_than_counted_is_stale(self):
        """The first version tested only 'more', so a rewritten log kept it silent."""
        self._log(self._sells(1))
        b = self.block()
        self.assertEqual(b["state"], "stale")
        self.assertIn("FEWER", b["reason"])

    def test_a_sale_made_by_hand_is_caught_by_the_holdings_tripwire(self):
        """It never reaches rh_trade_log.jsonl, but it lowers the holdings the daemon publishes."""
        self._write(self.st, {"positions": {"SPY": 0.4, "QQQ": 2.0}})
        b = self.block()
        self.assertEqual(b["state"], "stale")
        self.assertIn("by hand", b["reason"])
        self.assertIn("SPY", b["reason"])

    def test_a_vanished_symbol_is_caught(self):
        self._write(self.st, {"positions": {"QQQ": 2.0}})
        self.assertEqual(self.block()["state"], "stale")

    def test_large_buys_never_trip_it(self):
        """Weekly deposits and dividend reinvestment raise holdings; that is not a sale."""
        self._write(self.st, {"positions": {"SPY": 5.0, "QQQ": 9.0}})
        self.assertEqual(self.block()["state"], "ok")

    def test_degraded_and_empty_snapshots_prove_nothing(self):
        self._write(self.st, {"degraded": "broker_unreachable", "positions": {"SPY": 0.0}})
        self.assertEqual(self.block()["state"], "ok")
        self._write(self.st, {"positions": {}})
        self.assertEqual(self.block()["state"], "ok")

    def test_missing_status_file_leaves_the_log_tripwire_working(self):
        os.remove(self.st)
        self.assertEqual(self.block()["state"], "ok")
        self._log(self._sells(3))
        self.assertEqual(self.block()["state"], "stale")

    def test_an_unconfirmed_sale_counts_but_a_rejected_one_does_not(self):
        """The daemon's statuses are ok, rejected and unknown (bridge could not confirm). An
        'unknown' sell may really have filled, so missing it would leave a wrong tax number."""
        self._log(self._sells(2) + self._sells(1, "unknown"))
        self.assertEqual(self.block()["state"], "stale")
        self._log(self._sells(2) + self._sells(1, "rejected"))
        self.assertEqual(self.block()["state"], "ok")
        self.assertEqual(R.rh_sells_logged(self.log), 2)

    def test_buys_in_the_log_are_never_counted(self):
        self._log(self._sells(2) + [{"action": "buy", "status": "ok"}, {"action": "buy", "status": "unknown"}])
        self.assertEqual(R.rh_sells_logged(self.log), 2)

    def test_an_unverified_ledger_stays_unverified_and_gains_the_reason(self):
        self._write(self.led, {"summary": {"state": "unverified", "reason": "positions differ",
                                           "rh_log_sells_counted": 2}, "open": {}})
        self._log(self._sells(3))
        b = self.block()
        self.assertEqual(b["state"], "unverified")
        self.assertIn("positions differ", b["reason"])
        self.assertIn("logged", b["reason"])

    def test_missing_ledger_is_none_never_a_zero(self):
        self.assertIsNone(R.arm_b_block(os.path.join(self.d, "nope.json"), self.log, self.st))


class ReaderTests(unittest.TestCase):
    def test_reader_prints_every_arm_and_flags_cross_account_overlap(self):
        d = tempfile.mkdtemp()
        p = lambda n: os.path.join(d, n)
        z = lambda g, l, n, s: {"st": {"gains": g, "losses": l, "net": n, "sales": s},
                                "lt": {"gains": 0, "losses": 0, "net": 0, "sales": 0}}
        lot = R.fifo([F("2026-08-01T14:00:00Z", "IWM", "buy", 1, 10.0, "b"),
                      F("2026-08-24T14:00:00Z", "IWM", "sell", 1, 9.0, "s")])["closed"]
        def dump(name, obj):
            with open(p(name), "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
        dump("realized_a.json", {"summary": {"state": "ok", "by_year": {"2026": z(0.0, -1.0, -1.0, 1)}},
                                 "closed": lot, "buys": [], "open": {}})
        dump("realized_b.json", {"summary": {"state": "ok", "rh_log_sells_counted": 0,
                                             "by_year": {"2026": z(2.0, -3.0, -1.0, 4)}},
                                 "closed": [], "buys": [{"t": "2026-09-10", "symbol": "IWM", "qty": 1.0}],
                                 "open": {}})
        with open(p("rh_trade_log.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("")
        dump("status.json", {"realized": {"state": "ok", "by_year": {"2026": z(0.0, -1.0, -1.0, 1)}}})
        lines = R.repo_report_lines(year=2026, status_path=p("status.json"), a_ledger=p("realized_a.json"),
                                    b_ledger=p("realized_b.json"), log_path=p("rh_trade_log.jsonl"))
        text = "\n".join(lines)
        self.assertIn("Arm A (Alpaca) 2026 realized", text)
        self.assertIn("Arm B (Robinhood) 2026 realized", text)
        self.assertIn("BOTH ARMS 2026", text)
        self.assertIn("Cross-account wash-sale watch", text)      # Arm B bought IWM within 30 days of A's loss
        self.assertIn("Not a tax document", text)

    def test_reader_never_shows_a_missing_arm_as_zero(self):
        d = tempfile.mkdtemp()
        text = "\n".join(R.repo_report_lines(year=2026, status_path=os.path.join(d, "s.json"),
                                              a_ledger=os.path.join(d, "a.json"), b_ledger=os.path.join(d, "b.json"),
                                              log_path=os.path.join(d, "l.jsonl")))
        self.assertIn("NOT AVAILABLE", text)
        self.assertNotIn("$0.00", text)


class YearBoundaryTests(unittest.TestCase):
    """January 1 is when the running total restarts and when a December loss can meet a
    January repurchase. None of this can be observed live for three months, so it is pinned now."""

    DEC_LOSS = [F("2026-12-01T15:00:00Z", "IWM", "buy", 10, 10.0, "b1"),
                F("2026-12-28T15:00:00Z", "IWM", "sell", 10, 9.0, "s1"),     # -10.00, sold in 2026
                F("2027-01-05T15:00:00Z", "IWM", "buy", 10, 9.0, "b2")]      # repurchase in 2027

    def test_a_december_loss_with_a_january_repurchase_is_flagged_and_stays_in_2026(self):
        closed = R.fifo(self.DEC_LOSS)["closed"]
        watch = R.wash_watch(closed, self.DEC_LOSS)
        self.assertEqual(len(watch), 1)                         # the 30-day window crosses the year end
        self.assertEqual(watch[0]["sale_date"], "2026-12-28")
        self.assertEqual(R.wash_totals(R.watch_for_year(watch, 2026))["same_account"]["upper_bound"], -10.0)
        self.assertEqual(R.wash_totals(R.watch_for_year(watch, 2027))["same_account"]["sales"], 0)

    def test_the_year_filter_can_say_no(self):
        """Positive control: a year with no sales yields nothing, and a missing list is not an error."""
        watch = R.wash_watch(R.fifo(self.DEC_LOSS)["closed"], self.DEC_LOSS)
        self.assertEqual(R.watch_for_year(watch, 2025), [])
        self.assertEqual(R.watch_for_year(None, 2026), [])

    def test_the_new_years_line_does_not_inherit_last_years_wash_flag(self):
        closed = R.fifo(self.DEC_LOSS)["closed"]
        watch = R.wash_watch(closed, self.DEC_LOSS)
        blk = {"state": "ok", "by_year": R.summarize_by_year(closed)}
        self.assertIn("possibly wash-sale", R.report_line("A", blk, 2026, watch))
        line27 = R.report_line("A", blk, 2027, watch)
        self.assertIn("(0 sales)", line27)
        self.assertNotIn("wash", line27)

    def test_without_the_list_all_time_totals_are_labelled_all_years(self):
        """A status block carries only all-time wash totals. Across years that must say so,
        not pass last year's exposure off as this year's."""
        closed = R.fifo(self.DEC_LOSS)["closed"]
        blk = {"state": "ok", "by_year": R.summarize_by_year(closed),
               "wash_watch": R.wash_totals(R.wash_watch(closed, self.DEC_LOSS))}
        self.assertIn("(all years)", R.report_line("A", blk, 2027))
        self.assertNotIn("(all years)", R.report_line("A", blk, 2026))     # the only year: nothing to disclaim

    def test_yearly_totals_add_up_to_the_all_time_total(self):
        fills = self.DEC_LOSS[:2] + [F("2027-01-05T15:00:00Z", "IWM", "buy", 10, 9.0, "b2"),
                                     F("2027-01-20T15:00:00Z", "IWM", "sell", 10, 12.0, "s2")]
        closed = R.fifo(fills)["closed"]
        by = R.summarize_by_year(closed)
        allt = R.summarize(closed)
        self.assertEqual(sorted(by), ["2026", "2027"])
        self.assertAlmostEqual(sum(b["st"]["net"] for b in by.values()), allt["st"]["net"], places=2)
        self.assertEqual(sum(b["st"]["sales"] for b in by.values()), allt["st"]["sales"])

    def test_the_sale_year_is_the_new_york_year_not_the_utc_year(self):
        """9pm New York on Dec 31 is already Jan 1 in UTC; the sale is a 2026 sale."""
        buy = F("2026-12-30T15:00:00Z", "X", "buy", 1, 10.0, "b")
        late_dec = R.fifo([buy, F("2027-01-01T02:30:00Z", "X", "sell", 1, 11.0, "s")])["closed"]
        self.assertEqual(sorted(R.summarize_by_year(late_dec)), ["2026"])
        new_year = R.fifo([buy, F("2027-01-01T06:30:00Z", "X", "sell", 1, 11.0, "s")])["closed"]
        self.assertEqual(sorted(R.summarize_by_year(new_year)), ["2027"])

    def _ledgers(self, d):
        """A 2026 December loss in Arm A, and Arm B buying the same symbol in January."""
        p = lambda n: os.path.join(d, n)
        closed = R.fifo(self.DEC_LOSS)["closed"]
        by_a = R.summarize_by_year(closed)
        z = {"st": {"gains": 0.0, "losses": 0.0, "net": 0.0, "sales": 0},
             "lt": {"gains": 0, "losses": 0, "net": 0, "sales": 0}}
        def dump(name, obj):
            with open(p(name), "w", encoding="utf-8") as fh:
                json.dump(obj, fh)
        dump("realized_a.json", {"summary": {"state": "ok", "by_year": by_a}, "closed": closed,
                                 "wash_watch": R.wash_watch(closed, self.DEC_LOSS),
                                 "buys": [], "open": {}})
        dump("realized_b.json", {"summary": {"state": "ok", "rh_log_sells_counted": 0,
                                             "by_year": {"2026": z}},
                                 "closed": [], "wash_watch": [],
                                 "buys": [{"t": "2027-01-10", "symbol": "IWM", "qty": 1.0}], "open": {}})
        dump("status.json", {"realized": {"state": "ok", "by_year": by_a}})
        with open(p("rh_trade_log.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("")
        return dict(status_path=p("status.json"), a_ledger=p("realized_a.json"),
                    b_ledger=p("realized_b.json"), log_path=p("rh_trade_log.jsonl"))

    def test_january_keeps_last_years_final_figures_in_view(self):
        kw = self._ledgers(tempfile.mkdtemp())
        text = "\n".join(R.repo_report_lines(today=date(2027, 1, 15), **kw))
        self.assertIn("Arm A (Alpaca) 2027 realized, short-term: gains $0.00, losses $0.00, net $0.00 (0 sales)", text)
        self.assertIn("Prior tax year 2026 (still open for filing):", text)
        prior = text.split("Prior tax year 2026")[1]
        self.assertIn("Arm A (Alpaca) 2026 realized", prior)
        self.assertIn("losses -$10.00", prior)
        self.assertIn("up to $10.00 possibly wash-sale", prior)
        self.assertIn("Cross-account wash-sale watch 2026", prior)         # Arm A's Dec loss vs Arm B's Jan buy
        self.assertNotIn("Cross-account", text.split("Prior tax year 2026")[0])   # the new year has none

    def test_prior_year_lines_end_after_the_filing_window(self):
        kw = self._ledgers(tempfile.mkdtemp())
        late = "\n".join(R.repo_report_lines(today=date(2027, 11, 15), **kw))
        self.assertNotIn("Prior tax year", late)
        self.assertNotIn("2026 realized", late)
        edge = "\n".join(R.repo_report_lines(today=date(2027, 10, 31), **kw))
        self.assertIn("Prior tax year 2026", edge)                          # last day still shown

    def test_no_prior_year_block_when_the_prior_year_had_no_sales(self):
        kw = self._ledgers(tempfile.mkdtemp())
        text = "\n".join(R.repo_report_lines(today=date(2026, 9, 30), **kw))
        self.assertNotIn("Prior tax year", text)                            # 2025 had no sales
        self.assertIn("Arm A (Alpaca) 2026 realized", text)


if __name__ == "__main__":
    unittest.main()
