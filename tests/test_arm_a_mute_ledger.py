"""The mute's two cloud-found defects, exercised the way the daemon actually runs.

Cloud cross-audited the queued mute on 2026-10-25 and pinned two real defects in
tests/test_arm_a_mute_real.py (theirs):
  (a) a queued line arriving WITH a first-level stale line got the whole body dropped,
      including the stale line the mute promised to leave alone;
  (b) the mute returns True, so run_once believed the alert was delivered and sent
      "Arm A is running again" at the end of the episode - telling Devon an alert he
      never received had cleared.

Both are fixed in rh_daemon's _deliver. (a) flips their marker on its own. (b) CANNOT
pass their harness, and that is a property of the harness rather than of the fix: their
Base stubs _remember_alerts to `lambda l, **k: None` and their episode() helper passes a
fresh `{}` to each check_arm_a call. A fix that remembers "this alert was muted" anywhere
durable is therefore invisible to it, and the only fix it could accept is a module global -
which is exactly what this file has got wrong five times and what cloud themselves argued
should live on the ledger.

So this file runs the same two checks in the shape main() uses: ONE ledger object passed to
every pass, and a recorder that actually records. Cloud's `row` and `NOW` are imported
rather than rebuilt - my first attempt at my own row() passed created_at as a string and
the module raised on comparing it to a datetime, which is the third test double to be the
defect in one day and the first of those three that was mine.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")

import rh_daemon as D
import arm_a_watch as A
try:
    # `discover -s tests` puts tests/ itself on the path, so the bare name works everywhere it is run that way (CI included). The
    # package form `tests.test_...` breaks on any machine that has ANOTHER top-level package called `tests` installed (CLOUD's
    # desktop does, in site-packages, and it shadows this directory): ModuleNotFoundError at import, 2026-10-27.
    from test_arm_a_mute_real import NOW, REAL_RUN_ONCE, row   # cloud's verified double
except ImportError:
    from tests.test_arm_a_mute_real import NOW, REAL_RUN_ONCE, row


class TheMuteRemembersAcrossChecks(unittest.TestCase):
    def setUp(self):
        self.sent, self.logged, self.led = [], [], {}
        for name, fn in (
            ("notify", lambda s, b, untrusted=False: (self.sent.append(s), True)[1]),
            ("log", self.logged.append),
            # The real recorder's EFFECT on the ledger dict, without touching disk. Cloud's
            # harness makes this a no-op, which is why their (b) marker cannot flip.
            ("_remember_alerts", lambda l, **k: l.setdefault("alerts", {}).update(k)),
            ("save_ledger", lambda l: None),
            ("_save", self._refuse),
        ):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)
        D._ARM_A_FIRED.clear()
        self.addCleanup(D._ARM_A_FIRED.clear)

    @staticmethod
    def _refuse(path, obj):
        raise AssertionError("a test tried to write the real file %r" % path)

    def episode(self, muted=True):
        """A 7-minute queued run, then a healthy check - ONE ledger across both, as main() does."""
        state = {"now": NOW, "runs": [row(NOW - timedelta(minutes=7), "queued", 1)],
                 "commit": NOW - timedelta(minutes=10)}

        def drive(**kw):
            with contextlib.redirect_stdout(io.StringIO()):
                return REAL_RUN_ONCE(now=state["now"], fired=kw["fired"], deliver=kw["deliver"],
                                     get_commit_time=lambda ref: state["commit"],
                                     get_status=lambda ref: None,
                                     get_runs=lambda: state["runs"],
                                     get_job_state=lambda rid: False, sender="LAPTOP BOT")
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", muted), \
             mock.patch.object(A, "run_once", drive):
            D._ARM_A_NEXT = 0
            D.check_arm_a(self.led)                       # the false queued alert
            state["now"] = NOW + timedelta(minutes=5)
            state["runs"] = [row(state["now"] - timedelta(minutes=2), "in_progress", 2)]
            state["commit"] = state["now"] - timedelta(minutes=3)
            D._ARM_A_NEXT = 0
            D.check_arm_a(self.led)                       # the row finally says what it should
        return self.sent

    def test_control_without_the_mute_both_notices_are_sent(self):
        """Proves the harness can deliver: if this did not, the quiet below would mean nothing."""
        subjects = self.episode(muted=False)
        self.assertEqual(len(subjects), 2, subjects)
        self.assertIn("is late", subjects[0])
        self.assertIn("running again", subjects[1])

    def test_no_recovery_notice_follows_a_muted_alert(self):
        """Cloud's defect (b), fixed: nothing at all reaches Devon for a muted episode."""
        subjects = self.episode(muted=True)
        self.assertEqual(subjects, [],
                         "a muted episode must be silent in BOTH directions: %r" % (subjects,))
        self.assertTrue(any("SWALLOWED" in m for m in self.logged),
                        "and the swallow must leave a record: %r" % (self.logged,))

    def test_the_mute_flag_is_set_then_cleared_on_the_ledger(self):
        """The state lives where a restart cannot lose it, and does not leak past the episode."""
        self.episode(muted=True)
        self.assertIs((self.led.get("alerts") or {}).get("arm_a_queued_muted"), False,
                      "cleared when the recovery was swallowed, so the next episode starts fresh")

    def test_a_restart_between_the_alert_and_the_recovery_still_swallows(self):
        """Why the ledger and not a module global: the sixth time this lesson applies here.

        The daemon restarts for a code pull mid-episode. _ARM_A_FIRED is reloaded from the
        ledger by main(); the mute flag has to survive the same way, or the recovery for a
        muted alert arrives after the restart.
        """
        state = {"now": NOW, "runs": [row(NOW - timedelta(minutes=7), "queued", 1)],
                 "commit": NOW - timedelta(minutes=10)}

        def drive(**kw):
            with contextlib.redirect_stdout(io.StringIO()):
                return REAL_RUN_ONCE(now=state["now"], fired=kw["fired"], deliver=kw["deliver"],
                                     get_commit_time=lambda ref: state["commit"],
                                     get_status=lambda ref: None,
                                     get_runs=lambda: state["runs"],
                                     get_job_state=lambda rid: False, sender="LAPTOP BOT")
        with mock.patch.object(D, "ARM_A_MUTE_QUEUED", True), \
             mock.patch.object(A, "run_once", drive):
            D._ARM_A_NEXT = 0
            D.check_arm_a(self.led)
            self.assertIs((self.led.get("alerts") or {}).get("arm_a_queued_muted"), True)
            # the restart: module state goes, the ledger stays
            D._ARM_A_FIRED.clear()
            state["now"] = NOW + timedelta(minutes=5)
            state["runs"] = [row(state["now"] - timedelta(minutes=2), "in_progress", 2)]
            state["commit"] = state["now"] - timedelta(minutes=3)
            D._ARM_A_NEXT = 0
            D.check_arm_a(self.led)
        self.assertEqual(self.sent, [], "a restart must not resurrect the recovery: %r" % (self.sent,))


if __name__ == "__main__":
    unittest.main()
