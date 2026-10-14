"""One unconfirmed broker reading must not move Arm B's published equity.

rh_daemon.persist() publishes led["broker_total_value"] instead of the computed equity
when they disagree, because computed equity uses broker BUYING POWER, which excludes
unsettled sale proceeds. That substitution earned its place: after the 2026-08-24
wind-down sold 24 positions, publishing the computed figure put ~$124 in rh_status.json
for a full trading day while the account held ~$232 - a phantom 48% crash in the file
CLOUD's tools and the Sunday audit measure Arm B from.

It also trusted ONE reading with no bound. On 2026-10-13 a single reconcile returned a
broker total about $10 high and this file published equity 318.60 at ts 13:36 and 13:51
against a true ~308.4 - 3.2% on a $308 book, committed twice, corrected by the next
reconcile. The benchmark index moved 0.03% that day, so none of it was a move.

THE DISCRIMINATOR IS PERSISTENCE, NOT PLAUSIBILITY. The first guard written for this
asked whether anything could EXPLAIN the gap - a sale inside the settlement window, a
recent deposit - and the real incident disproves that: a deposit had been recorded the
day before, so the bad reading was perfectly explicable, and still wrong. A real
settlement gap survives to the next reconcile; that $10 was gone in fifteen minutes.

So a large gap is believed when two consecutive readings agree, and otherwise the
candidate closer to the already-published equity wins. Both real incidents are pinned
below, in both directions, because the cure must not become the 08-24 disease.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D


class PickEquity(unittest.TestCase):
    """The rule itself. (computed, broker, prev_reading, last_published)."""

    def pick(self, computed, broker, prev, published):
        return D._pick_equity(computed, broker, prev, published)

    # ---- the two real incidents ------------------------------------------------------

    def test_the_2026_10_13_bad_high_reading_is_held_back(self):
        value, held = self.pick(308.77, 318.60, 308.91, 308.91)
        self.assertEqual(value, 308.77, "must publish the computed figure, not the glitch")
        self.assertEqual(held, 318.60, "and record what it refused, so nothing is hidden")

    def test_the_2026_08_24_wind_down_still_substitutes_at_once(self):
        """The regression that matters most: this is the phantom 48% crash case.

        Unsettled proceeds are real and persist, so the reading before it agrees and the
        broker total is published immediately - no cycle of publishing ~$124.
        """
        value, held = self.pick(124.00, 232.00, 232.00, 232.10)
        self.assertEqual(value, 232.00)
        self.assertIsNone(held)

    def test_the_wind_down_is_still_safe_with_no_previous_reading(self):
        """A restart clears nothing here (the reading is on the ledger), but prove the
        continuity tie-break alone would also have got 08-24 right."""
        value, _ = self.pick(124.00, 232.00, None, 232.10)
        self.assertEqual(value, 232.00)

    # ---- the rule's shape ------------------------------------------------------------

    def test_a_small_gap_substitutes_exactly_as_before(self):
        value, held = self.pick(308.70, 309.90, 300.00, 308.80)
        self.assertEqual((value, held), (309.90, None))
        self.assertLessEqual(abs(309.90 - 308.70), D.EQUITY_JUMP_MAX)

    def test_two_agreeing_readings_are_believed_however_big_the_gap(self):
        """This is what lets a genuine step change be adopted on the following pass."""
        value, held = self.pick(308.77, 318.60, 318.55, 308.91)
        self.assertEqual((value, held), (318.60, None))

    def test_a_bad_LOW_reading_is_held_back_too(self):
        """Guard both directions. An under-reading is how 08-24 hurt in the first place."""
        value, held = self.pick(308.77, 298.90, 308.91, 308.91)
        self.assertEqual((value, held), (308.77, 298.90))

    def test_with_no_anchors_it_trusts_the_arithmetic(self):
        value, held = self.pick(308.77, 318.60, None, None)
        self.assertEqual((value, held), (308.77, 318.60))

    def test_no_computed_figure_leaves_the_broker_total_alone(self):
        value, held = self.pick(None, 318.60, None, 308.91)
        self.assertEqual((value, held), (318.60, None))

    def test_a_genuine_step_change_is_adopted_on_the_second_pass(self):
        """End to end over two passes: held back once, then corroborated and published."""
        first, held = self.pick(308.77, 358.60, 308.91, 308.91)      # a $50 deposit lands
        self.assertEqual((first, held), (308.77, 358.60))
        second, held2 = self.pick(308.77, 358.60, 358.60, 308.77)    # next reading agrees
        self.assertEqual((second, held2), (358.60, None))


class PersistUsesIt(unittest.TestCase):
    """The wiring, and that it remembers the READING rather than the published value."""

    def setUp(self):
        # save_ledger and _save are stubbed because a test of mine wrote its fixture dict
        # over the live rh_ledger.json on 2026-10-13 - gitignored, no backup, real
        # positions and cash. _save is the floor under every writer in the module.
        for name, fn in (("save_ledger", lambda led: None),
                         ("_save", self._refuse),
                         ("log", lambda m: None),
                         ("_push_status", lambda reason: None),
                         ("email_trades", lambda *a, **k: None)):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _refuse(path, obj):
        raise AssertionError("a test tried to write the real file %r" % path)

    def ledger(self, broker_total, prev=None):
        import time
        led = {"positions": [], "holds": {}, "orders_today": 0, "cash": 1.0,
               "broker_total_value": broker_total, "broker_total_at": time.time()}
        if prev is not None:
            led["broker_total_prev"] = prev
        return led

    def run_persist(self, led, computed, last_published):
        snap = {}
        with mock.patch.object(D, "_load",
                               lambda p, d: {"equity": last_published} if p == D.STATUS_F else d), \
             mock.patch.object(D, "_save", lambda p, o: snap.update(o) if p == D.STATUS_F else None):
            D.persist(led, {"snapshot": {"equity": computed}}, [])
        return snap

    def test_persist_holds_back_the_bad_sample_and_says_so_in_the_file(self):
        led = self.ledger(318.60, prev=308.91)
        snap = self.run_persist(led, computed=308.77, last_published=308.91)
        self.assertEqual(snap.get("equity"), 308.77)
        self.assertEqual(snap.get("broker_total_value_held_back"), 318.60)
        self.assertIn("held back", snap.get("equity_source", ""))

    def test_persist_remembers_the_reading_even_when_it_rejected_it(self):
        """Or a persistent gap could never corroborate itself on the next pass."""
        led = self.ledger(318.60, prev=308.91)
        self.run_persist(led, computed=308.77, last_published=308.91)
        self.assertEqual(led.get("broker_total_prev"), 318.60)

    def test_persist_substitutes_and_labels_a_normal_settlement_gap(self):
        led = self.ledger(232.00, prev=232.00)
        snap = self.run_persist(led, computed=124.00, last_published=232.10)
        self.assertEqual(snap.get("equity"), 232.00)
        self.assertEqual(snap.get("equity_source"), "broker_total_value")
        self.assertEqual(snap.get("unsettled_excluded_from_buying_power"), 108.00)


if __name__ == "__main__":
    unittest.main()
