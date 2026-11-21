"""The read-only ACCOUNT_TEST now shows corporate actions and positions with no usable cost basis (2026-11-20).

WHY. 2026-11-16: BKH became HZN (a ticker change). Alpaca reported it as two NC activity rows; the realized block went UNVERIFIED and stays so,
because any NC row counts as FIFO-breaking. LAPTOP BOT also raised a worse possibility it could not check from outside: the bracket loop skips a
held position whose broker avg_entry_price is 0 (`cost <= 0: continue`), so a basis that did not cross the rename would leave that position
with NO stop, silently. Nobody could see the raw NC rows or the broker's basis without Alpaca keys, which exist only in the Actions secrets. The
account test already makes these reads; it now prints them. These tests run the REAL __main__ block against a fake broker.

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

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

ACCOUNT = {"cash": "28.60", "equity": "199.53", "last_equity": "200.00", "account_number": "PA0000008736", "status": "ACTIVE"}


def position(sym, qty, avg, **kw):
    return dict({"symbol": sym, "qty": str(qty), "qty_available": str(qty), "avg_entry_price": str(avg), "cost_basis": str(round(qty * avg, 2)),
                 "market_value": str(round(qty * avg, 2)), "current_price": str(avg)}, **kw)


NC_OUT = {"id": "nc1", "activity_type": "NC", "date": "2026-11-16", "symbol": "BKH", "qty": "-0.144", "net_amount": "0", "description": "NAME CHANGE BKH TO HZN"}
NC_IN = {"id": "nc2", "activity_type": "NC", "date": "2026-11-16", "symbol": "HZN", "qty": "0.144", "net_amount": "0", "description": "NAME CHANGE BKH TO HZN"}
FILL = {"id": "f1", "activity_type": "FILL", "symbol": "BKH", "side": "buy", "qty": "0.144", "price": "77", "transaction_time": "2026-10-07T14:00:00Z", "order_id": "o1"}


def run_account_test(positions, activities, activity_error=None, positions_error=None):
    """Execute the real __main__ block with ACCOUNT_TEST=true. The namespace is built INSIDE the patches so the block's own direct calls see the fakes."""
    with open(os.path.join(ROOT, "alpaca_bot.py"), encoding="utf-8") as f:
        src = f.read()
    tail = src[src.index('if __name__ == "__main__":'):]

    calls = {"positions": 0}

    def get(path, _tries=3):
        if path == "/v2/account":
            return dict(ACCOUNT)
        if path.startswith("/v2/account/activities"):
            if "activity_types=" in path:
                return []                                    # the capital-flow reader
            if activity_error:
                raise activity_error
            if activities is None:
                return {"message": "rate limit exceeded"}            # an error body, not a list
            return list(activities)
        if path == "/v2/positions":
            calls["positions"] += 1
            if positions_error and calls["positions"] >= 2:          # the account test's own read (1st) works; the NEW section's read (2nd) fails
                raise positions_error
            return list(positions)
        return {"message": "stub"}
    out, err = io.StringIO(), None
    with mock.patch.object(bot, "alpaca_get", get), mock.patch.dict(os.environ, {"ACCOUNT_TEST": "true"}), mock.patch.object(bot.time, "sleep", lambda s: None), \
            contextlib.redirect_stdout(out):
        g = dict(vars(bot))
        g["__name__"] = "__main__"
        try:
            exec(compile(tail, "alpaca_bot.py(main block)", "exec"), g)
        except SystemExit as e:
            err = e
    return err, out.getvalue()


