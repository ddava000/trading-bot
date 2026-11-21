"""CROSS-AUDIT of rh_daemon.persist() / _pick_equity (laptop 185874e, 2026-10-14): the REAL persist, called the way the daemon calls it.

The guard believes a large broker/computed gap when two CONSECUTIVE broker readings agree. persist() remembers the reading in
led["broker_total_prev"] on EVERY call, but broker_total_value is only refreshed by a reconcile (at most every RECONCILE_MAX_AGE_SEC, 30
minutes), while persist runs every cycle (15 minutes). So one reconcile reading is persisted on two cycles in a row, and on the second the "previous
reading" is that same reading: it agrees with itself and is published. That is exactly how 318.60 reached rh_status.json at 13:36 AND 13:51 on
2026-10-13 (one reconcile, two persists; the two samples carry the same broker total).

tests/test_equity_guard.py pins _pick_equity as a pure function and persist() once; it never calls persist() twice on the same cached reading, so
it could not see this. Only the clock, the files, git, mail and log are stubbed here, exactly as in the laptop's own tests, and the ledger is a plain dict
(a helper of mine must never write the live rh_ledger.json: the 2026-10-13 incident).

The defect test is an EXPECTED FAILURE: it turns red as "unexpected success" when the owner (LAPTOP BOT) fixes it, which is the cue to delete the marker in
that commit. The controls prove the harness can say "held back" and "believed", so the failing one is not failing for a reason of its own making.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D  # noqa: E402


def persist_once(led, computed, published_before):
    """One REAL persist() call. Returns what would be written to rh_status.json."""
    out = {}

    def refuse(path, obj):
        if path == D.STATUS_F:
            out.update(obj)
        else:
            raise AssertionError("a test tried to write the real file %r" % path)

    with mock.patch.object(D, "save_ledger", lambda l: None), mock.patch.object(D, "log", lambda m: None), \
         mock.patch.object(D, "_push_status", lambda r: None), mock.patch.object(D, "email_trades", lambda *a, **k: None), \
         mock.patch.object(D, "_load", lambda p, d: {"equity": published_before} if p == D.STATUS_F else d), \
         mock.patch.object(D, "_save", refuse):
        D.persist(led, {"snapshot": {"equity": computed}}, [])
    return out


def ledger(bt, prev=None, at=None):
    led = {"positions": [], "holds": {}, "orders_today": 0, "cash": 1.0, "broker_total_value": bt,
           "broker_total_at": time.time() if at is None else at}
    if prev is not None:
        led["broker_total_prev"] = prev
    return led


class OneReadingPersistedTwice(unittest.TestCase):
    def test_control_the_first_persist_of_a_bad_reading_is_held_back(self):
        """Present case: the harness can say 'held back'."""
        led = ledger(318.60, prev=308.91)
        s = persist_once(led, computed=308.67, published_before=308.91)
        self.assertEqual(s["equity"], 308.67)
        self.assertEqual(s.get("broker_total_value_held_back"), 318.60)

    def test_control_a_genuine_step_change_is_still_believed_on_the_NEXT_RECONCILE(self):
        """Present case, the other direction: a real $50 deposit must not be held back forever. A SECOND, distinct reconcile that agrees is believed."""
        led = ledger(358.60, prev=308.91)
        first = persist_once(led, computed=308.67, published_before=308.91)
        self.assertEqual(first["equity"], 308.67)                                  # held back once
        led["broker_total_value"], led["broker_total_at"] = 358.55, time.time() + 5     # a NEW reconcile, agreeing within 0.50
        second = persist_once(led, computed=308.60, published_before=first["equity"])
        self.assertEqual(second["equity"], 358.55)
        self.assertNotIn("broker_total_value_held_back", second)

    def test_the_same_cached_reading_on_the_next_cycle_is_not_corroboration(self):
        """WAS cloud's FINDING of 2026-10-14, now the regression against it.

        No new reconcile, the SAME broker_total_value and the SAME broker_total_at, one persist cycle
        later. The remembered 'previous reading' used to be that very reading, so it corroborated
        itself and 318.60 was published, exactly as on 10-13. FIXED by laptop the same day: persist()
        rotates prev only when broker_total_at changes, so corroboration needs two DISTINCT
        reconciles. expectedFailure removed once both of these flipped to unexpected success."""
        led = ledger(318.60, prev=308.91)
        first = persist_once(led, computed=308.67, published_before=308.91)
        self.assertEqual(first["equity"], 308.67)
        second = persist_once(led, computed=308.53, published_before=first["equity"])
        self.assertEqual(second["equity"], 308.53, "the one bad reading corroborated itself and was published: %r" % (second.get("equity"),))

    def test_it_stays_held_back_across_several_cycles_until_a_reconcile_says_otherwise(self):
        led = ledger(318.60, prev=308.91)
        published = 308.91
        for computed in (308.67, 308.53, 308.60, 308.58):
            published = persist_once(led, computed=computed, published_before=published)["equity"]
        self.assertEqual(published, 308.58)


if __name__ == "__main__":
    unittest.main()
