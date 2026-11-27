"""What cloud's re-run of LAPTOP's thirteen-finding fix (ccac4de5, 2026-11-03) still found.

Cloud re-ran its original repros against the fixed code with the real functions and only
IO stubbed. Eleven of the thirteen held (partial snapshot, zero cash, None-vs-[] and the
utf-8 decode, the malformed element, all three deposit routes, the six-file gate, the stale
error text, the heartbeat pathspec, the basis-less wind-down, the boot counter). Three
things did not, and they are pinned here the way the mute was:

  * The tests that still describe a defect carry @unittest.expectedFailure. They are LAPTOP
    BOT's to fix (rh_daemon.py, rh_bot.py, rh_watchdog.py are laptop-owned). When one is
    fixed the test starts passing, unittest reports an UNEXPECTED SUCCESS, and the owner
    deletes the marker. Nothing here changes a threshold or a published number.
  * Every expected failure has a control beside it that PASSES on today's code and
    exercises the same path the other way round, so a "no" below can never be a harness
    that cannot say "yes".

Nothing in this file may reach a real bridge, the real ledger or git. The bridge function is
replaced wholesale (by one that fails loudly unless a test installs its own fake), and every
state file is redirected into a temp directory by absolute path. Do NOT stub subprocess.run
to intercept the bridge: the fixed daemon launches it with Popen (_run_bridge), so a run stub
is silently bypassed. Cloud's first re-run of its scratch repros made exactly that mistake and
only stayed harmless because `claude` was not on that shell's PATH.

SECOND PASS (cloud, 2026-11-03 16:30 ET, after LAPTOP BOT's 781a0591). The first three were fixed
and their markers are gone. Two smaller things were pinned, each with a passing control: the rh_HALT
daily reminder reached Devon only through the */30 cron slots (GitHub drops those), and a
planned-restart note was filed when the machine's boot time could not be read. LAPTOP BOT fixed both
in 2203086a (2026-11-04) and removed those two markers; the tests stay as plain regression tests,
and no expectedFailure is left in this file.

Run:  python -m unittest discover -s tests -p test_arm_b_review_followup.py -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")

import alpaca_bot as bot          # noqa: E402
import rh_bot                     # noqa: E402
import rh_daemon as D             # noqa: E402

PRICE = {"SPY": 500.0, "QQQ": 400.0, "IWM": 200.0}
SIG = {"consensus": 0, "rsi": 50, "trend": "up", "buys": 2}


def _no_real_process(*a, **k):
    raise AssertionError("a test tried to start a real process: %r" % (a[:1],))


class DaemonSandbox(unittest.TestCase):
    """Real cycle()/decide()/apply_fills()/adopt_truth()/persist(); stubbed: Yahoo, the bridge, git, mail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.logged, self.sent = [], []

        def at(name):
            return os.path.join(self.tmp.name, name)

        patches = [
            mock.patch.object(D, "LEDGER_F", at("rh_ledger.json")),
            mock.patch.object(D, "STATUS_F", at("rh_status.json")),
            mock.patch.object(D, "LOG_F", at("rh_trade_log.jsonl")),
            mock.patch.object(D, "DEPOSITS_F", at("rh_deposits.json")),
            mock.patch.object(D, "DRY", False),
            mock.patch.object(D, "CLAUDE_BIN", "claude-NEVER-RUN-BY-TESTS"),
            mock.patch.object(D, "log", self.logged.append),
            mock.patch.object(D, "notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
            mock.patch.object(D, "_push_status", lambda reason: False),
            mock.patch.object(D, "_run_bridge", _no_real_process),     # a test overrides it with a fake, never the reverse
            mock.patch.object(rh_bot, "_quote",
                              lambda sym, meme: (PRICE[sym], SIG, [PRICE[sym]] * 60) if sym in PRICE else (None, None, None)),
            mock.patch.object(bot, "fetch_wsb", lambda: []),
            mock.patch.object(bot, "news_flags", lambda s, m: {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def trade_log(self):
        if not os.path.exists(D.LOG_F):
            return []
        with open(D.LOG_F, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def empty_book(self):
        led = D.fresh_ledger()
        led.update({"cash": 100.0, "day": "2026-10-28"})
        return led


def _placed_ok():
    return {"placed": [{"symbol": s, "action": "buy", "status": "ok", "detail": ""} for s in PRICE]}


def _bridge_down(cmd, **kw):
    """The executor never answers (timeout) and the reconcile turn finds the CLI logged out."""
    if "Place these EXACT orders" in cmd[2]:
        raise subprocess.TimeoutExpired(cmd, 240)
    return subprocess.CompletedProcess(cmd, 1, stdout="Failed to authenticate", stderr="")


def _bridge_ok(cmd, **kw):
    if "Place these EXACT orders" in cmd[2]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_placed_ok()), stderr="")
    return subprocess.CompletedProcess(cmd, 1, stdout="Failed to authenticate", stderr="")


# ── 1. An executor turn with NO usable report must not take persist() down with it ──────────
class UnknownOutcomeIsLogged(DaemonSandbox):
    """place() now returns None for "no usable report" and apply_fills/email_trades handle it, but
    persist() still iterates `placed`: `for p in placed` over None raises TypeError out of cycle().

    persist() already has the right answer for an order with no matching report ("unknown" is
    its default status), and realized.arm_b_block()'s sells-logged tripwire deliberately counts
    "unknown" sells, so those lines are the record of a sale whose outcome nobody saw. The crash
    drops exactly them. The main loop catches it as "cycle error (loop continues)", which is why
    nothing is loud."""

    def test_control_a_reporting_executor_writes_one_trade_log_line_per_order(self):
        led = self.empty_book()
        with mock.patch.object(D, "_run_bridge", _bridge_ok):
            D.cycle(led, fast=False)
        lines = self.trade_log()
        self.assertEqual([l["status"] for l in lines], ["ok", "ok", "ok"])

    def test_control_no_phantom_position_is_booked_when_the_turn_gives_no_report(self):
        """The part of the fix that works: the ledger is not credited and a reconcile is forced."""
        led = self.empty_book()
        with mock.patch.object(D, "_run_bridge", _bridge_down):
            try:
                D.cycle(led, fast=False)
            except TypeError:
                pass                         # the defect below; this control is about the ledger
        self.assertEqual(led["positions"], [], "phantom positions booked for orders that never reached the broker")
        self.assertTrue(led.get("needs_reconcile"))
        self.assertEqual(led["cash"], 100.0)

    def test_cycle_does_not_raise_when_the_executor_turn_gives_no_report(self):
        led = self.empty_book()
        with mock.patch.object(D, "_run_bridge", _bridge_down):
            D.cycle(led, fast=False)

    def test_orders_of_unknown_outcome_are_logged_as_unknown(self):
        led = self.empty_book()
        with mock.patch.object(D, "_run_bridge", _bridge_down):
            try:
                D.cycle(led, fast=False)
            except TypeError:
                pass
        self.assertEqual([l["status"] for l in self.trade_log()], ["unknown", "unknown", "unknown"],
                         "three orders were handed to a bridge that never answered and the trade log is empty")


# ── 2. A trim must not repeat on the same day because the proceeds are unsettled ───────────
class UnsettledProceedsDoNotTriggerASecondTrim(DaemonSandbox):
    """After a sale the post-trade reconcile replaces led["cash"] with broker BUYING POWER, which
    excludes unsettled sale proceeds. decide() then subtracts led["sold_today"] from that cash AGAIN
    (rh_bot.py:69, settled = cash - unsettled) and leaves the proceeds out of equity (rh_bot.py:88,
    equity = cash + invested). Equity reads about a third low, the per-ETF target shrinks, and the
    SAME overweight ETF is trimmed again on the next full cycle.

    LAPTOP's fix for this item covered only half of it (the true-zero-cash `or`); this half is
    byte-for-byte what cloud mailed on 2026-10-30 and is not mentioned in the 2026-11-02 reply."""

    def setUp(self):
        super().setUp()
        self.broker = {"bp": 2.00, "unsettled": 0.0, "pos": {"SPY": 0.40, "QQQ": 0.12, "IWM": 0.25}}

    def total(self):
        b = self.broker
        return b["bp"] + b["unsettled"] + sum(PRICE[s] * q for s, q in b["pos"].items())

    def bridge(self, cmd, **kw):
        b = self.broker
        p = cmd[2]
        if "Place these EXACT orders" in p:
            orders = json.loads(p.split("ORDERS:\n")[1].split("\n\nThen reply")[0])
            placed = []
            for o in orders:
                if o["action"] == "sell":                       # proceeds exist but are NOT buying power until T+1
                    b["pos"][o["symbol"]] -= o["qty"]
                    b["unsettled"] += o["qty"] * PRICE[o["symbol"]]
                else:
                    b["bp"] -= o["notional"]
                    b["pos"][o["symbol"]] += o["notional"] / PRICE[o["symbol"]]
                placed.append({"symbol": o["symbol"], "action": o["action"], "status": "ok", "detail": ""})
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"placed": placed}), stderr="")
        snap = {"cash": round(b["bp"], 2), "total_value": round(self.total(), 2), "pending_deposits": 0,
                "positions": [{"symbol": s, "qty": q, "avg_cost": PRICE[s]} for s, q in b["pos"].items() if q > 1e-9]}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(snap), stderr="")

    def cycles(self, n=3):
        led = D.fresh_ledger()
        led.update({"cash": 2.0, "day": "2026-10-28",
                    "positions": [{"symbol": s, "qty": q, "avg_cost": PRICE[s]} for s, q in self.broker["pos"].items()]})
        sells = []
        with mock.patch.object(D, "_run_bridge", self.bridge):
            for _ in range(n):
                res = D.cycle(led, fast=False)
                sells.append([(o["symbol"], round(o["qty"] * PRICE[o["symbol"]], 2))
                              for o in res["orders"] if o["action"] == "sell"])
        return sells

    def test_control_the_first_full_cycle_does_trim_the_overweight_etf_to_its_target(self):
        """SPY is $200 of a $300 account, a third is $100: the first trim must happen, or the
        test below could pass for the wrong reason (a book that never trims)."""
        first = self.cycles(1)[0]
        self.assertEqual([s for s, _ in first], ["SPY"])
        self.assertAlmostEqual(first[0][1], 100.0, delta=0.5)

    def test_the_second_full_cycle_does_not_trim_spy_again(self):
        sells = self.cycles(3)
        later = [s for cycle_sells in sells[1:] for s in cycle_sells]
        self.assertEqual(later, [], "SPY was trimmed again on unsettled proceeds: %r" % (sells,))


