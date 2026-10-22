"""The laptop's first-level QUEUED mute: narrow, fail-open, and built on cloud's real words.

Devon's call, 2026-10-22, after ten alerts in one day about a healthy Arm A - five
late-and-recovered pairs at 10:23, 10:40, 10:51, 12:39 and 13:37, while Arm A pushed
status.json dead on its 15-minute cadence throughout. Cloud shipped job-truth and a second
look at 12:54 (live on this laptop from 13:08) and the 13:37 pair fired anyway, which is
the limit cloud themselves wrote down: if GitHub's jobs endpoint lags the way the run row
does, reading the job cannot save the alert.

So this is a STOPGAP in rh_daemon, not a fix in arm_a_watch, and the tests below pin the
three properties that make it safe to leave running:

  * it suppresses ONLY the non-urgent queued alert. The urgent 20-minute level, no_trigger
    and stale all still deliver, so a run that never starts, a trigger that stops and a
    stale status file still reach Devon.
  * it FAILS OPEN. The match is on the queued description's own wording, so if cloud ever
    rewords it the alert gets delivered - a dead mute is noisy, never silent.
  * the bodies here come from cloud's REAL describe(), not from a string I typed. If their
    wording changes, this test fails and tells me the mute is dead, instead of my inbox
    going quiet about something real. That is the test-double lesson from this morning
    applied to my own file: a double that cannot express the other system's behaviour
    certifies nothing.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D
import arm_a_watch as A

NOW = datetime(2026, 10, 22, 17, 40, tzinfo=timezone.utc)


def real_body(signal, minutes):
    """The alert body cloud's own code produces for one signal. Never a hand-typed string."""
    levels = {"queued": 0.0, "no_trigger": 0.0, "stale": 0.0}
    levels[signal] = minutes
    crossings = A.crossings(levels, window=50)
    assert crossings, "fixture produced no crossing for %s at %s" % (signal, minutes)
    return A.describe(crossings[0], NOW, [])


class TheMuteIsNarrow(unittest.TestCase):
    def setUp(self):
        self.sent, self.logged = [], []
        for name, fn in (("notify", lambda s, b, untrusted=False: (self.sent.append(s), True)[1]),
                         ("log", self.logged.append)):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def deliver(self, body, urgent, muted=True):
        """Drive the real _deliver that check_arm_a hands to run_once."""
        captured = {}

        # Call the callback INSIDE the patch block. My first draft captured it here and
        # invoked it after the `with` had exited, so ARM_A_MUTE_QUEUED had already
        # reverted to its real value and the flag-off test failed for the wrong reason.
        # Same shape as the clock-dependent test on 10-21: state that changed between
        # arranging the fixture and asserting on it.
        def fake_run_once(**kw):
            captured["result"] = kw["deliver"](
                "ARM A WATCH: Arm A is late (checked by LAPTOP BOT)", body, urgent)
            return 0, "line", []
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", muted), \
             mock.patch.object(A, "run_once", fake_run_once), \
             mock.patch.object(D, "_remember_alerts", lambda l, **k: None):
            D._ARM_A_NEXT = 0
            D.check_arm_a({})
        return captured["result"]

    # ---- what it silences ------------------------------------------------------------

    def test_the_first_level_queued_alert_is_suppressed(self):
        ok = self.deliver(real_body("queued", 6.0), urgent=False)
        self.assertTrue(ok, "must report handled, or run_once retries it and goes red")
        self.assertEqual(self.sent, [])
        self.assertTrue(any("SUPPRESSED" in m for m in self.logged), self.logged)

    # ---- what it must NOT silence ----------------------------------------------------

    def test_the_urgent_queued_level_still_sends(self):
        """20 minutes means a run has waited a third of an hour for a machine."""
        self.deliver(real_body("queued", 20.0), urgent=True)
        self.assertEqual(len(self.sent), 1)

    def test_no_trigger_still_sends(self):
        """cron-job.org stopped firing: nothing to do with GitHub's queue."""
        self.deliver(real_body("no_trigger", 22.0), urgent=False)
        self.assertEqual(len(self.sent), 1)

    def test_stale_still_sends(self):
        self.deliver(real_body("stale", 35.0), urgent=False)
        self.assertEqual(len(self.sent), 1)

    def test_nothing_is_suppressed_when_the_flag_is_off(self):
        """The flag is the revert: it goes False the moment cloud's fix holds."""
        self.deliver(real_body("queued", 6.0), urgent=False, muted=False)
        self.assertEqual(len(self.sent), 1)

    # ---- fail-open, which is the whole safety argument -------------------------------

    def test_an_unrecognised_body_is_DELIVERED_not_dropped(self):
        """If cloud rewords the queued description, the mute must stop working loudly."""
        self.deliver("Some future wording that says nothing about a machine.", urgent=False)
        self.assertEqual(len(self.sent), 1)

    def test_the_marker_this_mute_depends_on_is_still_in_clouds_text(self):
        """The canary. If this fails, the mute above is dead and my inbox went quiet.

        Deliberately asserts against cloud's live describe() rather than a copy, so their
        rewording surfaces here rather than as silence.
        """
        self.assertIn("give it a machine", real_body("queued", 6.0))
        for other in ("no_trigger", "stale"):
            self.assertNotIn("give it a machine", real_body(other, 40.0),
                             "%s must not collide with the queued marker" % other)


if __name__ == "__main__":
    unittest.main()
