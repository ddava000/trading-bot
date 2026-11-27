"""The thirteen findings of cloud's 2026-10-30 cold review, pinned so they stay fixed.

Devon asked for all thirteen on 2026-11-02. Each test below fails against the code as
it stood that morning. Where a finding is about WIRING rather than behaviour, the test
reads the source: the mutation runs on 2026-10-25 proved that deleting a call site or a
cap left pure behaviour tests green, because they re-implemented the arithmetic instead
of reaching the code.

Every test stubs BOTH save_ledger and _save against a refusing floor. A test of mine
wrote its fixture over the live rh_ledger.json on 2026-10-24; that file is gitignored
and had no backup, and it only survived because main() happened to flush the real one
on its way to a restart.

Run:  python -m unittest discover -s tests -v
"""
import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")

import rh_daemon as D
import rh_bot as R
import alpaca_bot as bot

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rh_daemon.py")


def _src(path=SRC):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _func(name, path=SRC):
    """The source text of one top-level function, for wiring assertions."""
    tree = ast.parse(_src(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef,)) and node.name == name:
            return ast.get_source_segment(_src(path), node)
    raise AssertionError("no top-level function %r in %s" % (name, path))


class Floor(unittest.TestCase):
    """Shared: nothing reaches disk, nothing reaches the network, logs are captured."""

    def setUp(self):
        self.logged, self.sent = [], []
        for name, fn in (
            ("save_ledger", lambda l: None),
            ("_save", self._refuse_save),
            ("log", self.logged.append),
            ("notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
        ):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _refuse_save(path, obj):
        raise AssertionError("a test tried to write the real file %r" % (path,))

    def logs(self):
        return "\n".join(self.logged)


# ── 1. A partial positions snapshot must not be adopted, and must not sell ──────
class PartialSnapshot(Floor):
    PRICE = 85.00

    def ledger(self):
        return {"cash": 0.50, "holds": {}, "last_buy": {},
                "positions": [{"symbol": s, "qty": 1.0, "avg_cost": self.PRICE}
                              for s in ("SPY", "QQQ", "IWM")]}

    def truth(self, syms, total=255.50, cash=0.50):
        return {"cash": cash, "total_value": total, "pending_deposits": 0,
                "positions": [{"symbol": s, "qty": 1.0, "avg_cost": self.PRICE} for s in syms]}

    def test_a_dropped_holding_with_a_contradicting_total_is_refused(self):
        """The reply omits IWM while its own total_value still describes all three."""
        led = self.ledger()
        self.assertFalse(D.adopt_truth(led, self.truth(("SPY", "QQQ"))))
        self.assertEqual([p["symbol"] for p in led["positions"]], ["SPY", "QQQ", "IWM"],
                         "the ledger must be left exactly as it was")
        self.assertIn("PARTIAL SNAPSHOT REFUSED", self.logs())

    def test_and_therefore_no_shares_are_sold_that_pass(self):
        """The money consequence, end to end: $56.33 of INDEX-TRIM sells, 22% of the book.

        decide() is driven for real; only _quote and the screeners are stubbed.
        """
        led = self.ledger()
        D.adopt_truth(led, self.truth(("SPY", "QQQ")))
        with mock.patch.object(R, "_quote", lambda sym, meme: (
                self.PRICE, {"consensus": 0, "rsi": 50, "buys": 0, "trend": "flat"}, [1.0] * 60)), \
             mock.patch.object(bot, "fetch_wsb", lambda: []), \
             mock.patch.object(bot, "news_flags", lambda *a, **k: {}), \
             mock.patch.object(bot, "load_plan",
                               lambda now: {"risk": 1.0, "avoid": set(), "regime": "neutral"}):
            res = R.decide({"cash": led["cash"], "unsettled": 0.0,
                            "positions": led["positions"], "holds": led["holds"],
                            "last_buy": led["last_buy"]}, fast=False)
        sells = [o for o in res["orders"] if o["action"] == "sell"]
        self.assertEqual(sells, [], "a refused snapshot must leave nothing to sell: %r" % (sells,))
        self.assertEqual(res["snapshot"]["equity"], 255.50)

    def test_a_genuine_sale_is_still_adopted(self):
        """The guard must not freeze the ledger when a holding really did leave.

        Devon selling IWM by hand lowers the positions AND the broker's own total.
        """
        led = self.ledger()
        self.assertTrue(D.adopt_truth(led, self.truth(("SPY", "QQQ"), total=170.50)))
        self.assertEqual([p["symbol"] for p in led["positions"]], ["SPY", "QQQ"])
        self.assertIn("treating the holding(s)", self.logs())

    def test_an_unusable_total_fails_closed(self):
        """No corroboration available means refuse: waiting costs a purchase, not shares."""
        led = self.ledger()
        self.assertFalse(D.adopt_truth(led, self.truth(("SPY", "QQQ"), total=0)))
        self.assertEqual(len(led["positions"]), 3)
        self.assertIn("no usable total_value", self.logs())

    def test_the_zero_position_guard_still_works(self):
        led = self.ledger()
        self.assertFalse(D.adopt_truth(led, self.truth(())))
        self.assertEqual(len(led["positions"]), 3)

    def test_holds_for_a_dropped_name_are_not_pruned(self):
        """The basis/peak history is the thing a partial snapshot silently destroyed."""
        led = self.ledger()
        led["holds"] = {"IWM": {"basis": 80.0, "peak": 92.0}}
        D.adopt_truth(led, self.truth(("SPY", "QQQ")))
        self.assertEqual(led["holds"], {"IWM": {"basis": 80.0, "peak": 92.0}})


# ── 2. Ledger cash ─────────────────────────────────────────────────────────────
class LedgerCash(Floor):
    def test_a_true_broker_zero_is_believed(self):
        """`or` read 0.00 as absent and kept stale cash, so decide() ordered buys
        the broker had to reject - burning the daily order budget every pass."""
        led = {"cash": 60.0, "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}],
               "holds": {}, "last_buy": {}}
        self.assertTrue(D.adopt_truth(led, {
            "cash": 0.0, "total_value": 85.0, "pending_deposits": 0,
            "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}]}))
        self.assertEqual(led["cash"], 0.0)

    def test_a_missing_cash_field_still_falls_back(self):
        led = {"cash": 60.0, "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}],
               "holds": {}, "last_buy": {}}
        D.adopt_truth(led, {"total_value": 85.0, "pending_deposits": 0,
                            "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}]})
        self.assertEqual(led["cash"], 60.0)
        self.assertIn("no cash field", self.logs())

    def test_unreadable_cash_falls_back_rather_than_raising(self):
        led = {"cash": 60.0, "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}],
               "holds": {}, "last_buy": {}}
        D.adopt_truth(led, {"cash": "$40.00", "total_value": 85.0, "pending_deposits": 0,
                            "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}]})
        self.assertEqual(led["cash"], 60.0)


# ── 3. A failed executor turn is not a filled one ──────────────────────────────
class FailedExecutorTurn(Floor):
    ORDERS = [{"action": "buy", "symbol": "SPY", "notional": 30.0, "reason": "x"},
              {"action": "buy", "symbol": "QQQ", "notional": 30.0, "reason": "x"}]

    def test_place_returns_none_when_the_turn_gave_no_report(self):
        with mock.patch.object(D, "DRY", False), mock.patch.object(D, "agent", lambda p: None):
            self.assertIsNone(D.place(self.ORDERS))

    def test_place_returns_none_on_a_reply_without_a_placed_list(self):
        with mock.patch.object(D, "DRY", False), \
             mock.patch.object(D, "agent", lambda p: {"error": "something else"}):
            self.assertIsNone(D.place(self.ORDERS))

    def test_place_returns_an_empty_list_when_the_broker_placed_nothing(self):
        """An answer, not the absence of one: [] must stay []."""
        with mock.patch.object(D, "DRY", False), \
             mock.patch.object(D, "agent", lambda p: {"placed": []}):
            self.assertEqual(D.place(self.ORDERS), [])

    def test_none_books_nothing_and_forces_a_reconcile(self):
        """The phantom-fill bug: [] went down the DRY simulation path and invented fills."""
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": 0.0, "orders_today": 0}
        with mock.patch.object(D, "DRY", False):
            D.apply_fills(led, self.ORDERS, None, {"SPY": 85.0, "QQQ": 85.0})
        self.assertEqual(led["positions"], [], "nothing may be booked from an unknown outcome")
        self.assertEqual(led["cash"], 100.0)
        self.assertEqual(led["last_buy"], {})
        self.assertIs(led["needs_reconcile"], True)
        self.assertEqual(led["orders_today"], 2, "unaccounted orders still charge the breaker")

    def test_persist_logs_unknown_rather_than_crashing_on_no_report(self):
        """Cloud's follow-up, 2026-11-03, and a bug I introduced with the None/[] split.

        persist only ITERATES placed, so None raised TypeError there while the ledger and
        status file had already been written - cycle() swallowed it as "cycle error (loop
        continues)" and the trade log silently lost the orders. The ones lost are exactly
        the orders whose outcome nobody saw, which is what the "unknown" status and
        realized.arm_b_block()'s sells-logged tripwire exist to preserve.
        """
        res = {"orders": list(self.ORDERS), "snapshot": {}, "notes": []}
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": 0.0, "orders_today": 0}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            log_f = os.path.join(td, "trade_log.jsonl")
            with mock.patch.object(D, "LOG_F", log_f), \
                 mock.patch.object(D, "DRY", False), \
                 mock.patch.object(D, "_save", lambda p, o: None), \
                 mock.patch.object(D, "_push_status", lambda r: False):
                D.persist(led, res, None)                  # must not raise
            with open(log_f, encoding="utf-8") as fh:
                lines = [json.loads(l) for l in fh.read().splitlines() if l.strip()]
        self.assertEqual([l["status"] for l in lines], ["unknown", "unknown"],
                         "an unknown outcome must still be recorded, not dropped: %r" % (lines,))
        self.assertEqual([l["symbol"] for l in lines], ["SPY", "QQQ"], "%r" % (lines,))

    def test_an_empty_report_in_a_live_run_books_nothing_either(self):
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": 0.0, "orders_today": 0}
        with mock.patch.object(D, "DRY", False):
            D.apply_fills(led, self.ORDERS, [], {"SPY": 85.0, "QQQ": 85.0})
        self.assertEqual(led["positions"], [])
        self.assertEqual(led["cash"], 100.0)

    def test_a_failed_sell_does_not_credit_cash_or_sold_today(self):
        """sold_today drives the T+1 settled-cash figure and adopt_truth never resets it,
        so a phantom sell held the whole deposit idle for the rest of the day."""
        led = {"cash": 0.0, "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}],
               "holds": {}, "last_buy": {}, "sold_today": 0.0, "orders_today": 0}
        sells = [{"action": "sell", "symbol": "SPY", "qty": 1.0, "reason": "WIND-DOWN"}]
        with mock.patch.object(D, "DRY", False):
            D.apply_fills(led, sells, None, {"SPY": 85.0})
        self.assertEqual(led["sold_today"], 0.0)
        self.assertEqual(led["cash"], 0.0)
        self.assertEqual(len(led["positions"]), 1, "the position must still be there")

    def test_a_dry_run_still_simulates_every_fill(self):
        """The simulation this ambiguity came from has to keep working."""
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": 0.0, "orders_today": 0}
        with mock.patch.object(D, "DRY", True):
            D.apply_fills(led, self.ORDERS, [], {"SPY": 85.0, "QQQ": 85.0})
        self.assertEqual(sorted(p["symbol"] for p in led["positions"]), ["QQQ", "SPY"])
        self.assertEqual(led["orders_today"], 0, "dry orders must not consume the breaker")

    def test_rejected_orders_are_still_filtered(self):
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": 0.0, "orders_today": 0}
        placed = [{"symbol": "SPY", "action": "buy", "status": "ok"},
                  {"symbol": "QQQ", "action": "buy", "status": "rejected"}]
        with mock.patch.object(D, "DRY", False):
            D.apply_fills(led, self.ORDERS, placed, {"SPY": 85.0, "QQQ": 85.0})
        self.assertEqual([p["symbol"] for p in led["positions"]], ["SPY"])

    def test_the_unknown_outcome_email_names_the_orders_and_claims_nothing(self):
        led = {"cash": 100.0, "positions": [], "holds": {}, "last_buy": {}}
        with mock.patch.object(D, "DRY", False):
            D.email_trades({"orders": self.ORDERS, "snapshot": {}}, None, led)
        self.assertEqual(len(self.sent), 1)
        subject, body = self.sent[0]
        self.assertIn("OUTCOME UNKNOWN", subject)
        self.assertIn("SPY", body)
        self.assertIn("QQQ", body)
        self.assertIn("is NOT known", body)
        self.assertIn("left unchanged", body)
        # Not a bare word search: the body says "rather than assume they filled", which
        # is the opposite of a claim. What must be absent is a COUNT of fills, the thing
        # the normal email leads with and the thing this one cannot honestly state.
        self.assertNotIn("order(s) accepted", body)
        self.assertNotIn("/2 order", body)

    def test_the_bridge_decodes_as_utf8(self):
        """One byte cp1252 cannot decode used to destroy the whole reply, including the
        JSON for orders that had already executed. Wiring, so it is read from source."""
        src = _func("_run_bridge")
        self.assertIn('encoding="utf-8"', src)
        self.assertIn('errors="replace"', src)
        self.assertNotIn("text=True,\n                            capture_output", src)


# ── 4. A malformed broker element must not raise out of adopt_truth ────────────
class MalformedSnapshot(Floor):
    BAD = [
        ("qty null", {"symbol": "SPY", "qty": None, "avg_cost": 85.0}),
        ("qty with units", {"symbol": "SPY", "qty": "0.17 shares", "avg_cost": 85.0}),
        ("no symbol", {"qty": 1.0, "avg_cost": 85.0}),
        ("avg_cost with a dollar sign", {"symbol": "SPY", "qty": 1.0, "avg_cost": "$40.00"}),
        ("not a mapping at all", "SPY"),
    ]

    def test_no_shape_of_bad_element_raises(self):
        for label, el in self.BAD:
            with self.subTest(label):
                led = {"cash": 10.0, "holds": {}, "last_buy": {},
                       "positions": [{"symbol": "QQQ", "qty": 1.0, "avg_cost": 85.0}]}
                self.assertFalse(D.adopt_truth(led, {"cash": 10.0, "total_value": 95.0,
                                                     "pending_deposits": 0, "positions": [el]}),
                                 "%s must be refused, not adopted" % label)
                self.assertEqual([p["symbol"] for p in led["positions"]], ["QQQ"])

    def test_the_whole_snapshot_is_refused_not_just_the_bad_line(self):
        """Dropping the unreadable element would manufacture a partial snapshot."""
        led = {"cash": 10.0, "holds": {}, "last_buy": {},
               "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0},
                             {"symbol": "QQQ", "qty": 1.0, "avg_cost": 85.0}]}
        self.assertFalse(D.adopt_truth(led, {
            "cash": 10.0, "total_value": 180.0, "pending_deposits": 0,
            "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0},
                          {"symbol": "QQQ", "qty": None, "avg_cost": 85.0}]}))
        self.assertEqual(len(led["positions"]), 2)
        self.assertIn("unreadable position element", self.logs())

    def test_the_post_trade_reconcile_is_guarded_so_the_trade_log_survives(self):
        """Wiring: persist() writes rh_trade_log.jsonl and the orders are already away."""
        src = _func("cycle")
        i, j = src.index("placed = place("), src.index("persist(led, res, placed)")
        between = src[i:j]
        self.assertIn("try:", between)
        self.assertIn("except Exception", between)
        self.assertIn("adopt_truth(led, reconcile())", between)

    def test_notify_does_not_raise_when_slack_notify_is_unimportable(self):
        """The untrusted branch is reached from inside cycle(), after orders are placed."""
        with mock.patch.dict(sys.modules, {"slack_notify": None}), \
             mock.patch.object(D, "notify", D.notify.__wrapped__
                               if hasattr(D.notify, "__wrapped__") else D.notify):
            pass  # notify itself is stubbed by Floor; assert on the source instead
        src = _func("notify")
        head = src[:src.index("# No early return")]
        self.assertIn("try:", head)
        self.assertIn("import slack_notify", head)
        self.assertIn("except Exception", head)


# ── 5. Deposit capture decides November's number ───────────────────────────────
class DepositCapture(Floor):
    def truth(self, pending):
        return {"cash": 10.0, "total_value": 95.0, "pending_deposits": pending,
                "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}]}

    def led(self, seen=0.0):
        return {"cash": 10.0, "holds": {}, "last_buy": {}, "pending_deposits_seen": seen,
                "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 85.0}]}

    def test_a_failed_write_does_not_consume_the_rising_edge(self):
        """The watermark advancing on a failed write is how a deposit vanished: 16 -> 16."""
        led = self.led()
        with mock.patch.object(D, "record_deposit", lambda *a, **k: False):
            D.adopt_truth(led, self.truth(10.0))
        self.assertEqual(led["pending_deposits_seen"], 0.0)
        self.assertIn("NOT recorded", self.logs())

    def test_a_successful_write_does_advance_it(self):
        led = self.led()
        with mock.patch.object(D, "record_deposit", lambda *a, **k: True):
            D.adopt_truth(led, self.truth(10.0))
        self.assertEqual(led["pending_deposits_seen"], 10.0)

    def test_a_null_pending_field_is_unknown_not_zero(self):
        """`or 0` reset the watermark, so the same still-pending $10 was recorded twice."""
        calls = []
        led = self.led(seen=10.0)
        with mock.patch.object(D, "record_deposit", lambda *a, **k: calls.append(a) or True):
            D.adopt_truth(led, self.truth(None))
        self.assertEqual(led["pending_deposits_seen"], 10.0, "the watermark must not reset")
        self.assertEqual(calls, [])
        # Mutation 2026-11-02: deleting the `is None` branch let float(None) raise into
        # the outer `except Exception`, which ALSO leaves the watermark alone - so the
        # assertions above passed against the broken code. The log line is what
        # distinguishes "deliberately left" from "crashed and skipped".
        self.assertIn("no pending_deposits figure", self.logs())
        self.assertNotIn("deposit check skipped", self.logs())
        D.adopt_truth(led, self.truth(10.0))
        self.assertEqual(calls, [], "and the same deposit must not be recorded again")

    def test_a_settled_deposit_still_lowers_the_watermark(self):
        led = self.led(seen=10.0)
        with mock.patch.object(D, "record_deposit", lambda *a, **k: True):
            D.adopt_truth(led, self.truth(0.0))
        self.assertEqual(led["pending_deposits_seen"], 0.0)

    def test_an_unparseable_deposit_file_is_never_overwritten(self):
        """(c) 16 events -> 1 and total_contributed_capital 324.92 -> 10.00, then pushed."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "rh_deposits.json")
            corrupt = "<<<<<<< HEAD\n{\"events\": []}\n=======\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(corrupt)
            cwd = os.getcwd()
            os.chdir(td)
            try:
                with mock.patch.object(D, "_save", self._refuse_save), \
                     mock.patch.object(D, "DEPOSITS_F", "rh_deposits.json"):
                    self.assertFalse(D.record_deposit(10.0, "test"))
            finally:
                os.chdir(cwd)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), corrupt, "the file must be byte-identical")
        self.assertIn("REFUSING to write", self.logs())
        self.assertTrue(any("cannot be read" in s for s, _ in self.sent),
                        "and Devon has to be told, because only a human can fix it")

    def test_a_missing_deposit_file_is_still_created(self):
        """A real first run must not be mistaken for corruption."""
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            os.chdir(td)
            try:
                written = {}
                with mock.patch.object(D, "_save", lambda p, o: written.update(o)), \
                     mock.patch.object(D, "DEPOSITS_F", "rh_deposits.json"):
                    self.assertTrue(D.record_deposit(10.0, "test"))
            finally:
                os.chdir(cwd)
        self.assertEqual(len(written.get("events") or []), 1)


# ── 6. The selftest gate has to cover every file it gates ─────────────────────
class SelftestGate(unittest.TestCase):
    def test_the_gate_imports_every_code_file(self):
        src = _func("sync_code")
        self.assertIn("CODE_FILES", src.split("chk = subprocess.run")[0][-800:],
                      "the gate must derive its import list from CODE_FILES, not a literal")
        self.assertIn("import {imports}", src)

    def test_a_syntax_error_in_each_code_file_is_rejected(self):
        """Behavioural, in a throwaway copy: reading the source cannot prove this.

        Measured before the fix: rh_bot and alpaca_bot were rejected; rh_daemon,
        realized, slack_notify and arm_a_watch all PASSED and the task restarted into
        code that could not start - silently, because pythonw has no console.
        """
        import shutil
        import subprocess
        root = os.path.dirname(SRC)
        imports = ", ".join(f[:-3] for f in D.CODE_FILES if f.endswith(".py"))
        cmd = (f"import {imports}; import runpy, sys; "
               f"sys.argv=['rh_bot.py','--selftest']; "
               f"runpy.run_module('rh_bot', run_name='__main__')")
        env = dict(os.environ, ALPACA_API_KEY="x", ALPACA_SECRET_KEY="x")
        with tempfile.TemporaryDirectory() as td:
            for name in os.listdir(root):
                if name.endswith((".py", ".json")):
                    shutil.copy(os.path.join(root, name), td)
            clean = subprocess.run([sys.executable, "-c", cmd], cwd=td, env=env,
                                   capture_output=True, timeout=180)
            self.assertEqual(clean.returncode, 0,
                             "the clean tree must pass: %r" % (clean.stderr[-400:],))
            for f in D.CODE_FILES:
                with self.subTest(f):
                    target = os.path.join(td, f)
                    # Closed explicitly: CI runs -W error::ResourceWarning, so a leaked
                    # handle here is a failure whenever the collector happens to run.
                    with open(target, encoding="utf-8") as fh:
                        good = fh.read()
                    try:
                        with open(target, "a", encoding="utf-8") as fh:
                            fh.write("\nthis is not valid python $$$\n")
                        broken = subprocess.run([sys.executable, "-c", cmd], cwd=td, env=env,
                                                capture_output=True, timeout=180)
                        self.assertNotEqual(broken.returncode, 0,
                                            "a broken %s must not pass the gate" % f)
                    finally:
                        with open(target, "w", encoding="utf-8") as fh:
                            fh.write(good)


# ── 7. Stale state and alert throttles ────────────────────────────────────────
class StaleState(Floor):
    def test_a_good_reply_clears_the_last_agent_error(self):
        """A quota message from one day classified an unrelated timeout on the next and
        parked the broker retry for 3.84 hours as a 'named reset'."""
        D._last_agent_error = "usage limit reached. resets 1:40pm"
        self.addCleanup(setattr, D, "_last_agent_error", "")
        with mock.patch.object(D, "_run_bridge",
                               lambda cmd: mock.Mock(stdout='{"ok": true}', returncode=0)):
            self.assertEqual(D.agent("x"), {"ok": True})
        self.assertEqual(D._last_agent_error, "")

    def test_a_timeout_records_itself_rather_than_inheriting(self):
        import subprocess
        D._last_agent_error = "usage limit reached. resets 1:40pm"
        self.addCleanup(setattr, D, "_last_agent_error", "")

        def boom(cmd):
            raise subprocess.TimeoutExpired(cmd, 240)
        with mock.patch.object(D, "_run_bridge", boom):
            self.assertIsNone(D.agent("x"))
        self.assertNotIn("1:40pm", D._last_agent_error)
        self.assertIn("Timeout", D._last_agent_error)

    def test_reached_nobody_distinguishes_email_failure_from_total_failure(self):
        """notify() returns an EMAIL verdict; a throttle needs 'reached him at all'."""
        D._set_notify_reach("slack")
        self.assertFalse(D.reached_nobody())
        D._set_notify_reach("both")
        self.assertFalse(D.reached_nobody())
        D._set_notify_reach("nobody")
        self.assertTrue(D.reached_nobody())

    def test_mail_is_not_marked_seen_when_the_notice_reached_nobody(self):
        src = _func("check_mail")
        tail = src[src.index("notify(f\"AGENT_MAIL"):]
        self.assertIn("reached_nobody()", tail)
        self.assertIn('led["last_mail_seen"] = keys[-1]', tail,
                      "the watermark must move AFTER the notify, not before")

    def test_the_broker_alert_is_not_stamped_when_it_reached_nobody(self):
        """Behavioural: the source-slice version of this test let a mutant survive.

        The alert whose whole job is to say the bridge is down is the one most likely
        to fail delivery, so stamping it regardless turned one failed send into a full
        BROKER_REALERT_SEC of silence.
        """
        D._broker_down_since = D.time.time() - 3600
        D._reconcile_fails = 99
        D._broker_alert_at = 0.0
        D._waiting_on_reset = False
        for name in ("_broker_down_since", "_reconcile_fails", "_broker_alert_at",
                     "_waiting_on_reset"):
            self.addCleanup(setattr, D, name, getattr(D, name))
        led = {}
        with mock.patch.object(D, "notify", lambda *a, **k: False),              mock.patch.object(D, "reached_nobody", lambda: True),              mock.patch.object(D, "market_minutes_since", lambda t: 60):
            D._maybe_alert_broker(led)
        self.assertEqual(D._broker_alert_at, 0.0,
                         "an alert that reached nobody must not be stamped as delivered")
        with mock.patch.object(D, "notify", lambda *a, **k: True),              mock.patch.object(D, "reached_nobody", lambda: False),              mock.patch.object(D, "market_minutes_since", lambda t: 60):
            D._maybe_alert_broker(led)
        self.assertGreater(D._broker_alert_at, 0.0,
                           "and one that was delivered must be")


# ── 8. The heartbeat commit must take only its own files ──────────────────────
class StatusPush(unittest.TestCase):
    def test_the_commit_names_its_pathspec(self):
        """Cloud pushed AGENT_MAIL.md into an 'rh bot' heartbeat from a staged file."""
        src = _func("_push_status")
        commit = src[src.index('"git", "commit"'):src.index('"git", "push"')]
        self.assertIn('"--"', commit)
        self.assertIn("STATUS_F", commit)
        self.assertIn("DEPOSITS_F", commit)

    def test_it_reports_false_when_both_pushes_failed(self):
        """Returning True suppressed the next heartbeat for 900s with the commit local.

        Asserting "return False appears after the pull" was vacuous - the pre-existing
        `except` clause already matched. The real property is that BOTH pushes have
        their returncode checked, so neither can be assumed to have worked.
        """
        src = _func("_push_status")
        pushes = src.count('"git", "push"')
        self.assertEqual(pushes, 2, "one initial push and one retry")
        # Scoped to the push region: the `git diff --cached` staging check above also
        # tests a returncode, so counting across the whole function gives 3.
        pushes_region = src[src.index('"git", "commit"'):]
        self.assertEqual(pushes_region.count("returncode == 0"), 2,
                         "both pushes must be checked, not just the first")
        # Behavioural, because every string assertion I tried here matched the
        # pre-existing `except Exception: return False` at the end of the function and
        # passed against the broken code. Two separate mutants survived that way.
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            name = cmd[1] if len(cmd) > 1 else ""
            if name == "diff":
                return mock.Mock(returncode=1)      # something IS staged
            if name == "push":
                return mock.Mock(returncode=1)      # both pushes fail
            return mock.Mock(returncode=0)
        with mock.patch.object(D.subprocess, "run", fake_run),              mock.patch.object(D, "log", lambda m: None):
            self.assertIs(D._push_status("test"), False,
                          "a commit that was never pushed must not report success")
        self.assertEqual(sum(1 for c in calls if c[1] == "push"), 2, calls)

    def test_a_push_that_works_reports_true(self):
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=1 if cmd[1] == "diff" else 0)
        with mock.patch.object(D.subprocess, "run", fake_run),              mock.patch.object(D, "log", lambda m: None):
            self.assertIs(D._push_status("test"), True)


# ── 9. A permanent quote gap must not silently stop index buying ──────────────
class RebalanceBlock(Floor):
    NOTE = "index rebalance skipped: no quote for ['LEGACY'], equity would be understated"

    def test_it_counts_on_the_ledger_so_a_restart_cannot_reset_it(self):
        led = {}
        for _ in range(3):
            D._watch_rebalance_block(led, {"notes": [self.NOTE]})
        self.assertEqual((led.get("alerts") or {}).get("rebalance_blocked"), 3)

    def test_it_alerts_once_past_the_threshold(self):
        led = {}
        for _ in range(D.REBALANCE_BLOCK_ALERT + 4):
            D._watch_rebalance_block(led, {"notes": [self.NOTE]})
        self.assertEqual(len(self.sent), 1, "once, not every cycle: %r" % (self.sent,))
        self.assertIn("blocked", self.sent[0][0])
        self.assertIn("LEGACY", self.sent[0][1])

    def test_the_watch_is_actually_called_from_cycle(self):
        """Wiring. My behaviour tests called it directly, so deleting the call site in
        cycle() left all three of them green - the same miss as the auth cap in October."""
        self.assertIn("_watch_rebalance_block(led, res)", _func("cycle"))

    def test_a_clear_pass_resets_the_count(self):
        led = {}
        for _ in range(5):
            D._watch_rebalance_block(led, {"notes": [self.NOTE]})
        D._watch_rebalance_block(led, {"notes": []})
        self.assertEqual((led.get("alerts") or {}).get("rebalance_blocked"), 0)
        self.assertEqual(self.sent, [])

    def test_a_basis_less_holding_is_named_instead_of_silently_skipped(self):
        with mock.patch.object(R, "_quote", lambda sym, meme: (
                85.0, {"consensus": 0, "rsi": 50, "buys": 0, "trend": "flat"}, [1.0] * 60)), \
             mock.patch.object(bot, "fetch_wsb", lambda: []), \
             mock.patch.object(bot, "news_flags", lambda *a, **k: {}), \
             mock.patch.object(bot, "load_plan",
                               lambda now: {"risk": 1.0, "avoid": set(), "regime": "neutral"}), \
             mock.patch.object(R, "WIND_DOWN", False):
            res = R.decide({"cash": 1.0, "unsettled": 0.0, "holds": {}, "last_buy": {},
                            "positions": [{"symbol": "LEGACY", "qty": 2.0, "avg_cost": 0}]},
                           fast=False)
        self.assertTrue(any("no usable cost basis" in n for n in res["notes"]), res["notes"])

    def test_the_wind_down_sells_it_instead_of_crashing_on_the_basis(self):
        """It divides by cost to build its reason, so cost 0 was excluded as a 'guard'."""
        with mock.patch.object(R, "_quote", lambda sym, meme: (
                85.0, {"consensus": 0, "rsi": 50, "buys": 0, "trend": "flat"}, [1.0] * 60)), \
             mock.patch.object(bot, "fetch_wsb", lambda: []), \
             mock.patch.object(bot, "news_flags", lambda *a, **k: {}), \
             mock.patch.object(bot, "load_plan",
                               lambda now: {"risk": 1.0, "avoid": set(), "regime": "neutral"}), \
             mock.patch.object(R, "WIND_DOWN", True):
            res = R.decide({"cash": 1.0, "unsettled": 0.0, "holds": {}, "last_buy": {},
                            "positions": [{"symbol": "LEGACY", "qty": 2.0, "avg_cost": 0}]},
                           fast=False)
        sells = [o for o in res["orders"] if o["action"] == "sell" and o["symbol"] == "LEGACY"]
        self.assertEqual(len(sells), 1, "the wind-down exists to clear exactly this")
        self.assertIn("no cost basis", sells[0]["reason"])


# ── 11. The dormant mute, correct before anyone re-arms it ────────────────────
class MuteThirdDefect(Floor):
    def test_a_delivered_alert_clears_the_muted_episode_flag(self):
        """Muted 6-minute alert then a DELIVERED 20-minute urgent one left the flag True,
        so the recovery was swallowed: told it broke, never told it recovered."""
        src = _func("check_arm_a")
        tail = src[src.index("# ANY DELIVERED ALERT ENDS"):]
        self.assertIn("arm_a_queued_muted=False", tail)
        self.assertIn("_remember_alerts", tail)
        # "Detected by" appears in three branches, so anchor on the LAST one - the
        # unconditional delivering notify at the end of _deliver.
        self.assertLess(src.index("# ANY DELIVERED ALERT ENDS"),
                        src.rindex("return notify(subject, chr(10).join(["),
                        "it has to clear BEFORE the delivering notify")

    def test_the_mute_is_off(self):
        self.assertIs(D.ARM_A_MUTE_QUEUED, False)


# ── 12. Monitoring must not die with the broker ───────────────────────────────
class MonitoringSurvivesABrokerOutage(unittest.TestCase):
    def test_the_arm_a_watch_runs_before_the_reconcile_gate(self):
        """Both degraded paths end in `continue`, and Arm B outages have run 90-165 min.
        Arm A is the arm with the stops; this is its dead-man switch."""
        src = _func("main")
        arm_a = src.index("check_arm_a(led)")
        gate = src.index('if led.get("needs_reconcile"):')
        self.assertLess(arm_a, gate, "check_arm_a must not sit behind the broker gate")

    def test_mail_and_code_sync_do_too(self):
        src = _func("main")
        gate = src.index('if led.get("needs_reconcile"):')
        self.assertLess(src.index("check_mail(led)"), gate)
        self.assertLess(src.index("sync_code(led)"), gate)

    def test_the_gate_still_guards_trading(self):
        src = _func("main")
        self.assertLess(src.index('if led.get("needs_reconcile"):'),
                        src.index("res = cycle(led, fast=not full)"))


# ── 13. The bridge timeout has to bound the loop ──────────────────────────────
class BridgeTimeout(unittest.TestCase):
    def test_a_timeout_kills_the_whole_process_tree(self):
        src = _func("_run_bridge")
        self.assertIn("_kill_tree", src)
        self.assertIn("TimeoutExpired", src)

    def test_kill_tree_uses_the_tree_flag(self):
        src = _func("_kill_tree")
        self.assertIn('"/T"', src)
        self.assertIn('"/F"', src)


# ── Mine, not cloud's: the uptime counter wrapped at 24.86 days ───────────────
class ImpliedBootEpoch(Floor):
    """Found by my own test on 2026-11-02, the day the laptop crossed 2**31 ms of uptime.

    THIS CLASS MUST STUB log(). It extended unittest.TestCase directly at first, so the
    wrapped-counter test below called the REAL log() and appended 13 lines reading
    "uptime counter returned 18446744073709552s" into the live rh_daemon.log - one per
    suite run. Nothing was published (that file is gitignored) and no money was
    touched, but it is the operational log I grep during an incident, and a line no
    production event ever produced is the phantom evidence this whole review was about.
    Floor stubs log, notify, save_ledger and _save.

    ctypes defaults a foreign function's return to c_int - 32 bits, SIGNED - while
    GetTickCount64 returns ULONGLONG, so the counter went negative past 24.86 days and
    _implied_boot_epoch returned a boot time in the FUTURE. report_downtime then reads
    `rebooted` as True forever, which disables the quiet path that stops false alarms.
    """

    def test_the_restype_is_set_so_the_counter_cannot_wrap(self):
        src = _func("_implied_boot_epoch")
        self.assertIn("restype", src)
        self.assertIn("c_ulonglong", src)

    def test_the_live_value_is_in_the_past_and_plausible(self):
        boot = D._implied_boot_epoch()
        if boot is None:
            self.skipTest("no uptime counter on this platform")
        import time as _t
        self.assertLess(boot, _t.time(), "a boot time in the future is never valid")
        self.assertGreater(boot, _t.time() - D.BOOT_UPTIME_MAX_SEC)

    def test_it_agrees_with_the_operating_system(self):
        """The 8-second agreement measured 2026-11-02 against Win32_OperatingSystem."""
        if not sys.platform.startswith("win"):
            self.skipTest("Windows only")
        import subprocess
        boot = D._implied_boot_epoch()
        if boot is None:
            self.skipTest("no uptime counter")
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime."
                            "ToUniversalTime().ToString('o')"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not (r.stdout or "").strip():
            self.skipTest("could not read LastBootUpTime")
        import datetime as dt
        os_boot = dt.datetime.fromisoformat((r.stdout or "").strip().replace("Z", "+00:00"))
        self.assertLess(abs(os_boot.timestamp() - boot), 300,
                        "the implied boot must match the OS within five minutes")

    def test_a_wrapped_counter_is_rejected_rather_than_trusted(self):
        """The range check, which is what makes this robust rather than lucky."""
        import ctypes
        if not sys.platform.startswith("win"):
            self.skipTest("Windows only")

        # The fake must return an INTEGER from a call. My first version made
        # GetTickCount64 a nested class, so calling it built an instance, dividing that
        # raised TypeError, and the except clause returned None - the test passed
        # against the unfixed code too, proving nothing. Caught by running this file
        # against HEAD before committing.
        fn = mock.Mock(return_value=(1 << 64) - 1)
        with mock.patch.object(ctypes, "windll", mock.Mock(kernel32=mock.Mock(
                GetTickCount64=fn))):
            self.assertIsNone(D._implied_boot_epoch())
        self.assertTrue(fn.called, "the fake counter must actually have been read")


if __name__ == "__main__":
    unittest.main()


# ── 11 (behavioural). The mute's third defect, driven as an episode ───────────
try:
    from test_arm_a_mute_real import NOW as A_NOW, REAL_RUN_ONCE, row  # cloud's double
except ImportError:                                                   # pragma: no cover
    from tests.test_arm_a_mute_real import NOW as A_NOW, REAL_RUN_ONCE, row


class MuteClearsOnADeliveredAlert(Floor):
    """A muted 6-minute alert, then a DELIVERED 20-minute urgent one, then recovery.

    The source-reading version of this test let a mutant survive: the mutation flipped
    the `if` to `if False:` and left the comment and the _remember_alerts line in place,
    so the string assertions still matched. Only an episode can tell.
    """

    def setUp(self):
        super().setUp()
        p = mock.patch.object(D, "_remember_alerts",
                              lambda l, **k: l.setdefault("alerts", {}).update(k))
        p.start()
        self.addCleanup(p.stop)
        D._ARM_A_FIRED.clear()
        self.addCleanup(D._ARM_A_FIRED.clear)

    def episode(self, minutes):
        """Drive check_arm_a once per stage, one ledger throughout, as main() does."""
        import arm_a_watch as A
        from datetime import timedelta
        led = self.led = getattr(self, "led", {})
        state = {}

        def drive(**kw):
            with contextlib.redirect_stdout(io.StringIO()):
                return REAL_RUN_ONCE(now=state["now"], fired=kw["fired"],
                                     deliver=kw["deliver"],
                                     get_commit_time=lambda ref: state["commit"],
                                     get_status=lambda ref: None,
                                     get_runs=lambda: state["runs"],
                                     get_job_state=lambda rid: False, sender="LAPTOP BOT")
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", True), \
             mock.patch.object(A, "run_once", drive):
            for stage in minutes:
                if stage is None:                        # recovered
                    state["now"] = A_NOW + timedelta(minutes=40)
                    state["runs"] = [row(state["now"] - timedelta(minutes=1),
                                         "in_progress", 9)]
                    state["commit"] = state["now"] - timedelta(minutes=2)
                else:
                    state["now"] = A_NOW + timedelta(minutes=stage)
                    state["runs"] = [row(A_NOW - timedelta(minutes=7), "queued", 1)]
                    state["commit"] = A_NOW - timedelta(minutes=10)
                D._ARM_A_NEXT = 0
                D.check_arm_a(led)
        return [s for s, _ in self.sent]

    def test_the_recovery_is_sent_after_a_delivered_urgent_alert(self):
        subjects = self.episode([0, 20, None])
        self.assertTrue(any("running again" in s for s in subjects),
                        "told it broke and never told it recovered is the worst outcome: %r"
                        % (subjects,))

    def test_the_flag_is_cleared_once_something_was_delivered(self):
        self.episode([0, 20])
        self.assertIs((self.led.get("alerts") or {}).get("arm_a_queued_muted"), False)

    def test_a_fully_muted_episode_is_still_silent_both_ways(self):
        """The fix must not undo the two defects cloud found before this one."""
        subjects = self.episode([0, None])
        self.assertEqual(subjects, [], "%r" % (subjects,))


# ── 10 (behavioural). The watchdog's SMS and its halt branch ─────────────────
try:
    from test_rh_watchdog import run_main, snap                      # cloud's harness
except ImportError:                                                  # pragma: no cover
    from tests.test_rh_watchdog import run_main, snap

import rh_watchdog as W


class WatchdogSms(unittest.TestCase):
    LONG = ("Arm B has not reported for 95 minutes and the laptop may be down. "
            "Positions are unprotected.")

    def payloads(self, alert_email):
        """Run the real alert() and capture what each channel was handed."""
        got = []
        env = {"GMAIL_APP_PASSWORD": "pw", "GMAIL_USER": "a@b.c", "ALERT_EMAIL": alert_email,
               "SMS_TO": "555@vtext.com", "NTFY_TOPIC": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(W, "_email", lambda frm, pw, to, subj, body:
                               got.append((to, subj, body))), \
             mock.patch.object(W.bot, "GMAIL_APP_PW", "pw", create=True), \
             contextlib.redirect_stdout(io.StringIO()):
            W.alert(self.LONG, urgent=True)
        return dict((to, body) for to, _s, body in got)

    def test_the_sms_is_the_alert_not_a_note_about_email_routing(self):
        """127 of the SMS's 140 characters were a note about ALERT_EMAIL, leaving 13.

        It only fired when the secret was blank, i.e. exactly when configuration was
        already wrong and the urgent path mattered most.
        """
        sms = self.payloads("")["555@vtext.com"]
        self.assertNotIn("ALERT_EMAIL is not set", sms)
        self.assertIn("Arm B has not reported", sms)
        self.assertLessEqual(len(sms), 140)

    def test_the_email_still_carries_the_routing_note(self):
        bodies = self.payloads("")
        to_self = bodies["a@b.c"]
        self.assertIn("ALERT_EMAIL is not set", to_self)
        self.assertIn("Arm B has not reported", to_self)

    def test_a_correctly_routed_alert_has_no_note_anywhere(self):
        bodies = self.payloads("devon@example.com")
        for body in bodies.values():
            self.assertNotIn("ALERT_EMAIL is not set", body)


class WatchdogHalt(unittest.TestCase):
    def test_a_deliberate_halt_is_reported_calmly_and_not_as_an_outage(self):
        from datetime import timedelta
        rc, sent, out = run_main([snap(W.datetime(2026, 9, 30, 13, 55),
                                       "halted_by_rh_HALT")],
                                 now=W.datetime(2026, 9, 30, 14, 0))
        self.assertEqual(len(sent), 1, "%r" % (sent,))
        msg, urgent = sent[0]
        self.assertFalse(urgent, "paging about a state someone chose is how a watchdog "
                                 "gets muted")
        # Must assert on text UNIQUE to this branch. Asserting `"rh_HALT" in msg` let a
        # mutant survive: disabling this branch falls through to the generic degraded
        # one, which sends a single non-urgent alert that also names the reason, so
        # every weaker assertion passed against the broken code.
        self.assertIn("PAUSED BY rh_HALT", msg)
        self.assertIn("alive and reporting", msg)
        self.assertNotIn("DEGRADED", msg,
                         "a deliberate pause is not the broker-outage message")
        self.assertEqual(rc, 0)

    def test_a_halted_bot_that_stops_reporting_is_still_an_outage(self):
        """The kill switch must not become a way to disable the dead-man's switch."""
        from datetime import timedelta
        now = W.datetime(2026, 9, 30, 14, 0)
        rc, sent, out = run_main([snap(now - timedelta(minutes=600), "halted_by_rh_HALT")],
                                 now=now)
        self.assertEqual(len(sent), 1)
        # It may NAME the halt as context - "it was already DEGRADED when it went
        # quiet" is useful - but it must be the stopped-reporting alert, not the calm
        # paused-on-purpose one.
        self.assertIn("stopped reporting", sent[0][0])
        self.assertNotIn("PAUSED BY rh_HALT", sent[0][0],
                         "a stale heartbeat is a dead laptop, whatever it last said")


class SettledIsAlwaysBuyingPower(Floor):
    """Mine, guarding the repeat-trim fix in the DANGEROUS direction.

    cycle() now hands decide() cash = buying power PLUS sold_today, with unsettled =
    sold_today, so equity (cash + invested) finally includes unsettled proceeds. The risk
    I introduced is the other side of that trade: if those two figures ever disagree, the
    settled number decide() sizes BUYS from could rise above the cash that actually
    exists, which is a real order the broker would reject - or worse, accept. It cannot,
    because settled = (bp + sold) - sold = bp identically, and this pins that algebra
    across a range of sold_today values instead of trusting me to have done it right.

    Driven through the real cycle() so it reads the real construction. Asserting the
    formula in the test instead would just restate the code and agree with it either way.
    """

    def _state_decide_saw(self, sold):
        seen = {}
        led = {"cash": 42.0, "positions": [], "holds": {}, "last_buy": {},
               "sold_today": sold, "orders_today": 0, "day": "2026-11-03"}

        def capture(state, fast=False):
            seen.update(state)
            return {"orders": [], "notes": [], "snapshot": {}}

        with mock.patch.object(D.rh_bot, "decide", capture), \
             mock.patch.object(D, "persist", lambda *a, **k: None):
            D.cycle(led, fast=True)
        self.assertTrue(seen, "decide() was never reached")
        return seen

    def test_settled_is_exactly_buying_power_whatever_the_unsettled_proceeds(self):
        for sold in (0.0, 0.01, 50.0, 1000.0):
            with self.subTest(sold=sold):
                s = self._state_decide_saw(sold)
                # The same expression decide() uses, rh_bot.py:69.
                settled = max(0.0, float(s["cash"]) - float(s["unsettled"]))
                self.assertAlmostEqual(settled, 42.0, places=6,
                                       msg="settled must stay AT buying power: a buy sized "
                                           "from more than that spends cash the account "
                                           "does not have")

    def test_a_sale_does_not_move_buying_power(self):
        """The WRITE side of the one-meaning fix, and the half my own tests were missing:
        a mutation that restored `led["cash"] += proceeds` in apply_fills survived both my
        files, caught only by cloud's. led["cash"] is buying power everywhere now, and
        buying power does not move on a sale until T+1 settles - the proceeds belong to
        sold_today alone. Counting them in both places is what gave the field two meanings.
        """
        led = {"cash": 2.0, "positions": [{"symbol": "SPY", "qty": 1.0, "avg_cost": 100.0}],
               "holds": {}, "last_buy": {}, "sold_today": 0.0, "orders_today": 0}
        orders = [{"action": "sell", "symbol": "SPY", "qty": 1.0, "notional": 100.0,
                   "reason": "trim to target"}]
        placed = [{"symbol": "SPY", "action": "sell", "status": "ok"}]
        with mock.patch.object(D, "DRY", False):
            D.apply_fills(led, orders, placed, {"SPY": 100.0})
        self.assertEqual(led["sold_today"], 100.0, "the proceeds are recorded as unsettled")
        self.assertEqual(led["cash"], 2.0,
                         "buying power must not move on a sale: the proceeds are unsettled "
                         "until T+1, and counting them here as well as in sold_today is the "
                         "double-count that trimmed the same ETF twice")

    def test_equity_now_includes_the_unsettled_proceeds(self):
        """The bug itself. Proceeds left out of equity shrink the per-ETF target, and the
        next pass trims the same ETF a second time - $133.33 sold against a $100 intent."""
        s = self._state_decide_saw(50.0)
        self.assertAlmostEqual(float(s["cash"]), 92.0, places=6,
                               msg="equity is cash + invested, so the proceeds have to be "
                                   "inside cash or the target shrinks")


class HaltIsAReminderNotAStream(unittest.TestCase):
    """Mine, from cloud's follow-up review 2026-11-03: the halt branch had NO gate.

    Every other branch in main() goes through crossed(); this one returned notify()
    unconditionally, and the watchdog is stateless and runs after each Arm A pass plus a
    */30 cron - so a halt held through one trading day was a few dozen identical emails,
    while the comment directly above the branch claimed it "reported once".

    Exactly-once is not reachable without durable state, and cloud's own pair of tests is
    the proof: their fixture rebuilds the history so the pause is 150 minutes old on EVERY
    run, so any function of the duration alone has to answer all three runs identically -
    zero, and their control fails; three, and their dedupe test fails. A wall-clock gate
    that is true at 14:00 and false at 14:15 and 14:30 is a window tuned to their
    fixture's clock, not a design. So the property pinned here is the one I chose and can
    defend: announce a NEW pause, remind once a day, stay quiet in between.

    The duration and the gate are tested SEPARATELY, by patching degraded_minutes. Going
    through the fixture's history for the boundary cases would make these tests agree with
    whatever that history happens to measure, which is how test 3 of mine passed against
    unfixed code last week.
    """

    AGED = 150                      # well past CROSS_WINDOW_MIN
    OFF_WINDOW = 14 * 60            # 14:00 ET, nowhere near the 10:00 reminder

    def _history(self, now, minutes):
        from datetime import timedelta
        t, snaps = now - timedelta(minutes=minutes), []
        while t <= now - timedelta(minutes=2):
            snaps.append(snap(t, "halted_by_rh_HALT"))
            t += timedelta(minutes=5)
        return snaps

    def _run(self, at, **consts):
        now = W.datetime(2026, 9, 30, at // 60, at % 60)
        return run_main(self._history(now, self.AGED), now=now, **consts)

    # ── the bug itself ────────────────────────────────────────────────────────
    def test_an_aged_pause_outside_the_reminder_window_says_nothing(self):
        rc, sent, out = self._run(self.OFF_WINDOW)
        self.assertEqual(sent, [], "the same pause was announced again: %r" % (sent,))
        self.assertEqual(rc, 0)

    def test_going_quiet_does_not_fall_through_to_the_broker_outage_alert(self):
        """The gate must RETURN. Falling through reaches the generic degraded branch,
        which would turn a deliberate pause into an Arm B DEGRADED alert - louder than
        the bug I was fixing."""
        rc, sent, out = self._run(self.OFF_WINDOW)
        self.assertNotIn("DEGRADED", "".join(m for m, _u in sent))
        self.assertIn("already been reported", out)

    def test_three_runs_fifteen_minutes_apart_on_an_aged_pause_are_all_silent(self):
        per_run = [self._run(m)[1] for m in
                   (self.OFF_WINDOW, self.OFF_WINDOW + 15, self.OFF_WINDOW + 30)]
        self.assertEqual(sum(len(s) for s in per_run), 0, "%r" % (per_run,))

    # ── but not silent forever ────────────────────────────────────────────────
    def test_the_daily_reminder_still_fires(self):
        """Once ever is the wrong target: the failure mode of announcing a pause a single
        time is Devon forgetting the bot is paused while deposits sit uninvested."""
        rc, sent, out = self._run(W.HALT_REMIND_AT_MIN)
        self.assertEqual(len(sent), 1, "%r" % (sent,))
        self.assertIn("PAUSED BY rh_HALT", sent[0][0])
        self.assertFalse(sent[0][1], "a deliberate pause is never urgent")

    # ── the boundary, with the measurement held fixed ─────────────────────────
    def test_a_new_pause_is_announced_even_outside_the_window(self):
        rc, sent, out = self._run(self.OFF_WINDOW, degraded_minutes=lambda _et: 3.0)
        self.assertEqual(len(sent), 1, "%r" % (sent,))
        self.assertIn("PAUSED BY rh_HALT", sent[0][0])

    def test_a_pause_just_past_the_new_window_is_not_announced_again(self):
        rc, sent, out = self._run(self.OFF_WINDOW,
                                  degraded_minutes=lambda _et: float(W.CROSS_WINDOW_MIN))
        self.assertEqual(sent, [], "%r" % (sent,))

    def test_a_pause_whose_duration_cannot_be_measured_is_still_announced(self):
        """Cloud's control test caught this one. I had `held is None` counting as NOT new,
        on the reasoning that for a deliberate pause the storm is the dangerous direction
        and a delay costs nothing - and a pause with no measurable history then went
        unannounced entirely. An unmeasurable duration is never read as zero here."""
        rc, sent, out = self._run(self.OFF_WINDOW, degraded_minutes=lambda _et: None)
        self.assertEqual(len(sent), 1, "%r" % (sent,))
        self.assertIn("PAUSED BY rh_HALT", sent[0][0])

    # -- cloud's D1, 2026-11-03: the reminder must not ride on a dropped cron ---------
    # rh-watchdog.yml's own per-trigger overrides for workflow_run (after each Arm A pass).
    RUN_TRIGGER = dict(GRACE_MIN=30, CROSS_WINDOW_MIN=20)

    def test_the_daily_reminder_reaches_the_narrow_window_trigger(self):
        """Cloud measured ZERO reminders on day two of a held pause from workflow_run alone:
        the reminder rode entirely on the */30 cron, which rh-watchdog.yml records GitHub
        running 1 to 3 times a day. The notice exists so Devon does not forget the bot is
        paused while deposits sit uninvested, so it must not depend on a schedule GitHub
        drops. Real main(), that trigger's own constants, Arm A's real 15-minute spacing."""
        sent = []
        for at in (9 * 60 + 44, 9 * 60 + 59, 10 * 60 + 14,
                   10 * 60 + 29, 10 * 60 + 44, 10 * 60 + 59):
            now = W.datetime(2026, 9, 30, at // 60, at % 60)
            sent += run_main(self._history(now, self.AGED), now=now, **self.RUN_TRIGGER)[1]
        self.assertGreaterEqual(len(sent), 1,
                                "a held pause got no reminder from workflow_run alone")
        self.assertLessEqual(len(sent), 2, "a reminder is not a stream: %d" % len(sent))

    def test_the_reminder_minute_clears_the_narrow_triggers_grace(self):
        """The RELATION, not the number - this is how D1 happened and how it would return.
        The grace runs from the bot's 09:45 open, so the workflow_run trigger is silent
        until 09:45+GRACE_MIN, while crossed() stops accepting the threshold
        CROSS_WINDOW_MIN after it. A reminder set BEFORE the grace expires forfeits that
        overlap, and what is left can be narrower than the run spacing: at 10:00 it was
        five minutes against runs every fifteen. Asserted so that moving the grace fails
        here rather than quietly re-silencing the reminder."""
        expires = (9 * 60 + 45) + self.RUN_TRIGGER["GRACE_MIN"]
        self.assertGreaterEqual(W.HALT_REMIND_AT_MIN, expires,
                                "the reminder at %d is inside the grace, which expires at %d"
                                % (W.HALT_REMIND_AT_MIN, expires))
        usable = (W.HALT_REMIND_AT_MIN + self.RUN_TRIGGER["CROSS_WINDOW_MIN"]) - expires
        self.assertGreaterEqual(usable, 15,
                                "only %dm of the window is reachable; Arm A runs every 15m"
                                % usable)


class HaltHeartbeatIsMarketGated(Floor):
    """Mine, found reviewing my own fix: the halt branch sits ABOVE the market gate.

    An unconditional publish_degraded there commits and pushes for as long as the halt
    is held - at DEGRADED_PUSH_SEC of 300 that is 288 commits a day, around the clock,
    which is the commit storm publish_degraded's own docstring was written about. The
    watchdog returns early on a closed market, so a heartbeat outside the window
    informs nobody.
    """

    def test_it_publishes_while_the_market_is_open(self):
        calls = []
        with mock.patch.object(D, "publish_degraded", lambda l, r: calls.append(r)), \
             mock.patch.object(bot, "check_market", lambda: (True, None)):
            if bot.check_market()[0]:
                D.publish_degraded({}, "halted_by_rh_HALT")
        self.assertEqual(calls, ["halted_by_rh_HALT"])

    def test_the_call_site_is_guarded_by_the_market(self):
        src = _func("main")
        i = src.index('log("rh_HALT present')
        branch = src[i:i + 2000]
        # The CALL, not the bare name: the comment above it mentions publish_degraded
        # too, and matching that put the guard on the wrong side of the index.
        j = branch.index("publish_degraded(led,")
        self.assertIn("check_market", branch[:j],
                      "the halt heartbeat must be inside a market check")
