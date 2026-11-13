"""Arm A: the defects a cold review (2026-10-28, four read-only reviewers, every claim reproduced against the REAL functions) found in
the live-money engine, pinned so they cannot come back. Before this file NOTHING in tests/ drove run_bot(), exit_pass(), the __main__
shield, alpaca_position_gone or load_holds, which is why a careful read of that code had not caught any of this.

Each class names the failure it pins. The harness runs the real functions with only IO replaced: the broker is a router that answers
the paths the bot asks for, requests is blocked, orders are recorded and never sent, and the working directory is a throwaway folder
(run_bot writes holds.json, trade_log.jsonl, status.json into it).

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import itertools
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

requests = bot.requests
ET_NOW = datetime(2026, 10, 28, 10, 30, tzinfo=bot.ET_TZ)
ACCOUNT = {"cash": "50.00", "equity": "225.00", "last_equity": "225.00", "account_number": "PA0000008736"}
SPY = {"symbol": "SPY", "qty": "0.14", "qty_available": "0.14", "avg_entry_price": "600", "market_value": "84.0"}


def pos(sym, qty, avg, last, available=None):
    return {"symbol": sym, "qty": str(qty), "qty_available": str(qty if available is None else available),
            "avg_entry_price": str(avg), "market_value": str(round(qty * last, 2))}


def e404(sym=None):
    return {"code": 40410000, "message": "position does not exist"}


def e429(sym=None):
    return {"code": 42910000, "message": "rate limit exceeded"}


def e500(sym=None):
    return {"code": 50010000, "message": "internal server error"}


def held(sym):
    return {"symbol": sym, "qty": "10", "qty_available": "10", "avg_entry_price": "3.0"}


def refuse(*a, **k):
    raise RuntimeError("network blocked in test")


def broker(positions=(), orders=(), per_symbol=e404, account=None):
    def get(path, _tries=3):
        if path == "/v2/account":
            return dict(ACCOUNT, **(account or {}))
        if path.startswith("/v2/account/activities"):
            return []
        if path == "/v2/positions":
            return list(positions)
        if path.startswith("/v2/positions/"):
            return per_symbol(path.rsplit("/", 1)[1])
        if path.startswith("/v2/orders"):
            return list(orders)
        return {"message": "stub"}
    return get


@contextlib.contextmanager
def sandbox(get, prices=None, holds=None, et=ET_NOW, market_open=True, http=None):
    """Real functions, fake world. Yields a namespace with .out (stdout), .sent (orders that would have been POSTed), .dir."""
    ns = types.SimpleNamespace(out=io.StringIO(), sent=[], dir=None, holds_after=None)
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as st:
        os.chdir(d)
        ns.dir = d
        if holds is not None:
            with open("holds.json", "w") as f:
                json.dump(holds, f)

        def data_get(path):
            if prices is not None and "trades/latest" in path:
                return {"trades": {s: {"p": p} for s, p in prices.items()}}
            raise RuntimeError("data API down in test")

        def order(payload):
            ns.sent.append(dict(payload))
            return {"id": "o%d" % len(ns.sent)}

        for name, val in (("alpaca_get", get), ("alpaca_data_get", data_get), ("alpaca_order", order),
                          ("check_market", lambda: (market_open, et)), ("send_email", lambda *a, **k: True),
                          ("_slack", lambda *a, **k: None), ("news_flags", lambda s, m: {}),
                          ("crypto_latest_multi", lambda pairs: {})):
            st.enter_context(mock.patch.object(bot, name, val))
        st.enter_context(mock.patch.object(requests, "get", http or refuse))
        st.enter_context(mock.patch.object(requests, "post", refuse))
        st.enter_context(mock.patch.object(bot.time, "sleep", lambda s: None))
        st.enter_context(contextlib.redirect_stdout(ns.out))
        try:
            yield ns
        finally:
            try:
                with open("holds.json") as f:
                    ns.holds_after = json.load(f)
            except (OSError, ValueError):
                ns.holds_after = None
            os.chdir(cwd)


def holds_file(ns):
    """holds.json as run_bot left it (read before the sandbox's folder is removed)."""
    return ns.holds_after


XYZ_HOLD = {"XYZ": {"ts": "2026-10-20T10:00", "basis": 3.0, "notional": 20.0, "peak": 3.1}}


class PositionGone(unittest.TestCase):
    """alpaca_position_gone promises: True ONLY on an explicit 404 or a zero quantity; on any doubt, NOT gone. It answered True for every
    JSON error body (429, 403, 500) and for {}, so one rate-limited read could erase the holds ledger."""

    def gone(self, body):
        with mock.patch.object(bot, "alpaca_get", lambda path, _tries=3: body):
            return bot.alpaca_position_gone("XYZ")

    def test_a_real_404_is_gone(self):
        self.assertTrue(self.gone(e404()))

    def test_a_zero_quantity_is_gone(self):
        self.assertTrue(self.gone({"symbol": "XYZ", "qty": "0"}))

    def test_a_held_position_is_not_gone(self):
        self.assertFalse(self.gone(held("XYZ")))

    def test_any_other_error_body_is_not_gone(self):
        for body in (e429(), e500(), {"code": 40310000, "message": "forbidden."}, {"message": "forbidden."}, {}):
            self.assertFalse(self.gone(body), body)

    def test_a_body_that_is_not_an_object_is_not_gone(self):
        for body in (None, [], "oops"):
            self.assertFalse(self.gone(body), body)

    def test_an_unreadable_quantity_is_not_gone(self):
        self.assertFalse(self.gone({"symbol": "XYZ", "qty": "n/a"}))

    def test_a_network_error_is_not_gone(self):
        def boom(path, _tries=3):
            raise requests.exceptions.ConnectionError("reset")
        with mock.patch.object(bot, "alpaca_get", boom):
            self.assertFalse(bot.alpaca_position_gone("XYZ"))


class HoldsLedgerSurvives(unittest.TestCase):
    """2026-07-07 lost the whole holds ledger to one empty positions snapshot; the guard built for it believed any error body."""

    def run_with(self, per_symbol, orders=()):
        h = dict(XYZ_HOLD, **{"ABC": dict(XYZ_HOLD["XYZ"])})
        with sandbox(broker(positions=[], orders=orders, per_symbol=per_symbol), holds=h) as ns:
            bot.run_bot()
        return ns, h

    def test_a_real_404_on_an_otherwise_empty_account_clears_a_stale_ledger(self):
        ns, _ = self.run_with(e404)
        self.assertEqual(holds_file(ns), {})
        self.assertIn("stale ledger", ns.out.getvalue())

    def test_a_rate_limited_per_symbol_read_does_not_clear_the_ledger(self):
        for body in (e429, e500):
            ns, h = self.run_with(body)
            self.assertEqual(sorted(holds_file(ns)), sorted(h), body.__name__)
            self.assertIn("corrupt snapshot", ns.out.getvalue())
            self.assertEqual(ns.sent, [])

    def test_a_network_error_on_the_per_symbol_read_does_not_clear_it_either(self):
        def conn(sym):
            raise requests.exceptions.ConnectionError("reset")
        ns, h = self.run_with(conn)
        self.assertEqual(sorted(holds_file(ns)), sorted(h))

    def test_a_429_on_the_confirm_does_not_prune_a_hold_missing_from_one_batch_read(self):
        h = dict(XYZ_HOLD, **{"NVDA": {"ts": "2026-10-20T10:00", "basis": 200.0, "notional": 20.0}})

        def per_symbol(sym):
            return e429() if sym == "NVDA" else held(sym)
        with sandbox(broker(positions=[SPY, pos("XYZ", 10, 3.0, 3.0)], per_symbol=per_symbol), holds=h) as ns:
            bot.run_bot()
        self.assertIn("NVDA", holds_file(ns))
        self.assertIn("XYZ", holds_file(ns))


class HoldWithAnOpenOrder(unittest.TestCase):
    """qty_available excludes shares reserved by an open order, so a hold whose shares are all in a resting sell is absent from the
    positions map. run_bot indexed positions[s] for every hold and died with KeyError at the top of every cycle."""

    def test_a_hold_fully_reserved_by_a_resting_sell_does_not_crash_the_cycle(self):
        tied = pos("XYZ", 10, 3.0, 3.0, available=0)
        orders = [{"symbol": "XYZ", "side": "sell", "type": "limit", "status": "new"}]
        with sandbox(broker(positions=[SPY, tied], orders=orders, per_symbol=held), holds=dict(XYZ_HOLD)) as ns:
            bot.run_bot()                                    # raised KeyError('XYZ') before
        self.assertIn("XYZ", holds_file(ns))

    def test_a_hold_whose_entry_order_is_still_resting_is_not_pruned(self):
        # HOLD-BUY of a sub-$5 name goes out as a marketable limit and writes holds.json at ORDER time. Fifteen minutes later it has not
        # filled: no position, the per-symbol read is a real 404, and the entry order is open. The prune dropped it anyway.
        h = {"XYZ": {"ts": "2026-10-28T10:15", "basis": 4.0, "notional": 20.0}}
        orders = [{"symbol": "XYZ", "side": "buy", "type": "limit", "status": "new", "limit_price": "4.08"}]
        with sandbox(broker(positions=[SPY], orders=orders, per_symbol=e404), holds=h) as ns:
            bot.run_bot()
        self.assertIn("XYZ", holds_file(ns))

    def test_the_same_hold_IS_pruned_once_its_order_is_gone_and_the_broker_says_404(self):
        h = {"XYZ": {"ts": "2026-10-28T10:15", "basis": 4.0, "notional": 20.0}}
        with sandbox(broker(positions=[SPY], orders=[], per_symbol=e404), holds=h) as ns:
            bot.run_bot()
        self.assertNotIn("XYZ", holds_file(ns))


def run_main(run_bot_impl, *, market_open=True, get=None, http=None, prices=None, real_exit_pass=False, holds=None):
    """Execute the REAL __main__ block verbatim with a fake clock. Returns (exception or None, exit_pass call count, stdout, orders).
    exit_pass is a counter unless real_exit_pass is set; run_bot is the real one unless run_bot_impl is given."""
    with open(os.path.join(ROOT, "alpaca_bot.py"), encoding="utf-8") as f:
        src = f.read()
    tail = src[src.index('if __name__ == "__main__":'):]
    calls = []
    clock = itertools.count(0, 200)

    def fake_exit_pass(_et, alerted):
        calls.append(1)
        return False
    g = dict(vars(bot))
    g.update({"__name__": "__main__", "outage_note_blind": lambda: None,
              "outage_note_contact": lambda: None, "time": types.SimpleNamespace(time=lambda: next(clock), sleep=lambda s: None)})
    if not real_exit_pass:
        g["exit_pass"] = fake_exit_pass
    if run_bot_impl is not None:
        g["run_bot"] = run_bot_impl
    err = None
    with sandbox(get or broker(), market_open=market_open, http=http, prices=prices, holds=holds) as ns:
        g["check_market"] = bot.check_market
        try:
            exec(compile(tail, "alpaca_bot.py(main block)", "exec"), g)
        except BaseException as e:                       # noqa: BLE001 - the point is to see what escapes
            err = e
    return err, len(calls), ns.out.getvalue(), ns.sent


class HoldWithAnOpenOrderEndToEnd(unittest.TestCase):
    """The same poison pill seen from the job: with the hold's shares reserved by a resting sell, the whole job died before any stop
    could run, so a separate trade position at -20% (stop is -7%) was never sold."""

    def test_a_trade_position_at_minus_20_percent_is_still_sold_beside_it(self):
        tied = pos("XYZ", 10, 3.0, 3.0, available=0)
        abc = pos("ABC", 10, 10.0, 8.0)
        orders = [{"symbol": "XYZ", "side": "sell", "type": "limit", "status": "new"}]
        err, passes, out, sent = run_main(None, get=broker(positions=[SPY, tied, abc], orders=orders, per_symbol=held),
                                          prices={"ABC": 8.0, "XYZ": 3.0}, real_exit_pass=True, holds=dict(XYZ_HOLD))
        self.assertIsNone(err)
        self.assertEqual([(o["side"], o["symbol"]) for o in sent], [("sell", "ABC")])

    def test_control_with_the_hold_fully_available_the_same_sale_happens(self):
        free = pos("XYZ", 10, 3.0, 3.0)
        abc = pos("ABC", 10, 10.0, 8.0)
        err, passes, out, sent = run_main(None, get=broker(positions=[SPY, free, abc], per_symbol=held),
                                          prices={"ABC": 8.0, "XYZ": 3.0}, real_exit_pass=True, holds=dict(XYZ_HOLD))
        self.assertIsNone(err)
        self.assertEqual([(o["side"], o["symbol"]) for o in sent], [("sell", "ABC")])


class _Resp502:
    status_code = 502
    text = "<html>502 Bad Gateway</html>"

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)