class CorporateActionsAreShown(unittest.TestCase):
    def test_the_rename_rows_and_the_new_symbols_broker_row_are_printed_raw(self):
        err, out = run_account_test([position("SPY", 0.2, 600.0), position("HZN", 0.144, 77.0)], [FILL, NC_OUT, NC_IN])
        self.assertEqual(err.code, 0)
        self.assertIn("corp-actions: 2 activity row(s) of a FIFO-breaking type", out)
        self.assertIn('"activity_type": "NC"', out)
        self.assertIn('"symbol": "HZN"', out)
        self.assertIn("NAME CHANGE BKH TO HZN", out)
        self.assertIn('position {"symbol": "HZN", "qty": "0.144"', out)
        self.assertIn('"avg_entry_price": "77.0"', out)                       # the datum LAPTOP BOT could not see
        self.assertIn("every held position has a positive avg_entry_price", out)

    def test_a_realistic_length_row_still_shows_its_symbol_and_qty(self):
        """2026-11-20: the first live run cut each real NC row off at the 420-character cap, in ALPHABETICAL order, so the row ended at "price" and the
        symbol and qty (the fields the whole check exists to show) never printed. Short rows in the other tests could not catch that. This row is the
        real shape: the full field set Alpaca sent, ids and all."""
        real = {"account_id": "acct-secret", "activity_sub_type": "SNC", "activity_type": "NC",
                "corporate_action_id": "0bbf827c-e765-4887-a603-e5f5e4236212", "created_at": "2026-11-16T12:06:55.507987Z", "currency": "USD",
                "cusip": "092113109", "date": "2026-11-16", "description": "Name Change from BKH to HZN",
                "group_id": "e54ff29b-0935-4acf-99d5-aa6f955a8647", "id": "20261116000000000::8efd7046-f94e-439a-8d43-9c585e1386e7",
                "net_amount": "0", "price": "76.75", "qty": "-0.143977258", "status": "executed", "symbol": "BKH",
                "system_date": "2026-11-16", "per_share_amount": "0", "leaves_qty": "0", "side": "sell"}
        err, out = run_account_test([position("HZN", 0.143977258, 76.75)], [real, dict(real, id="20261116000000000::fdfab492-a77c-4c5e-832e-fe6cfe5dbda4", symbol="HZN", qty="0.143977258")])
        self.assertEqual(err.code, 0)
        rows = [ln for ln in out.splitlines() if ln.startswith("    activity ")]
        self.assertEqual(len(rows), 2)
        for ln in rows:
            for needle in ('"symbol": ', '"qty": ', '"price": "76.75"', '"date": "2026-11-16"', '"description": "Name Change from BKH to HZN"'):
                self.assertIn(needle, ln)                                 # nothing the check exists to show may fall behind a cap
        self.assertIn('"symbol": "BKH"', rows[0] + rows[1])
        self.assertIn('"qty": "-0.143977258"', rows[0] + rows[1])
        self.assertNotIn("acct-secret", out)

    def test_an_unrelated_position_is_not_printed(self):
        _, out = run_account_test([position("SPY", 0.2, 600.0), position("HZN", 0.144, 77.0)], [NC_OUT, NC_IN])
        self.assertNotIn('"symbol": "SPY"', out)
        self.assertNotIn("account_id", out)

    def test_a_position_with_no_cost_basis_is_named_as_having_no_stop(self):
        """The silent-skip hazard: avg_entry_price 0 makes the bracket loop skip the symbol. It must be printed even with no corporate action at all."""
        err, out = run_account_test([position("SPY", 0.2, 600.0), position("HZN", 0.144, 0)], [])
        self.assertEqual(err.code, 0)
        self.assertIn("NO usable avg_entry_price for ['HZN']", out)
        self.assertIn("SKIPS these, they have no stop", out)
        self.assertIn('position {"symbol": "HZN"', out)

    def test_control_a_clean_account_prints_no_rows_and_says_all_bases_are_positive(self):
        err, out = run_account_test([position("SPY", 0.2, 600.0), position("QQQ", 0.1, 500.0)], [FILL])
        self.assertEqual(err.code, 0)
        self.assertIn("corp-actions: 0 activity row(s)", out)
        self.assertNotIn("    position ", out)
        self.assertNotIn("    activity ", out)
        self.assertIn("every held position has a positive avg_entry_price", out)

    def test_a_missing_avg_entry_price_counts_as_no_basis(self):
        bad = position("HZN", 0.144, 77.0)
        bad.pop("avg_entry_price")
        _, out = run_account_test([bad], [])
        self.assertIn("NO usable avg_entry_price for ['HZN']", out)


