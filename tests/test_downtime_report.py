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
        # notify/log/_remember_alerts are the only things report_downtime reaches
        # outward through; patching them keeps this off the real files and out of mail.
        for name, fn in (
            ("notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
            ("log", self.logged.append),
            ("_remember_alerts",
             lambda led, **kw: led.setdefault("alerts", {}).update(kw)),
        ):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

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

    def test_a_failed_send_is_not_recorded_as_reported(self):
        """Cloud's 2026-10-08 audit, item 2.

        This runs seconds after boot - 40 s on 2026-10-08 - which is exactly when the
        network may not be up. Writing the dampener before the send meant a mail that
        never left was filed as delivered and never retried: the outage alert lost to
        the outage. Record only on a True from notify.
        """
        with mock.patch.object(D, "notify", lambda s, b, untrusted=False: False):
            self.call(self.stamp_ago(31), 31, time.time() - 300)
        self.assertNotIn("downtime_reported", self.ledger.get("alerts", {}),
                         "a failed send must stay unreported so the next restart retries")
        self.assertTrue(any("NOT reported (send failed)" in m for m in self.logged), self.logged)

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
