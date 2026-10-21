"""CROSS-AUDIT of rh_daemon.alert_keep_awake (laptop 65d342f, 2026-10-20): the REAL function, called the way main() calls it.

alert_keep_awake emails once a day when the Modern Standby hold did not take. It writes the day's dampener to the ledger BEFORE it sends, and the
send's True/False is returned but never used to undo that. main() calls it seconds after boot, which is exactly when the network may not be up
yet (40 seconds on 2026-10-08), so a failed first send is recorded as "alerted today" and no later call that day will try again. report_downtime
had the same shape and was fixed on 2026-10-14 by parking the report on the ledger and retrying it from the loop.

This alert also has a safety net the other did not: daily_digest.py reads rh_status.json["machine"]["keep_awake"] and flags a hold that is not
held every evening. So the loss is bounded to that day's email, not silent. The defect is still worth fixing because the email is the only
alert that arrives while the hold is broken.

Only notify, log and the ledger writer are stubbed; the ledger is a plain dict (a helper of mine must never write the live rh_ledger.json).
The defect test is an EXPECTED FAILURE: it goes red as "unexpected success" when the owner (LAPTOP BOT) fixes it; delete the marker then.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D  # noqa: E402


class KeepAwakeAlert(unittest.TestCase):
    def setUp(self):
        self.sent, self.led, self.net = [], {}, {"up": True}
        for name, fn in (("notify", self._notify), ("log", lambda m: None),
                         ("_remember_alerts", lambda l, **kw: l.setdefault("alerts", {}).update(kw))):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def _notify(self, subject, body, untrusted=False):
        if self.net["up"]:
            self.sent.append(subject)
            return True
        return False

    def test_control_a_broken_hold_with_the_network_up_sends_once_and_only_once_that_day(self):
        if os.name != "nt":
            self.skipTest("alert_keep_awake only acts on Windows")
        self.assertTrue(D.alert_keep_awake(self.led, held=False))
        self.assertFalse(D.alert_keep_awake(self.led, held=False))             # a restart loop must not become a mail per restart
        self.assertEqual(len(self.sent), 1)

    def test_control_a_hold_that_took_sends_nothing(self):
        self.assertFalse(D.alert_keep_awake(self.led, held=True))
        self.assertEqual(self.sent, [])

    def test_a_first_send_that_failed_because_the_network_was_down_is_retried_later_the_same_day(self):
        if os.name != "nt":
            self.skipTest("alert_keep_awake only acts on Windows")
        self.net["up"] = False
        D.alert_keep_awake(self.led, held=False)                               # boot: hold failed, no network yet
        self.net["up"] = True
        D.alert_keep_awake(self.led, held=False)                               # a restart minutes later, network up
        self.assertEqual(len(self.sent), 1, "the alert was lost for the whole day: %r" % (self.led.get("alerts"),))


if __name__ == "__main__":
    unittest.main()