class _Resp429:
    status_code = 429

    def json(self):
        return e429()


class NetworkShield(unittest.TestCase):
    """The shield exists so that when Alpaca misbehaves the 60-second protective loop STILL runs (no stop orders exist at the broker).
    It caught only ConnectionError and Timeout, so a gateway HTML page or a JSON error body on the first read skipped the loop for a
    whole window. A real crash must still fail the job (red X), after the loop has run."""

    def crash(self, exc):
        def run_bot():
            raise exc
        return run_bot

    def test_a_non_network_crash_still_runs_the_protective_passes(self):
        for exc in (KeyError("equity"), ValueError("Expecting value"), AttributeError("list has no get")):
            err, passes, out, _ = run_main(self.crash(exc))
            self.assertGreaterEqual(passes, 2, repr(exc))

    def test_and_the_job_still_fails_afterwards(self):
        err, passes, out, _ = run_main(self.crash(KeyError("equity")))
        self.assertIsInstance(err, KeyError)
        self.assertGreaterEqual(passes, 1)
        self.assertIn("CRASHED", out)

    def test_a_crash_with_the_market_closed_still_fails_the_job(self):
        err, passes, _, _ = run_main(self.crash(KeyError("equity")), market_open=False)
        self.assertIsInstance(err, KeyError)
        self.assertEqual(passes, 0)

    def test_a_network_error_runs_the_loop_and_finishes_normally(self):
        err, passes, out, _ = run_main(self.crash(requests.exceptions.ConnectionError("reset")))
        self.assertIsNone(err)
        self.assertGreaterEqual(passes, 2)
        self.assertIn("unreachable", out)

    def test_a_clean_cycle_runs_the_loop_and_finishes_normally(self):
        err, passes, _, _ = run_main(lambda: None)
        self.assertIsNone(err)
        self.assertGreaterEqual(passes, 2)

    def test_with_the_real_run_bot_an_html_502_page_still_leaves_the_stops_watched(self):
        err, passes, out, _ = run_main(None, get=bot.alpaca_get, http=lambda *a, **k: _Resp502())
        self.assertGreaterEqual(passes, 2)
        self.assertIsInstance(err, ValueError)               # JSONDecodeError: the job still goes red

    def test_with_the_real_run_bot_a_429_json_body_still_leaves_the_stops_watched(self):
        err, passes, out, _ = run_main(None, get=bot.alpaca_get, http=lambda *a, **k: _Resp429())
        self.assertGreaterEqual(passes, 2)
        self.assertIsInstance(err, KeyError)


