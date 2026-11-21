"""The shape of every order the live bot can send, pinned at the payload level.

The standing rails are cash-only, no leverage, no shorting, no options. The rails are enforced in
many places in a 2,300 line engine; this file pins the one place they all end up, the order payload,
so an edit that sneaks in a margin or options field, a notional on a sell, or an oversized quantity
fails here and not in production. Nothing touches the network: alpaca_order and alpaca_asset are
replaced, and the tests assert on what would have been POSTed.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

ALLOWED_KEYS = {"symbol", "notional", "qty", "side", "type", "time_in_force", "limit_price"}


class Harness(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.asset = {"tradable": True, "fractionable": True}

        def fake_order(payload):
            self.sent.append(dict(payload))
            return {"id": "o1"}

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(bot, "alpaca_order", fake_order))
        stack.enter_context(mock.patch.object(bot, "alpaca_asset", lambda sym: self.asset))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def buy(self, dollars, live):
        return bot.place_buy("XYZ", dollars, live)

    def sell(self, qty, live):
        return bot.place_sell("XYZ", qty, live)


class BuyPayloadTests(Harness):
    def test_a_liquid_fractionable_name_is_a_market_notional_buy(self):
        self.buy(50.0, 100.0)
        self.assertEqual(self.sent, [{"symbol": "XYZ", "notional": "50.0", "side": "buy",
                                      "type": "market", "time_in_force": "day"}])

    def test_the_notional_is_rounded_to_cents(self):
        self.buy(33.3333, 100.0)
        self.assertEqual(self.sent[0]["notional"], "33.33")

    def test_a_non_fractionable_name_buys_whole_shares_it_can_afford(self):
        self.asset = {"tradable": True, "fractionable": False}
        self.buy(250.0, 60.0)
        p = self.sent[0]
        self.assertEqual((p["qty"], p["type"], "notional" in p), ("4", "market", False))
        self.assertLessEqual(int(p["qty"]) * 60.0, 250.0)

    def test_a_cheap_name_is_a_whole_share_marketable_limit_two_percent_above_the_quote(self):
        self.buy(100.0, 2.00)
        p = self.sent[0]
        self.assertEqual((p["type"], p["qty"], p["limit_price"]), ("limit", "50", "2.04"))
        self.assertLessEqual(int(p["qty"]) * 2.00, 100.0)

    def test_a_sub_dollar_limit_keeps_four_decimals(self):
        self.buy(10.0, 0.5)
        self.assertEqual(self.sent[0]["limit_price"], "0.51")
        self.sent.clear()
        self.buy(10.0, 0.3333)
        self.assertEqual(self.sent[0]["limit_price"], "0.34")
        self.assertEqual(bot._px(0.33337), 0.3334)
        self.assertEqual(bot._px(12.3456), 12.35)

    def test_a_name_alpaca_says_is_not_tradable_sends_nothing(self):
        self.asset = {"tradable": False}
        r = self.buy(50.0, 100.0)
        self.assertEqual(self.sent, [])
        self.assertIn("not tradable", r["message"])

    def test_less_than_one_share_of_a_non_fractionable_name_sends_nothing(self):
        self.asset = {"tradable": True, "fractionable": False}
        r = self.buy(30.0, 100.0)
        self.assertEqual(self.sent, [])
        self.assertIn("buys <1 share", r["message"])

    def test_every_buy_is_a_plain_long_day_order_with_no_margin_or_options_fields(self):
        scenarios = [({"tradable": True, "fractionable": True}, 50.0, 100.0),
                     ({"tradable": True, "fractionable": False}, 250.0, 60.0),
                     ({"tradable": True, "fractionable": True}, 100.0, 2.0),
                     ({"tradable": True, "fractionable": True}, 10.0, 0.4)]
        for asset, dollars, live in scenarios:
            with self.subTest(asset=asset, dollars=dollars, live=live):
                self.sent.clear()
                self.asset = asset
                self.buy(dollars, live)
                p = self.sent[0]
                self.assertTrue(set(p) <= ALLOWED_KEYS, set(p) - ALLOWED_KEYS)
                self.assertEqual((p["side"], p["time_in_force"]), ("buy", "day"))
                self.assertIn(p["type"], ("market", "limit"))
                self.assertNotIn("order_class", p)
                self.assertNotIn("position_intent", p)

    def test_the_rail_check_can_say_no(self):
        """Positive control: a payload carrying a forbidden field is caught by the same assertion."""
        bad = {"symbol": "X", "side": "buy", "type": "market", "time_in_force": "day", "order_class": "bracket"}
        self.assertFalse(set(bad) <= ALLOWED_KEYS)


class SellPayloadTests(Harness):
    def test_a_sell_sends_exactly_the_quantity_given_and_never_a_notional(self):
        for qty in (0.341418, 1, 7.5):
            with self.subTest(qty=qty):
                self.sent.clear()
                self.sell(qty, 100.0)
                p = self.sent[0]
                self.assertEqual((p["side"], p["qty"], p["time_in_force"]), ("sell", str(qty), "day"))
                self.assertNotIn("notional", p)
                self.assertTrue(set(p) <= ALLOWED_KEYS)

    def test_a_fractional_quantity_must_go_market(self):
        self.sell(0.5, 3.0)                                   # cheap, but fractional: Alpaca allows market only
        self.assertEqual(self.sent[0]["type"], "market")

    def test_a_whole_share_cheap_sell_is_a_marketable_limit_two_percent_below(self):
        self.sell(10, 2.00)
        p = self.sent[0]
        self.assertEqual((p["type"], p["limit_price"]), ("limit", "1.96"))

    def test_a_liquid_whole_share_sell_is_a_market_order(self):
        self.sell(3, 100.0)
        self.assertEqual(self.sent[0]["type"], "market")
        self.assertNotIn("limit_price", self.sent[0])

    def test_no_live_quote_means_market(self):
        self.sell(3, None)
        self.assertEqual(self.sent[0]["type"], "market")


class AcceptanceTests(unittest.TestCase):
    def test_ok_requires_an_id_and_no_error_message(self):
        self.assertTrue(bot._ok({"id": "abc"}))
        self.assertFalse(bot._ok({"id": "abc", "message": "insufficient"}))
        self.assertFalse(bot._ok({"message": "rejected"}))
        self.assertFalse(bot._ok({}))
        self.assertFalse(bot._ok(None))

    def test_a_rejected_buy_is_reported_not_raised(self):
        with mock.patch.object(bot, "alpaca_order", lambda p: {"message": "insufficient buying power"}), \
                mock.patch.object(bot, "alpaca_asset", lambda s: {"tradable": True, "fractionable": True}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            r = bot.place_buy("XYZ", 50.0, 100.0)
        self.assertFalse(bot._ok(r))
        self.assertIn("buy rejected", out.getvalue())


if __name__ == "__main__":
    unittest.main()
