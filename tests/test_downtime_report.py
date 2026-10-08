"""The daemon must announce a gap that nothing could announce while it was happening.

2026-10-08 the laptop went down twice. The 04:54 ET bugcheck (0x154, no dump written)
reached Devon only because LAPTOP BOT DAILY CHECK happened to run at 10:24; the 11:27 ET
outage, wholly inside the trading session, reached him not at all - the daemon came back
at 11:32:52, logged "rh_daemon starting", and said nothing. Every other blindness in this
file emails: broker unreachable, selftest failing, login expired, Arm A silent. The one
that takes the whole process down had no alert, because a dead daemon cannot report
itself and nothing looked back on the way up.

report_downtime closes that. These tests pin the two gates that keep it from crying wolf
(a code-pull restart, and a weekend with no reboot in it) and the dedupe that keeps a
second restart inside one outage from re-alerting - the dampener bug this file has now
produced five times. All five of these were mutation-checked: breaking either gate, the
dedupe, the reboot comparison or the subject line each fails at least one test here.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D


class DowntimeReportTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.logged, self.ledger = [], [], {}
        # EVERY outward call report_downtime and retry_pending_downtime make has to be
        # stubbed here, and save_ledger is in that list for a reason: on 2026-10-08 I
        # added a save_ledger call to the parking path, ran this file, and it wrote the
        # TEST's dict straight over the live rh_ledger.json - the real positions, cash
        # and alert dampeners, on a file that is gitignored and has no backup. The suite
        # had been safe only because the code happened to go through _remember_alerts.
        # A test must not be able to reach live state by accident, so _save is stubbed
        # too: it is the single floor under every writer in this module, and anything
        # that tries to write a real file from here fails loudly instead of silently
        # succeeding.
        for name, fn in (
            ("notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
            ("log", self.logged.append),
            ("_remember_alerts",
             lambda led, **kw: led.setdefault("alerts", {}).update(kw)),
            ("save_ledger", lambda led: None),
            ("_save", self._refuse_write),
        ):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _refuse_write(path, obj):
        raise AssertionError("a test tried to write the real file %r - stub the writer" % path)

    def call(self, stamp, mins, boot, led=None):
        """One report_downtime call with everything outside it pinned."""
        with mock.patch.object(D, "_load", lambda p, d: ({"ts": stamp} if stamp else {})), \
             mock.patch.object(D, "market_minutes_since", lambda ts: mins), \
             mock.patch.object(D, "_implied_boot_epoch", lambda: boot):
            D.report_downtime(self.ledger if led is None else led)
        return self.sent, self.logged

    @staticmethod
    def stamp_ago(minutes):
        return (D.now_et() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")

    # ---- the gates that keep it quiet -------------------------------------------------

    def test_a_restart_for_code_is_not_an_outage(self):
        """sync_code restarts take well under a minute and must never mail."""
        sent, _ = self.call(self.stamp_ago(2), 999, time.time())
        self.assertEqual(sent, [])
        self.assertEqual(self.ledger, {}, "a non-event must not write the dedupe key either")

    def test_a_closed_market_gap_with_no_reboot_is_logged_not_mailed(self):
        """Without this gate the first code pull each Monday mails about the weekend.

        The mocked value here is 1, not 0. It used to be 0, and cloud's 2026-10-08
        audit showed 0 is a value the real market_minutes_since CANNOT return for an
        after-hours restart: the last stamp of a session is 15:54, and counting from
        the start of that minute bills 15:54-15:55 as one blind minute. The test
        passed while production mailed a false alert on every such restart, because
        the mock was kinder than reality. 1 is what really happens; assert against
        that. tests/test_downtime_report_real_clock.py pins the real function.
        """
        sent, logged = self.call(self.stamp_ago(4000), 1, time.time() - 999999)
        self.assertEqual(sent, [])
        self.assertTrue(any("not alerting" in m for m in logged), logged)
        self.assertIn("downtime_reported", self.ledger.get("alerts", {}),
                      "still dedupe it, or every restart re-logs the same gap")

    def test_the_floor_is_two_minutes_not_one(self):
        """One market minute is heartbeat resolution; two is time Arm B could not act."""
        self.call(self.stamp_ago(4000), D.DOWNTIME_MIN_MARKET_MIN - 1, time.time() - 999999)
        self.assertEqual(self.sent, [], "below the floor must stay quiet")
        self.call(self.stamp_ago(4001), D.DOWNTIME_MIN_MARKET_MIN, time.time() - 999999, led={})
        self.assertEqual(len(self.sent), 1, "at the floor it must mail")
        self.assertIn("blind 2 market minute(s)", self.sent[0][0])

    # ---- what it must actually report ------------------------------------------------

    def test_market_minutes_lost_are_named_in_the_subject(self):
        sent, _ = self.call(self.stamp_ago(31), 31, time.time() - 300)
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertIn("blind 31 market minute(s)", subject)
        self.assertTrue(subject.startswith(D.SESSION_NAME + ":"),
                        "Devon's rule: the subject says which session must act")
        self.assertIn("The LAPTOP went down", body)
        self.assertIn("Kernel-Power 41", body, "point him at where the cause is recorded")
        self.assertIn("stops could not have", body,
                      "the exposure, not just the downtime: positions sat unmanaged")

    def test_a_reboot_outside_market_hours_still_mails(self):
        """No trading time lost, but his trading laptop restarting is his to know."""
        sent, _ = self.call(self.stamp_ago(4000), 0, time.time() - 300)
        self.assertEqual(len(sent), 1)
        self.assertIn("market was shut", sent[0][0])

    def test_a_gap_with_the_machine_still_up_points_at_the_task_without_asserting_it(self):
        """It must report what the uptime counter SAID, not what that implies.

        Cloud's 2026-10-08 audit, item 3: Fast Startup keeps the kernel across a Shut
        down, so the counter may not reset and a human shutdown could read as "the
        machine never left". The wording has to leave that open or the mail blames the
        task for something Windows did.
        """
        sent, _ = self.call(self.stamp_ago(31), 31, time.time() - 999999)
        self.assertIn("scheduled task", sent[0][1])
        self.assertIn("Fast Startup", sent[0][1], "name the counter's blind spot")
        self.assertNotIn("STAYED UP", sent[0][1], "too strong for a single uptime read")

    def test_an_unreadable_boot_time_still_mails_and_says_so(self):
        sent, _ = self.call(self.stamp_ago(31), 31, None)
        self.assertEqual(len(sent), 1)
        self.assertIn("is unknown", sent[0][1])

    # ---- the dampener this file has got wrong five times -----------------------------

    def test_a_second_restart_inside_one_outage_does_not_re_alert(self):
        led = {}
        self.call(self.stamp_ago(31), 31, time.time() - 300, led)
        self.assertEqual(len(self.sent), 1)
        self.call(self.stamp_ago(31), 31, time.time() - 300, led)
        self.assertEqual(len(self.sent), 1, "the dedupe key must live on the LEDGER, "
                                            "not in a module global a restart clears")

    def test_a_failed_send_parks_the_report_and_does_not_latch(self):
        """Cloud's 2026-10-08 audit, item 2, and its follow-up.

        This runs about 40 seconds after boot, exactly when the network may not be up.
        The first fix stopped a failed send being filed as delivered, but left the
        report with nowhere to live: the evidence is the stale ts in rh_status.json and
        the first pass overwrites it, so the promised retry could never happen. The
        report is now parked on the ledger, which outlives that file.
        """
        with mock.patch.object(D, "notify", lambda s, b, untrusted=False: False):
            self.call(self.stamp_ago(31), 31, time.time() - 300)
        alerts = self.ledger.get("alerts", {})
        self.assertNotIn("downtime_reported", alerts, "a failed send must not latch")
        parked = alerts.get("pending_downtime")
        self.assertIsNotNone(parked, "the report must survive the failed send")
        self.assertIn("blind 31 market minute(s)", parked["subject"])
        self.assertEqual(parked["stamp"], self.stamp_ago(31))
        self.assertEqual(parked["tries"], 1)

    def test_the_parked_report_goes_once_the_network_is_back(self):
        """The exact sequence cloud reproduced: the heartbeat file is gone by then."""
        with mock.patch.object(D, "notify", lambda s, b, untrusted=False: False):
            self.call(self.stamp_ago(31), 31, time.time() - 300)
        self.sent.clear()
        D.retry_pending_downtime(self.ledger)          # what the main loop calls each pass
        self.assertEqual(len(self.sent), 1, "the parked report must send")
        self.assertIn("blind 31 market minute(s)", self.sent[0][0])
        alerts = self.ledger.get("alerts", {})
        self.assertNotIn("pending_downtime", alerts, "delivered, so unpark it")
        self.assertEqual(alerts.get("downtime_reported"), self.stamp_ago(31))
        D.retry_pending_downtime(self.ledger)
        self.assertEqual(len(self.sent), 1, "and every later pass must send nothing")

    def test_the_retry_gives_up_loudly_rather_than_forever(self):
        """Both channels dead is its own alarm, not something to retry until reboot."""
        with mock.patch.object(D, "notify", lambda s, b, untrusted=False: False):
            self.call(self.stamp_ago(31), 31, time.time() - 300)
            for _ in range(D.DOWNTIME_RETRY_MAX):
                D.retry_pending_downtime(self.ledger)
        alerts = self.ledger.get("alerts", {})
        self.assertNotIn("pending_downtime", alerts, "must stop, or it logs every pass forever")
        self.assertEqual(alerts.get("downtime_reported"), self.stamp_ago(31))
        self.assertTrue(any("GIVEN UP" in m for m in self.logged), self.logged)

    def test_the_retry_is_free_when_nothing_is_parked(self):
        """It runs on EVERY pass, before the market gate, so it must cost nothing."""
        D.retry_pending_downtime({})
        D.retry_pending_downtime({"alerts": {}})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.logged, [])

    def test_the_retry_never_raises_on_a_damaged_record(self):
        """It is called from the top of the main loop; it must never stop the daemon."""
        for bad in ({"alerts": {"pending_downtime": {}}},
                    {"alerts": {"pending_downtime": {"tries": "x"}}}):
            D.retry_pending_downtime(bad)
        self.assertTrue(True, "no exception escaped")

    def test_a_delivered_send_is_recorded(self):
        """Control for the test above: with delivery working, the dampener must latch."""
        self.call(self.stamp_ago(31), 31, time.time() - 300)
        self.assertIn("downtime_reported", self.ledger.get("alerts", {}))

    def test_the_dedupe_is_per_outage_not_forever(self):
        """A new gap has a new ending heartbeat, so it must mail again."""
        led = {}
        self.call(self.stamp_ago(31), 31, time.time() - 300, led)
        self.call(self.stamp_ago(90), 31, time.time() - 300, led)
        self.assertEqual(len(self.sent), 2)

    # ---- it must never be the reason the daemon fails to start -----------------------

    def test_no_heartbeat_on_disk_is_silent(self):
        sent, _ = self.call(None, 31, time.time())
        self.assertEqual(sent, [])
        self.assertEqual(self.ledger, {})

    def test_a_malformed_heartbeat_is_swallowed(self):
        sent, logged = self.call("not-a-timestamp", 31, time.time())
        self.assertEqual(sent, [])
        self.assertTrue(any("report_downtime failed" in m for m in logged), logged)

    # ---- the wiring, read from SOURCE ------------------------------------------------

    def test_the_retry_is_wired_into_the_main_loop_before_the_market_gate(self):
        """Without this nothing notices if the call site is dropped.

        A mutation run on 2026-10-08 removed `retry_pending_downtime(led)` from main()'s
        loop and every behavioural test here still passed: the retry would then only run
        at startup, which is precisely the hole it was written to close, because the
        first pass overwrites the heartbeat the startup path reads. Position matters too
        - it sits above the HALT check and the market gate, since neither a paused bot
        nor a shut market makes the outage it is reporting less true.
        """
        import ast

        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "rh_daemon.py"), encoding="utf-8").read()
        main = next(n for n in ast.walk(ast.parse(src))
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.While))

        def calls(node):
            return [c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]

        top = [name for stmt in loop.body for name in calls(stmt)]
        self.assertIn("retry_pending_downtime", top,
                      "the retry must run every pass, not only at startup")
        gate = [i for i, stmt in enumerate(loop.body)
                if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                       and c.func.attr == "check_market" for c in ast.walk(stmt))]
        retry_at = next(i for i, stmt in enumerate(loop.body)
                        if "retry_pending_downtime" in calls(stmt))
        if gate:
            self.assertLess(retry_at, gate[0],
                            "a report parked out of session must not wait for an open")

    # ---- the boot clock itself --------------------------------------------------------

    def test_implied_boot_is_in_the_past_and_plausible(self):
        """Control: if this returned None everywhere, the reboot branch would never run."""
        boot = D._implied_boot_epoch()
        if boot is None:
            self.skipTest("no uptime counter on this platform")
        self.assertLess(boot, time.time())
        self.assertGreater(boot, time.time() - 400 * 24 * 3600, "implausible boot time")


if __name__ == "__main__":
    unittest.main()