class RealizedBlockAndBuyDatesAreShown(unittest.TestCase):
    """2026-11-20: the account test also prints what the real realized block says and which buy date each position's time stop will use, so a
    corporate-action change can be replayed on live data instead of argued about."""

    def test_the_realized_line_shows_the_state_and_the_proven_renames(self):
        err, out = run_account_test([position("HZN", 0.144, 77.0)], [FILL, NC_OUT, NC_IN])
        self.assertEqual(err.code, 0)
        line = [ln for ln in out.splitlines() if ln.startswith("  realized :")]
        self.assertEqual(len(line), 1, out)
        self.assertIn("state=", line[0])
        self.assertIn('renames={}', line[0])                  # these short test rows are not a proven pair (no group key): the strict rule says so

    def test_a_proven_pair_is_reported_as_a_rename(self):
        real = {"activity_sub_type": "SNC", "activity_type": "NC", "corporate_action_id": "ca1", "cusip": "1", "date": "2026-11-16",
                "net_amount": "0", "status": "executed"}
        rows = [dict(real, id="a1", symbol="BKH", qty="-0.144"), dict(real, id="a2", symbol="HZN", qty="0.144")]
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:               # buy dates come from trade_log.jsonl in the working directory: never the repo's real one
            os.chdir(d)
            try:
                with open("trade_log.jsonl", "w") as f:
                    f.write(json.dumps({"ts": "2026-10-21T09:45", "side": "buy", "symbol": "BKH"}) + "\n")
                err, out = run_account_test([position("HZN", 0.144, 77.0)], [FILL] + rows)
            finally:
                os.chdir(cwd)
        self.assertEqual(err.code, 0)
        self.assertIn('renames={"BKH": "HZN"}', out)
        self.assertIn('"HZN": "2026-10-21T09:45"', out)         # the BKH buy was credited to HZN, so its time stop has a date

    def test_the_buy_dates_line_is_printed(self):
        _, out = run_account_test([position("SPY", 0.2, 600.0)], [])
        self.assertIn("  buy dates: ", out)


class ItCanNeverFailTheTest(unittest.TestCase):
    def test_an_error_body_for_the_activity_history_prints_zero_rows_and_says_why(self):
        err, out = run_account_test([position("SPY", 0.2, 600.0)], None)
        self.assertEqual(err.code, 0)
        self.assertIn("activity history unavailable", out)
        self.assertIn("corp-actions: 0 activity row(s)", out)

    def test_an_exception_while_reading_the_activity_history_is_reported_not_raised(self):
        err, out = run_account_test([position("SPY", 0.2, 600.0)], [], activity_error=OSError("down"))
        self.assertEqual(err.code, 0)
        self.assertIn("corp-actions: could not read (OSError: down)", out)

    def test_an_exception_in_the_new_section_is_printed_and_the_test_still_ends_normally(self):
        err, out = run_account_test([position("SPY", 0.2, 600.0)], [NC_IN], positions_error=OSError("boom"))
        self.assertEqual(err.code, 0)
        self.assertIn("corp-actions: could not read (OSError: boom)", out)
        self.assertIn("capital  :", out)                                     # everything printed before it is still there

    def test_the_existing_output_is_unchanged(self):
        _, out = run_account_test([position("SPY", 0.2, 600.0)], [])
        for line in ("=== ACCOUNT_TEST (", "endpoint :", "equity   :", "positions: 1 ['SPY']", "rails    :", "capital  :"):
            self.assertIn(line, out)


if __name__ == "__main__":
    unittest.main()