# ── 3. A deliberate rh_HALT: announced when it starts, reminded daily, never every run ──────
import test_rh_watchdog as _tw          # noqa: E402  (cloud's harness: a real snapshot history, a fixed clock)

W = _tw.W


class HaltDayIsAFewMessages(unittest.TestCase):
    """rh_HALT used to be announced on EVERY watchdog run (28 messages in one simulated day, 34 on a full
    one). LAPTOP BOT's 781a0591 announces a NEW pause (held < CROSS_WINDOW_MIN) and reminds once a day
    at HALT_REMIND_AT_MIN through crossed() on the wall clock.

    THE FIXTURE MATTERS. Cloud's first version of these tests rebuilt the pause relative to each
    run's own `now`, so the pause was 150 minutes old on every run and no stateless gate could satisfy
    both tests; LAPTOP BOT was right about that. Here the pause starts at a FIXED time and the runs walk
    forward on the two schedules rh-watchdog.yml really has: workflow_run after each Arm A pass (about
    every 15 minutes, window 20, grace 30) and the */30 cron (the defaults)."""

    RUN = dict(CROSS_WINDOW_MIN=20, GRACE_MIN=30, STALE_ALERT_MIN=(30, 120, 360))   # as rh-watchdog.yml sets workflow_run
    CRON = {}                                                                       # schedule: the module defaults
    DAY1 = _tw.datetime(2026, 9, 30)
    DAY2 = _tw.datetime(2026, 10, 1)
    PAUSE = _tw.datetime(2026, 9, 30, 11, 7)

    def halted_history(self, now, held=150):
        start = now - timedelta(minutes=held)
        snaps, t = [], start
        while t <= now - timedelta(minutes=2):
            snaps.append(_tw.snap(t, "halted_by_rh_HALT"))
            t += timedelta(minutes=5)
        return snaps

    def history(self, now):
        """A healthy heartbeat, then a halted one every 15 minutes in session hours, from a FIXED start."""
        snaps = [_tw.snap(self.PAUSE - timedelta(minutes=15))]
        t = self.PAUSE
        while t <= now - timedelta(minutes=3):
            if (9, 45) <= (t.hour, t.minute) < (15, 56):
                snaps.append(_tw.snap(t, "halted_by_rh_HALT"))
            t += timedelta(minutes=15)
        return snaps

    def messages(self, day, slots, consts):
        """Run the real watchdog at each (hour, minute) slot and return every message it sent."""
        out = []
        for h, m in slots:
            now = day.replace(hour=h, minute=m)
            rc, sent, _ = _tw.run_main(self.history(now), now=now, market_open=(9, 45) <= (h, m) < (15, 55),
                                       **consts)
            self.assertEqual(rc, 0)
            for msg, urgent in sent:
                self.assertFalse(urgent, "a deliberate pause was sent as urgent")
                self.assertIn("PAUSED BY rh_HALT", msg)
            out.extend(sent)
        return out

    def test_control_a_single_run_does_report_the_pause_calmly(self):
        now = _tw.NOW
        rc, sent, _ = _tw.run_main(self.halted_history(now, held=6), now=now)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1, "%r" % (sent,))
        self.assertIn("PAUSED BY rh_HALT", sent[0][0])
        self.assertFalse(sent[0][1])

    def test_a_pause_that_begins_mid_session_is_announced_a_few_times_not_every_run(self):
        # 11:00-12:59 on both schedules is 8 workflow_run passes and 4 cron slots. The pre-fix watchdog
        # sent one message for EACH of those 12 runs (28 across the whole simulated day).
        slots_run = [(h, m) for h in (11, 12) for m in (14, 29, 44, 59)]
        slots_cron = [(h, m) for h in (11, 12) for m in (0, 30)]
        sent = (self.messages(self.DAY1, slots_run, self.RUN)
                + self.messages(self.DAY1, slots_cron, self.CRON))
        self.assertGreaterEqual(len(sent), 1, "a new pause was never announced")
        self.assertLessEqual(len(sent), 3, "%d messages for one pause in two hours" % len(sent))

    def test_control_the_daily_reminder_does_arrive_on_the_cron_slots(self):
        sent = self.messages(self.DAY2, [(9, 30), (10, 0), (10, 30), (11, 0)], self.CRON)
        self.assertGreaterEqual(len(sent), 1)
        self.assertLessEqual(len(sent), 2, "a daily reminder is not a stream: %d" % len(sent))

    def test_the_daily_reminder_does_not_depend_on_the_cron_slots(self):
        """The same next morning, workflow_run alone. FOUND at HALT_REMIND_AT_MIN = 10:00: 0 messages.
        The workflow_run trigger passes GRACE_MIN=30 (silent until 10:15) and a 20-minute window, so only
        10:15-10:19 qualified and the 15-minute cadence usually missed it. The reminder therefore rode on
        the */30 cron, which GitHub ran 1 to 3 times a day when it was measured (rh-watchdog.yml). FIXED
        2026-11-04 (2203086a) by moving the reminder to 10:15 (615): exactly one from this trigger. The
        pause notice exists so Devon does not forget the bot is paused; it must not depend on a dropped cron."""
        slots = [(9, 44), (9, 59), (10, 14), (10, 29), (10, 44), (10, 59)]
        sent = self.messages(self.DAY2, slots, self.RUN)
        self.assertGreaterEqual(len(sent), 1, "no reminder from the workflow_run trigger on a held pause")


