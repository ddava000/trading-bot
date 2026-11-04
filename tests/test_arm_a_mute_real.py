"""CROSS-AUDIT of rh_daemon's first-level QUEUED mute (laptop 4467e30 and c1f9284, 2026-10-22): the REAL code, driven the way
the daemon drives it.

LAPTOP BOT's own tests (tests/test_arm_a_mute.py) build one-signal bodies, which is the right start. Two situations they
cannot see, both reproducible with cloud's real arm_a_watch functions and the daemon's real check_arm_a:

  1. COMBINED ALERTS. build_alert lists every threshold crossed at one check in ONE body. A queued first-level crossing that
     arrives together with a stale first-level crossing is still non-urgent and still contains "give it a machine", so the
     mute drops the WHOLE body, including the stale line, which is real. (Queued with no_trigger cannot happen: a run that
     has waited N minutes was created N minutes ago, so no_trigger can never exceed queued, and 22 minutes is already the
     urgent queued level. The first draft of this file had a test for it; it was an input measure() cannot produce.)
  2. RECOVERY NOTICES. run_once treats a muted alert as delivered (the mute returns True), so when the episode ends it sends
     "Arm A is running again" through the same callback. That text does not contain the marker, so it is delivered: Devon is
     told Arm A is running again after an alert he never received.

History: both defects were found here as EXPECTED FAILURES and fixed by the owner (LAPTOP BOT). The recovery-notice marker was kept
on purpose for a while because this harness could not show the fix: each check got a fresh {} ledger and _remember_alerts was a no-op,
so a fix that remembers the muted alert on the ledger was invisible. Now `episode()` hands ONE ledger to both checks and the stub
records into it exactly as the real _remember_alerts does (setdefault("alerts").update, minus the disk write), and the marker is gone.
The controls prove the setup can deliver and can suppress, and that a LOST ledger still produces the misleading notice.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")

import rh_daemon as D  # noqa: E402
import arm_a_watch as A  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)          # a Tuesday, 14:00 ET, inside the session
REAL_RUN_ONCE = A.run_once


def row(created, status, rid):
    return {"id": rid, "status": status, "conclusion": None, "created_at": created}


class Base(unittest.TestCase):
    def setUp(self):
        self.sent, self.logged = [], []
        for name, fn in (("notify", lambda s, b, untrusted=False: (self.sent.append((s, b)), True)[1]),
                         ("log", self.logged.append),
                         ("_remember_alerts", self.remember)):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)
        D._ARM_A_FIRED.clear()
        self.addCleanup(D._ARM_A_FIRED.clear)

    @staticmethod
    def remember(led, **kw):
        """rh_daemon._remember_alerts without save_ledger: what the daemon keeps on its ledger survives into the next check."""
        led.setdefault("alerts", {}).update(kw)

    def deliver_combined(self, levels, muted=True):
        """The daemon's real _deliver, handed the body cloud's real build_alert makes for these signal levels."""
        crossings = A.crossings(levels, window=60)
        subject, body, urgent = A.build_alert(crossings, NOW, [], "LAPTOP BOT")
        captured = {}

        def fake_run_once(**kw):
            captured["result"] = kw["deliver"](subject, body, urgent)
            return 0, "line", []
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", muted), mock.patch.object(A, "run_once", fake_run_once):
            D._ARM_A_NEXT = 0
            D.check_arm_a({})
        return subject, body, urgent, captured["result"]

    def episode(self, muted=True, lose_ledger=False):
        """Real run_once, real daemon callback: one check with a run waiting 7 minutes, then a healthy check 5 minutes later.
        The SAME ledger dict goes to both checks, as in production (rh_daemon loads it once and passes it on). lose_ledger=True hands
        the second check a fresh one: what a restart that lost the ledger looks like."""
        ledger = {}
        state = {"now": NOW, "runs": [row(NOW - timedelta(minutes=7), "queued", 1)], "commit": NOW - timedelta(minutes=10)}

        def drive(**kw):
            with contextlib.redirect_stdout(io.StringIO()):
                return REAL_RUN_ONCE(now=state["now"], fired=kw["fired"], deliver=kw["deliver"],
                                     get_commit_time=lambda ref: state["commit"], get_status=lambda ref: None,
                                     get_runs=lambda: state["runs"], get_job_state=lambda rid: False, sender="LAPTOP BOT")
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", muted), mock.patch.object(A, "run_once", drive):
            D._ARM_A_NEXT = 0
            D.check_arm_a(ledger)                                       # the false alert (muted or not)
            state["now"] = NOW + timedelta(minutes=5)
            state["runs"] = [row(state["now"] - timedelta(minutes=2), "in_progress", 2)]
            state["commit"] = state["now"] - timedelta(minutes=3)
            D._ARM_A_NEXT = 0
            D.check_arm_a({} if lose_ledger else ledger)                # the row finally says what it should
        return [s for s, _ in self.sent]


class Controls(Base):
    def test_control_a_queued_only_first_level_alert_is_suppressed(self):
        _, body, urgent, ok = self.deliver_combined({"queued": 7.0, "no_trigger": 0.0, "stale": 0.0})
        self.assertFalse(urgent)
        self.assertIn("give it a machine", body)
        self.assertTrue(ok)
        self.assertEqual(self.sent, [])

    def test_control_the_same_combined_alert_is_delivered_when_the_flag_is_off(self):
        self.deliver_combined({"queued": 7.0, "no_trigger": 0.0, "stale": 36.0}, muted=False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("has not been committed", self.sent[0][1])

    def test_control_without_the_mute_an_episode_sends_late_then_running_again(self):
        subjects = self.episode(muted=False)
        self.assertEqual(len(subjects), 2, subjects)
        self.assertIn("is late", subjects[0])
        self.assertIn("running again", subjects[1])


class WhatTheMuteLets_Slip(Base):
    def test_a_stale_status_line_that_arrives_with_a_queued_line_is_still_delivered(self):
        """queued 7 min AND status.json uncommitted 36 min at the same check: the stale line is real and non-queued."""
        self.deliver_combined({"queued": 7.0, "no_trigger": 0.0, "stale": 36.0})
        self.assertTrue(any("has not been committed" in body for _, body in self.sent),
                        "the whole body was dropped, including the stale line: %r" % (self.sent,))

    # The marker that stood here (laptop, 2026-10-26: "the defect IS fixed, and this harness cannot show it") is removed
    # 2026-11-04: one ledger now goes to both checks and the stub records into it, which is the production shape. The
    # restart-mid-episode proof is still tests/test_arm_a_mute_ledger.py.
    def test_no_running_again_notice_is_sent_for_an_alert_the_mute_swallowed(self):
        subjects = self.episode(muted=True)
        self.assertEqual([s for s in subjects if "running again" in s], [],
                         "Devon was told Arm A is running again after an alert he never received: %r" % (subjects,))

    def test_control_a_lost_ledger_still_produces_the_misleading_notice(self):
        """So the test above is green because the ledger remembers, not because nothing can send the notice: with the ledger lost
        between the two checks (the state a restart WITHOUT the persisted flag leaves), the notice is delivered."""
        subjects = self.episode(muted=True, lose_ledger=True)
        self.assertTrue([s for s in subjects if "running again" in s], "a lost ledger must still deliver the notice: %r" % (subjects,))


if __name__ == "__main__":
    unittest.main()