class ExitPassBlindness(unittest.TestCase):
    """exit_pass skipped every position without a word when the price feed returned nothing, so the minute-by-minute stop check
    reported calm while evaluating nothing. Behaviour is unchanged (it cannot sell on a price it does not have); the pass now says so."""

    def go(self, prices, positions=None, orders=()):
        positions = positions if positions is not None else [SPY, pos("ABC", 10, 10.0, 8.0)]
        with sandbox(broker(positions=positions, orders=orders), prices=prices) as ns:
            placed = bot.exit_pass(ET_NOW, set())
        return placed, ns

    def test_a_healthy_feed_sells_a_minus_20_percent_trade_position_and_says_nothing_about_blindness(self):
        placed, ns = self.go({"ABC": 8.0})
        self.assertTrue(placed)
        self.assertEqual([(o["side"], o["symbol"]) for o in ns.sent], [("sell", "ABC")])
        self.assertNotIn("NOT evaluated", ns.out.getvalue())

    def test_a_dead_feed_sells_nothing_but_names_what_it_could_not_check(self):
        placed, ns = self.go(None)
        self.assertFalse(placed)
        self.assertEqual(ns.sent, [])
        out = ns.out.getvalue()
        self.assertIn("NOT evaluated", out)
        self.assertIn("ABC", out)

    def test_a_feed_missing_only_one_name_names_only_that_one(self):
        placed, ns = self.go({"ABC": 9.5}, positions=[SPY, pos("ABC", 10, 10.0, 9.5), pos("DEF", 5, 20.0, 20.0)])
        out = ns.out.getvalue()
        self.assertIn("DEF", out)
        self.assertNotIn("ABC", out)

    def test_a_name_with_a_resting_order_is_not_reported_as_unchecked(self):
        orders = [{"symbol": "ABC", "side": "sell", "type": "limit", "status": "new"}]
        placed, ns = self.go(None, orders=orders)
        self.assertNotIn("ABC", ns.out.getvalue())

    def test_index_etfs_are_not_priced_here_and_are_not_reported(self):
        placed, ns = self.go(None, positions=[SPY])
        self.assertNotIn("NOT evaluated", ns.out.getvalue())


