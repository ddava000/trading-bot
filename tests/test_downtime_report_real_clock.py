"""CROSS-AUDIT of rh_daemon.report_downtime (laptop c2a3e7e, 2026-10-08): the REAL market_minutes_since, not a stub.

tests/test_downtime_report.py patches market_minutes_since to return 0 for "a weekend with no reboot in it". The real
function cannot return 0 there: rh_daemon's loop only passes (and only writes rh_status.json) while
bot.check_market() is true, i.e. up to 15:55:00.000000, so the last local ts after a normal close reads "15:54" at best
(minute resolution), and market_minutes_since bills 15:54 to 15:55 as one market minute. The second gate
("a gap that cost no market minutes is only mailed when the machine rebooted") therefore never opens, and every plain
restart outside the session (a code sync, a Task Scheduler restart) mails Devon "Arm B was blind 1 market minute(s)".

Only the clock, the status file, the boot time, notify and log are pinned here. Everything else is the real code.

The false-alert test is an EXPECTED FAILURE: it documents the defect and turns red-as-"unexpected success" the moment
the owner (LAPTOP BOT) fixes it, which is the signal to delete the marker. The other tests prove the harness CAN see a
real outage and the 15:55 case, so the failing one is not failing for a reason of its own making.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D  # noqa: E402

REAL_DT = datetime


def report(last_stamp, now_str, rebooted):
    """Run the real report_downtime. Returns (mails, log lines)."""
    now = REAL_DT.strptime(now_str, "%Y-%m-%d %H:%M").replace(tzinfo=D.bot.ET_TZ)

    class FrozenDT(REAL_DT):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    boot = now.timestamp() - (60 if rebooted else 40 * 3600)     # booted a minute ago, or two days ago
    sent, logged, led = [], [], {}
    with mock.patch.object(D, "datetime", FrozenDT), \
         mock.patch.object(D, "now_et", lambda: now), \
         mock.patch.object(D, "_load", lambda p, d: {"ts": last_stamp}), \
         mock.patch.object(D, "_implied_boot_epoch", lambda: boot), \
         mock.patch.object(D, "notify", lambda s, b, untrusted=False: (sent.append((s, b)), True)[1]), \
         mock.patch.object(D, "log", logged.append), \
         mock.patch.object(D, "_remember_alerts", lambda l, **kw: l.setdefault("alerts", {}).update(kw)):
        D.report_downtime(led)
    return sent, logged


class RealClockDowntimeReport(unittest.TestCase):
    def test_the_harness_still_sees_a_real_mid_session_outage(self):
        """Present case: the machine rebooted inside a gap that cost real market minutes. Must mail."""
        sent, _ = report("2026-10-08T11:20", "2026-10-08 11:33", rebooted=True)
        self.assertEqual(len(sent), 1)
        self.assertIn("blind 13 market minute", sent[0][0])             # 11:20 to 11:33

    def test_a_last_pass_stamped_1555_is_quiet_as_the_original_test_assumed(self):
        sent, logged = report("2026-10-09T15:55", "2026-10-10 20:00", rebooted=False)
        self.assertEqual(sent, [])
        self.assertTrue(any("not alerting" in m for m in logged), logged)

    def test_the_last_pass_of_a_session_cannot_be_stamped_1555(self):
        """Why the quiet case above never happens: check_market() is false past 15:55:00.000000."""
        import alpaca_bot

        class At(REAL_DT):
            @classmethod
            def now(cls, tz=None):
                return REAL_DT(2026, 10, 9, 15, 55, 1, tzinfo=D.bot.ET_TZ)
        with mock.patch.object(alpaca_bot, "datetime", At):
            open_, _ = alpaca_bot.check_market()
        self.assertFalse(open_)

    @unittest.expectedFailure
    def test_a_plain_restart_after_the_close_does_not_mail(self):
        """FINDING (cloud, 2026-10-08): Friday's last pass stamps 15:54, the daemon restarts Saturday 20:00 with the machine
        still up. Nothing was missed and nothing went down, but the real market_minutes_since says 1, so a mail goes out."""
        sent, _ = report("2026-10-09T15:54", "2026-10-10 20:00", rebooted=False)
        self.assertEqual(sent, [], "mailed: %r" % (sent[0][0] if sent else None))

    @unittest.expectedFailure
    def test_a_plain_overnight_restart_does_not_mail(self):
        sent, _ = report("2026-10-07T15:54", "2026-10-08 07:00", rebooted=False)
        self.assertEqual(sent, [], "mailed: %r" % (sent[0][0] if sent else None))


if __name__ == "__main__":
    unittest.main()
