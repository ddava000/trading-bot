"""End-to-end tests of Arm A's realized-P&L pipeline in alpaca_bot.py against a FAKE broker.

alpaca_bot.realized_for_run() runs inside the live trading cycle, so these tests are what stand
between an edit and a bad number in status.json (or a broken bot). They run in a temp directory so
they never touch the repo's real realized_a.json.

Run:  python -m unittest discover -s tests -v      (needs the `requests` package)
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot   # noqa: E402
import realized as R       # noqa: E402


def acts(sell_price=12.0, extra=None):
    base = [
        {"id": "1", "activity_type": "CSD", "net_amount": "100", "date": "2026-08-01"},
        {"id": "2", "activity_type": "FILL", "symbol": "AAA", "side": "buy", "qty": "10", "price": "10",
         "transaction_time": "2026-08-12T14:00:00Z", "order_id": "o1"},
        {"id": "3", "activity_type": "FILL", "symbol": "AAA", "side": "sell", "qty": "4", "price": str(sell_price),
         "transaction_time": "2026-08-13T14:00:00Z", "order_id": "o2"},
    ]
    return base + (extra or [])


class FakeBrokerCase(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)                     # REALIZED_F is relative: keep the repo clean
        self.acts = acts()
        self.pos_qty, self.cash, self.fail = 6.0, 48.0, False
        p1 = mock.patch.object(bot, "alpaca_get", side_effect=self._get)
        p2 = mock.patch.object(bot.time, "sleep", lambda s: None)   # the mismatch retry sleeps 4s
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        bot._RUN_REALIZED.clear()

    def tearDown(self):
        os.chdir(self._cwd)

    def _get(self, path, _tries=3):
        if self.fail:
            return {"message": "service unavailable"}
        if path.startswith("/v2/account/activities"):
            return [] if "page_token" in path else self.acts
        if path == "/v2/positions":
            return [{"symbol": "AAA", "qty": str(self.pos_qty), "market_value": str(self.pos_qty * 11)}]
        if path == "/v2/account":
            return {"cash": str(self.cash)}
        return {}

    def fresh(self):
        bot._RUN_REALIZED.clear()
        return bot.realized_for_run()


class SuccessPathTests(FakeBrokerCase):
    def test_healthy_account_publishes_an_ok_block_and_a_ledger(self):
        s = self.fresh()
        self.assertEqual(s["state"], "ok")
        self.assertEqual((s["st"]["gains"], s["st"]["sales"]), (8.0, 1))
        self.assertEqual(s["by_year"]["2026"]["st"]["net"], 8.0)
        self.assertLess(abs(s["reconciliation_residual"]), 0.01)      # the accounting identity closes
        self.assertTrue(os.path.exists("realized_a.json"))

    def test_recomputing_without_new_fills_does_not_rewrite_the_ledger(self):
        """Deterministic on purpose: the persist step commits it, so a changing file would
        mean a commit every 15 minutes."""
        self.fresh()
        before = os.path.getmtime("realized_a.json")
        time.sleep(0.05)
        self.fresh()
        self.assertEqual(os.path.getmtime("realized_a.json"), before)

    def test_result_is_computed_once_per_process(self):
        first = self.fresh()
        self.assertIs(bot.realized_for_run(), first)


class FailureModeTests(FakeBrokerCase):
    def test_a_transient_api_failure_carries_the_last_good_figures_forward_as_stale(self):
        self.fresh()                                     # establishes a ledger
        self.fail = True
        s = self.fresh()
        self.assertEqual((s["state"], s["st"]["gains"]), ("stale", 8.0))
        self.assertIn("unavailable", s["reason"])
        self.assertEqual(s["as_of"], "2026-08-13T14:00:00Z")

    def test_no_ledger_and_api_down_is_unknown_never_zero(self):
        self.fail = True
        s = self.fresh()
        self.assertEqual(s["state"], "unknown")
        self.assertNotIn("st", s)                        # no invented totals
        self.assertIn("UNKNOWN", R.report_line("Arm A", s))

    def test_an_unexplained_share_makes_it_unverified(self):
        self.pos_qty = 7.0                               # broker holds 1 more than the fills explain
        s = self.fresh()
        self.assertEqual(s["state"], "unverified")
        self.assertIn("AAA", s["reason"])

    def test_a_stock_split_makes_it_unverified(self):
        self.acts = acts(extra=[{"id": "9", "activity_type": "SSP", "symbol": "AAA"}])
        s = self.fresh()
        self.assertEqual(s["state"], "unverified")
        self.assertIn("SSP", s["reason"])

    def test_a_wrong_fill_price_is_caught_by_the_accounting_identity(self):
        """Position reconciliation only proves share COUNTS line up. Fills now imply +12 while
        the account cash still reflects +8, and only the identity can see that."""
        self.acts = acts(sell_price=13.0)
        s = self.fresh()
        self.assertEqual(s["state"], "unverified")
        self.assertIn("residual", s["reason"])

    def test_it_recovers_to_ok_once_the_problem_clears(self):
        self.pos_qty = 7.0
        self.assertEqual(self.fresh()["state"], "unverified")
        self.pos_qty = 6.0
        self.assertEqual(self.fresh()["state"], "ok")


class PaginationTests(FakeBrokerCase):
    def test_pages_are_followed_and_duplicates_dropped(self):
        rows = [{"id": str(i), "activity_type": "CSD", "net_amount": "1"} for i in range(1, 6)]
        pages = {None: rows[0:2], "2": rows[1:4], "4": rows[4:5]}     # id 2 repeats across pages

        def get(path, _tries=3):
            token = path.split("page_token=")[1] if "page_token=" in path else None
            return pages[token]
        with mock.patch.object(bot, "alpaca_get", side_effect=get):
            got = bot.alpaca_activity_history(page_size=2)
        self.assertEqual([a["id"] for a in got], ["1", "2", "3", "4", "5"])

    def test_an_error_object_yields_none_never_a_partial_history(self):
        """A partial history would produce a confident WRONG total, which is worse than none."""
        calls = {"n": 0}

        def get(path, _tries=3):
            calls["n"] += 1
            return [{"id": "1", "activity_type": "CSD"}, {"id": "2", "activity_type": "CSD"}] \
                if calls["n"] == 1 else {"message": "rate limited"}
        with mock.patch.object(bot, "alpaca_get", side_effect=get):
            self.assertIsNone(bot.alpaca_activity_history(page_size=2))


if __name__ == "__main__":
    unittest.main()