class HoldsFile(unittest.TestCase):
    """A holds.json that cannot be read made load_holds return {} with no log line, which silently reclassifies every hold as a trade
    position (-7% stop instead of -25%). save_holds wrote in place, so a crash mid-write is how such a file gets made."""

    def test_an_unreadable_ledger_is_loud(self):
        for text in ("{", "<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> x\n", ""):
            with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
                cwd = os.getcwd()
                os.chdir(d)
                try:
                    with open("holds.json", "w") as f:
                        f.write(text)
                    got = bot.load_holds()
                finally:
                    os.chdir(cwd)
            self.assertEqual(got, {})
            self.assertIn("holds.json", out.getvalue())
            self.assertIn("UNREADABLE", out.getvalue())

    def test_a_missing_ledger_is_quiet_and_empty(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                got = bot.load_holds()
            finally:
                os.chdir(cwd)
        self.assertEqual(got, {})
        self.assertEqual(out.getvalue(), "")

    def test_a_save_that_fails_midway_leaves_the_old_ledger_intact(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                bot.save_holds({"AAA": {"basis": 1.0}})
                with mock.patch.object(bot.json, "dump", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        bot.save_holds({"BBB": {"basis": 2.0}})
                with open("holds.json") as f:
                    kept = json.load(f)
                leftovers = [n for n in os.listdir(".") if n != "holds.json"]
            finally:
                os.chdir(cwd)
        self.assertEqual(kept, {"AAA": {"basis": 1.0}})
        self.assertEqual(leftovers, [])

    def test_a_good_save_round_trips_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                bot.save_holds({"AAA": {"basis": 1.0}, "BBB": {"basis": 2.0}})
                got = bot.load_holds()
                names = os.listdir(".")
            finally:
                os.chdir(cwd)
        self.assertEqual(got, {"AAA": {"basis": 1.0}, "BBB": {"basis": 2.0}})
        self.assertEqual(names, ["holds.json"])


class OpenOrdersRead(unittest.TestCase):
    """alpaca_open_orders is the guard that stops a resting limit buy being bought again. Any error reply silently disabled it.
    Behaviour is unchanged (the next cycle must still run); it now says the guard is off."""

    def read(self, body):
        with mock.patch.object(bot, "alpaca_get", lambda path, _tries=3: body), contextlib.redirect_stdout(io.StringIO()) as out:
            got = bot.alpaca_open_orders()
        return got, out.getvalue()

    def test_healthy_read_is_quiet(self):
        got, out = self.read([{"symbol": "PLUG"}, {"symbol": "SPY"}])
        self.assertEqual(got, {"PLUG", "SPY"})
        self.assertEqual(out, "")

    def test_an_error_object_says_the_double_buy_guard_is_off(self):
        got, out = self.read(e429())
        self.assertEqual(got, set())
        self.assertIn("open orders", out)

    def test_a_network_error_says_it_too(self):
        def boom(path, _tries=3):
            raise requests.exceptions.ConnectionError("reset")
        with mock.patch.object(bot, "alpaca_get", boom), contextlib.redirect_stdout(io.StringIO()) as out:
            got = bot.alpaca_open_orders()
        self.assertEqual(got, set())
        self.assertIn("open orders", out.getvalue())


if __name__ == "__main__":
    unittest.main()
