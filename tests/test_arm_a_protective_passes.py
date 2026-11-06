"""Arm A's protective pass (exit_pass), two behaviours Devon chose from clickable options on 2026-11-04 / 2026-11-05.

1. A STOP SELL THAT RESTS UNFILLED. Under CHEAP_PX a stop sells with a DAY limit at 98% of the price, and exit_pass skips any symbol with a resting
   order, so a price that keeps falling past that limit leaves the sell unfilled, never re-priced, and the stock unsellable that day. A resting sell
   that is the bot's OWN stop (its order id is in trade_log.jsonl with stop_loss / hold_stop / news_exit), has rested 5 minutes and sits above the
   live price is CANCELLED; the next pass places a fresh stop at the current price. A bracket's take-profit leg is a resting sell above the market
   on purpose and must never be cancelled: that is the hazard these tests pin hardest.
2. A HELD STOCK WITH NO LIVE PRICE. Devon is emailed once when a held name has had no price for 3 consecutive passes, with a back-off kept in
   outage.json. No trading change and no other price source.

The real exit_pass runs in the cold-review harness (real functions, fake world); cancels and emails are recorded, never sent.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")           # alpaca_bot reads its keys at import: set them BEFORE it, so this file
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")     # also runs alone (the full suite only worked by import order)

import alpaca_bot as bot  # noqa: E402
from test_arm_a_cold_review import ET_NOW, broker, pos, sandbox  # noqa: E402

NOW_UTC = ET_NOW.astimezone(timezone.utc)


def ts(minutes_ago):
    return (NOW_UTC - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def order(oid="stop1", sym="XYZ", side="sell", typ="limit", limit="2.94", age=6.0, **kw):
    return dict({"id": oid, "symbol": sym, "side": side, "type": typ, "limit_price": limit, "created_at": ts(age), "status": "new"}, **kw)


def write_log(*rows):
    with open("trade_log.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


STOP = {"ts": "2026-10-28T10:20", "side": "sell", "symbol": "XYZ", "order_id": "stop1", "stop_loss": True}
XYZ = pos("XYZ", 10, 3.0, 2.70)            # cost 3.00, price 2.70: below the -7% stop (2.79)


class PureHelpers(unittest.TestCase):
    LASTS = {"XYZ": 2.70}
    OWN = {"stop1"}

    def stale(self, rows, own=OWN, lasts=LASTS):
        return bot.stale_stop_sells(rows, own, lasts, NOW_UTC)

    def test_our_stop_that_rested_six_minutes_above_the_price_is_stale(self):
        got = self.stale([order()])
        self.assertEqual([(o["id"], o["symbol"], o["limit"], o["live"]) for o in got], [("stop1", "XYZ", 2.94, 2.70)])
        self.assertAlmostEqual(got[0]["age"], 6.0, places=1)

    def test_every_control_that_must_leave_the_order_alone(self):
        cases = {
            "only 4 minutes old": [order(age=4.0)],
            "exactly at the limit is marketable": [order(limit="2.70")],
            "price above the limit": [order(limit="2.50")],
            "not one of our stops (a bracket take-profit leg)": [order(oid="tp1", limit="3.60")],
            "a buy": [order(side="buy")],
            "a market order": [order(typ="market", limit=None)],
            "no id": [order(oid=None)],
            "no symbol": [order(sym=None)],
            "no creation time": [dict(order(), created_at=None)],
            "garbage creation time": [dict(order(), created_at="yesterday")],
            "no limit price": [order(limit=None)],
            "no live price for the symbol": [order(sym="ABC", oid="stop1")],
        }
        for label, rows in cases.items():
            self.assertEqual(self.stale(rows), [], label)
        self.assertEqual(self.stale([]), [])
        self.assertEqual(self.stale(None), [])
        self.assertEqual(self.stale([{"symbol": "XYZ"}]), [])                    # the bare rows older tests use

    def test_the_older_order_type_field_is_understood(self):
        row = order()
        row.pop("type")
        row["order_type"] = "limit"
        self.assertEqual(len(self.stale([row])), 1)

    def test_only_the_stale_ones_of_several_are_returned(self):
        rows = [order("stop1"), order("stop2", sym="XYZ", age=2.0), order("stop3", sym="XYZ", age=9.0)]
        self.assertEqual(sorted(o["id"] for o in self.stale(rows, own={"stop1", "stop2", "stop3"})), ["stop1", "stop3"])


class OwnStopIds(unittest.TestCase):
    def ids(self, *rows, raw=None, tail=600):
        with sandbox(broker()) as ns:
            if raw is not None:
                open("trade_log.jsonl", "w").write(raw)
            elif rows:
                write_log(*rows)
            return bot.own_stop_sell_ids(tail=tail)

    def test_only_our_protective_sells_are_ours(self):
        got = self.ids({"side": "sell", "order_id": "a", "stop_loss": True}, {"side": "sell", "order_id": "b", "hold_stop": True},
                       {"side": "sell", "order_id": "c", "news_exit": True},
                       {"side": "sell", "order_id": "d", "take_profit": True},          # a take-profit sell: not a stop
                       {"side": "buy", "order_id": "e", "stop_loss": True},             # a buy is never a stop sell
                       {"side": "sell", "stop_loss": True},                             # no order id
                       {"side": "sell", "order_id": "g"})                               # an ordinary sell
        self.assertEqual(got, {"a", "b", "c"})

    def test_junk_lines_a_missing_log_and_the_tail_limit(self):
        self.assertEqual(self.ids(raw='not json\n{"side": "sell", "order_id": "x", "stop_loss": true}\n[1,2]\n\n'), {"x"})
        self.assertEqual(self.ids(), set())                                              # no trade log at all
        rows = [{"side": "sell", "order_id": "old", "stop_loss": True}] + [{"side": "buy", "order_id": str(i)} for i in range(10)]
        self.assertEqual(self.ids(*rows, tail=5), set())                                 # the old one fell off the tail
        self.assertEqual(self.ids(*rows, tail=50), {"old"})


class CancelCall(unittest.TestCase):
    def cancel(self, status=204, text="", boom=None):
        calls = []

        def fake(url, headers=None, timeout=None):
            calls.append(url)
            if boom:
                raise boom
            return mock.Mock(status_code=status, text=text)
        out = io.StringIO()
        with mock.patch.object(bot.requests, "delete", fake), contextlib.redirect_stdout(out):
            return bot.alpaca_cancel_order("abc-123"), calls, out.getvalue()

    def test_204_is_accepted_and_it_deletes_exactly_that_order(self):
        ok, calls, out = self.cancel(204)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("/v2/orders/abc-123"))
        self.assertEqual(out, "")

    def test_refusals_and_errors_are_false_and_logged_never_raised(self):
        for status, text in ((404, "order not found"), (422, "order is not cancelable"), (500, "boom")):
            ok, _, out = self.cancel(status, text)
            self.assertFalse(ok)
            self.assertIn("not accepted", out)
        ok, _, out = self.cancel(boom=OSError("reset"))
        self.assertFalse(ok)
        self.assertIn("failed", out)


class OpenOrderRows(unittest.TestCase):
    def test_the_set_is_still_a_plain_set_of_symbols_and_remembers_its_rows(self):
        rows = [{"symbol": "PLUG", "id": "1"}, {"symbol": "SPY", "id": "2"}, {"id": "no-symbol"}]
        with mock.patch.object(bot, "alpaca_get", lambda path, _tries=3: rows):
            got = bot.alpaca_open_orders()
        self.assertEqual(got, {"PLUG", "SPY"})
        self.assertIsInstance(got, set)
        self.assertEqual([r["id"] for r in got.rows], ["1", "2", "no-symbol"])

    def test_an_error_reply_is_still_the_fail_open_empty_set(self):
        with mock.patch.object(bot, "alpaca_get", lambda path, _tries=3: {"message": "rate limit exceeded"}), contextlib.redirect_stdout(io.StringIO()) as out:
            got = bot.alpaca_open_orders()
        self.assertEqual(got, set())
        self.assertEqual(getattr(got, "rows", ()), ())
        self.assertIn("open orders unreadable", out.getvalue())


class ExitPassCancelsStaleStops(unittest.TestCase):
    def go(self, orders, log=(STOP,), prices=None, cancel=True, passes=1, positions=None):
        cancels = []

        def rec(oid):
            cancels.append(oid)
            return cancel
        with sandbox(broker(positions=positions or [XYZ], orders=orders), prices=prices or {"XYZ": 2.70}) as ns:
            write_log(*log)
            with mock.patch.object(bot, "alpaca_cancel_order", rec):
                for _ in range(passes):
                    placed = bot.exit_pass(ET_NOW, set())
            return placed, cancels, ns

    def test_our_stale_stop_is_cancelled_and_nothing_is_placed_in_the_same_pass(self):
        placed, cancels, ns = self.go([order()])
        self.assertEqual(cancels, ["stop1"])
        self.assertEqual(ns.sent, [])                      # the shares are still held for the old order until the cancel lands
        self.assertFalse(placed)
        self.assertIn("STALE-STOP XYZ", ns.out.getvalue())

    def test_a_brackets_take_profit_leg_is_never_cancelled(self):
        """Resting above the market on purpose, old enough, price below its limit: everything but 'ours' says cancel. It is not in the trade log as a stop."""
        placed, cancels, ns = self.go([order(oid="tp-leg", limit="3.60", age=30.0)])
        self.assertEqual(cancels, [])

    def test_a_young_order_or_one_the_market_can_still_fill_is_left_alone(self):
        self.assertEqual(self.go([order(age=3.0)])[1], [])
        self.assertEqual(self.go([order(limit="2.50")])[1], [])

    def test_an_order_we_cannot_read_fully_is_left_alone(self):
        self.assertEqual(self.go([{"id": "stop1", "symbol": "XYZ", "side": "sell"}])[1], [])

    def test_a_failed_cancel_does_not_stop_the_pass_and_the_next_one_tries_again(self):
        placed, cancels, ns = self.go([order()], cancel=False, passes=2)
        self.assertEqual(cancels, ["stop1", "stop1"])
        self.assertIn("STALE-STOP", ns.out.getvalue())

    def test_once_the_order_is_gone_the_next_pass_places_a_fresh_stop_at_the_current_price(self):
        placed, cancels, ns = self.go([])                 # the cancel landed: no resting order any more
        self.assertTrue(placed)
        self.assertEqual(cancels, [])
        self.assertEqual([(o["side"], o["symbol"], o["type"]) for o in ns.sent], [("sell", "XYZ", "limit")])
        self.assertAlmostEqual(float(ns.sent[0]["limit_price"]), 2.70 * 0.98, places=2)      # 98% of the CURRENT price

    def test_without_a_price_for_the_symbol_nothing_is_cancelled(self):
        placed, cancels, ns = self.go([order()], prices={"ABC": 1.0})
        self.assertEqual(cancels, [])


class BlindPriceAlert(unittest.TestCase):
    def setUp(self):
        bot._BLIND_STREAK.clear()
        self.addCleanup(bot._BLIND_STREAK.clear)

    def passes(self, n, prices=None, positions=None, orders=(), seed=None, between=None):
        sent = []
        with sandbox(broker(positions=positions or [XYZ], orders=orders), prices=prices) as ns:
            if seed is not None:
                json.dump(seed, open("outage.json", "w"))
            with mock.patch.object(bot, "send_email", lambda s, b: sent.append((s, b)) or True):
                for i in range(n):
                    if between:
                        between(i)
                    bot.exit_pass(ET_NOW, set())
            state = json.load(open("outage.json")) if os.path.exists("outage.json") else {}
        return sent, state, ns

    def test_three_blind_passes_in_a_row_send_one_email_naming_the_stock(self):
        sent, state, ns = self.passes(3)
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertIn("XYZ", subject)
        self.assertIn("NOT evaluated", body)
        self.assertIn("Nothing was sold", body)
        (subject + body).encode("ascii")
        self.assertIn("blind_alert_utc", state)

    def test_two_blind_passes_are_not_enough(self):
        self.assertEqual(self.passes(2)[0], [])

    def test_a_healthy_pass_in_between_resets_the_count(self):
        prices = [None, None, {"XYZ": 2.95}, None, None]                  # blind, blind, priced (no stop at 2.95), blind, blind: never 3 in a row
        sent = []
        with sandbox(broker(positions=[XYZ]), prices=None) as ns:
            with mock.patch.object(bot, "send_email", lambda s, b: sent.append(s) or True):
                for p in prices:
                    with mock.patch.object(bot, "alpaca_latest_multi", (lambda syms, p=p: dict(p or {}))):
                        bot.exit_pass(ET_NOW, set())
        self.assertEqual(sent, [])

    def test_a_long_gap_emails_once_not_every_pass(self):
        sent, state, ns = self.passes(10)
        self.assertEqual(len(sent), 1)

    def test_the_next_run_inside_the_back_off_stays_quiet_because_the_state_is_on_disk(self):
        """A new process every 15 minutes: the streak starts again at 1, but outage.json remembers the last email."""
        recent = (NOW_UTC - timedelta(minutes=15)).isoformat(timespec="seconds")
        sent, state, ns = self.passes(4, seed={"blind_alert_utc": recent})
        self.assertEqual(sent, [])

    def test_after_the_back_off_a_continuing_gap_emails_again(self):
        old = (NOW_UTC - timedelta(hours=3)).isoformat(timespec="seconds")
        sent, state, ns = self.passes(3, seed={"blind_alert_utc": old})
        self.assertEqual(len(sent), 1)

    def test_prices_returning_re_arms_the_alert(self):
        recent = (NOW_UTC - timedelta(minutes=15)).isoformat(timespec="seconds")
        sent, state, ns = self.passes(1, prices={"XYZ": 2.95}, seed={"blind_alert_utc": recent})
        self.assertNotIn("blind_alert_utc", state)

    def test_it_does_not_disturb_the_other_outage_state(self):
        seed = {"unreachable_since": "2026-10-28T13:00:00+00:00", "last_alert_utc": "2026-10-28T13:00:00+00:00"}
        sent, state, ns = self.passes(3, seed=seed)
        self.assertEqual(state["unreachable_since"], seed["unreachable_since"])
        self.assertEqual(state["last_alert_utc"], seed["last_alert_utc"])
        self.assertIn("blind_alert_utc", state)

    def test_a_name_with_a_resting_order_is_not_counted_as_blind(self):
        sent, state, ns = self.passes(4, orders=[{"symbol": "XYZ", "side": "sell", "type": "limit", "status": "new"}])
        self.assertEqual(sent, [])

    def test_an_email_that_fails_never_costs_the_pass(self):
        with sandbox(broker(positions=[XYZ]), prices=None) as ns:
            with mock.patch.object(bot, "send_email", side_effect=OSError("smtp down")):
                for _ in range(3):
                    bot.exit_pass(ET_NOW, set())
            self.assertIn("blind-price alert failed", ns.out.getvalue())

    def test_the_pass_still_sells_nothing_it_cannot_price(self):
        sent, state, ns = self.passes(3)
        self.assertEqual(ns.sent, [])                                      # no price, no sale: unchanged


if __name__ == "__main__":
    unittest.main()