# ── 4. A planned-restart note must not be filed when the boot time could not be read ────────
class PlannedRestartBootTimeUnreadable(unittest.TestCase):
    """781a0591 files a quiet 'adopted new code' note when the daemon's own restart marked the gap. The
    marker is bounded to PLANNED_RESTART_MAX_MIN market minutes (verified here with the REAL
    market_minutes_since: 2 and 5 note, 6 and 45 alert). One hole inside the bound: when the boot time
    cannot be read, the unmarked report says so ('Could not read this machine's boot time, so whether
    the LAPTOP went away ... is unknown') and the marked one drops that sentence and says it needs no
    action. This file's standing rule is that an unmeasurable thing is never read as fine. At most
    PLANNED_RESTART_MAX_MIN market minutes, and it needs the boot read to fail too, so it is small."""

    def setUp(self):
        self.sent, self.logged = [], []
        for name, fn in (
            ("notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
            ("log", self.logged.append),
            ("_remember_alerts", lambda led, **kw: led.setdefault("alerts", {}).update(kw)),
            ("save_ledger", lambda led: None),
            ("_save", self._refuse_write),
        ):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _refuse_write(path, obj):
        raise AssertionError("a test tried to write the real file %r" % path)

    def report(self, boot, marked, mins=3):
        stamp = (D.now_et() - timedelta(minutes=4000)).strftime("%Y-%m-%dT%H:%M")
        led = ({"alerts": {"planned_restart_stamp": stamp, "planned_restart_to": "ccac4de"}}
               if marked else {})
        with mock.patch.object(D, "_load", lambda p, d=None: {"ts": stamp}), \
             mock.patch.object(D, "market_minutes_since", lambda ts: mins), \
             mock.patch.object(D, "_implied_boot_epoch", lambda: boot):
            D.report_downtime(led)
        self.assertEqual(len(self.sent), 1, self.sent)
        return self.sent[0]

    def test_control_unmarked_with_an_unreadable_boot_time_says_so(self):
        subject, body = self.report(None, marked=False)
        self.assertIn("blind 3 market minute(s)", subject)
        self.assertIn("boot time", body)

    def test_control_marked_with_a_readable_old_boot_is_the_quiet_note(self):
        import time
        subject, body = self.report(time.time() - 999999, marked=True)
        self.assertIn("adopted new code", subject)

    def test_marked_with_an_unreadable_boot_time_still_says_the_boot_time_is_unknown(self):
        subject, body = self.report(None, marked=True)
        self.assertIn("boot time", body,
                      "filed as %r with no mention that the boot time could not be read" % subject)


if __name__ == "__main__":
    unittest.main()
