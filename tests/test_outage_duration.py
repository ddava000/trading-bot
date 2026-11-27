"""Arm A's "Alpaca unreachable" and "reachable again" emails must count TRADING time, not wall-clock time.

2026-10-07: LAPTOP BOT found its own alert path reporting "~1069 minutes" for ONE failed attempt, because the
daemon was idle overnight and nothing asked the broker for 17 hours. Arm A had the same error latent in
alpaca_bot.outage_note_contact / outage_note_blind (wall-clock _hours_since): one failed run just before the
15:55 close and a good run at the next open would have emailed "unreachable for about 18.0h". It had not fired
(no outage state was ever left across a close), which is exactly why it was found by reading, not by an alert.

Nothing else about the alerts changed, and the tests pin that too: the first blind window still alerts at once,
repeats are still rate-limited on the wall clock, recovery still always sends one note, a corrupt stamp is
unknown rather than zero.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

UTC = timezone.utc


def et(y, m, d, h, mi):
    """An Eastern wall-clock moment as UTC."""
    return datetime(y, m, d, h, mi, tzinfo=bot.ET_TZ).astimezone(UTC)


def frozen(now):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    return FixedDT


class MarketMinutesTests(unittest.TestCase):
    def mins(self, a, b):
        return bot.market_minutes_between(a, b)

    def test_inside_one_session_it_is_the_plain_difference(self):
        self.assertAlmostEqual(self.mins(et(2026, 10, 7, 10, 0), et(2026, 10, 7, 11, 30)), 90)

    def test_a_whole_session_is_370_minutes(self):
        self.assertAlmostEqual(self.mins(et(2026, 10, 7, 0, 0), et(2026, 10, 7, 23, 59)), 370)

    def test_overnight_counts_only_the_two_edges(self):
        """15:45 Tuesday to 09:50 Wednesday: 10 minutes before the close, 5 after the open."""
        self.assertAlmostEqual(self.mins(et(2026, 10, 6, 15, 45), et(2026, 10, 7, 9, 50)), 15)

    def test_the_exact_incident_on_the_laptop_17_hours_for_one_attempt(self):
        wall = (et(2026, 10, 6, 15, 45) - et(2026, 10, 5, 22, 0)).total_seconds() / 60
        self.assertGreater(wall, 1000)
        self.assertAlmostEqual(self.mins(et(2026, 10, 5, 22, 0), et(2026, 10, 6, 9, 50)), 5)

    def test_a_weekend_adds_nothing(self):
        self.assertAlmostEqual(self.mins(et(2026, 10, 2, 15, 45), et(2026, 10, 5, 9, 45)), 10)      # Fri to Mon open

    def test_a_holiday_adds_nothing(self):
        """Labor Day 2026-09-07: Friday 15:45 to Tuesday 09:55 is 10 minutes Friday + 10 minutes Tuesday."""
        self.assertAlmostEqual(self.mins(et(2026, 9, 4, 15, 45), et(2026, 9, 8, 9, 55)), 20)

    def test_the_clock_change_does_not_move_the_window(self):
        """Fall back on 2026-11-01: Friday 15:45 EDT to Monday 09:45 EST is still 10 minutes."""
        a, b = et(2026, 10, 30, 15, 45), et(2026, 11, 2, 9, 45)
        self.assertEqual((a.hour, b.hour), (19, 14))                       # UTC hours differ by the shift
        self.assertAlmostEqual(self.mins(a, b), 10)

    def test_backwards_or_empty_is_zero(self):
        t = et(2026, 10, 7, 10, 0)
        self.assertEqual(self.mins(t, t), 0.0)
        self.assertEqual(self.mins(t + timedelta(hours=1), t), 0.0)

    def test_multiple_sessions_add_up(self):
        self.assertAlmostEqual(self.mins(et(2026, 10, 6, 9, 45), et(2026, 10, 7, 15, 55)), 740)


class Harness(unittest.TestCase):
    def setUp(self):
        self.sent, self.state, self.saved = [], {}, []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(bot, "send_email", lambda s, b: self.sent.append((s, b)) or True))
        stack.enter_context(mock.patch.object(bot, "_outage_load", lambda: dict(self.state)))
        stack.enter_context(mock.patch.object(bot, "_outage_save", lambda d: (self.saved.append(dict(d)), self.state.clear(), self.state.update(d))))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack = stack

    def at(self, now):
        self.stack.enter_context(mock.patch.object(bot, "datetime", frozen(now)))

    def iso(self, dt):
        return dt.astimezone(UTC).isoformat(timespec="seconds")


class ReachableAgainTests(Harness):
    def test_one_failed_run_before_the_close_then_contact_at_the_open_is_minutes_not_hours(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 6, 15, 45))}
        self.at(et(2026, 10, 7, 9, 50))
        bot.outage_note_contact()
        subject, body = self.sent[0]
        self.assertIn("reachable again", subject)
        self.assertIn("about 15 minutes of trading time", body)
        self.assertNotIn("18", body)                       # the wall clock said about 18 hours
        self.assertEqual(self.saved[-1], {})                # state cleared

    def test_a_real_outage_inside_the_session_still_reports_its_length(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 7, 11, 11))}
        self.at(et(2026, 10, 7, 14, 41))
        bot.outage_note_contact()
        self.assertIn("about 3.5h of trading time", self.sent[0][1])

    def test_it_says_when_the_outage_was_under_a_minute_of_trading_time(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 7, 10, 0))}
        self.at(et(2026, 10, 7, 10, 0) + timedelta(seconds=20))
        bot.outage_note_contact()
        self.assertIn("under a minute of trading time", self.sent[0][1])

    def test_a_corrupt_stamp_gives_no_duration_but_still_sends_the_note(self):
        self.state = {"unreachable_since": "not a time"}
        self.at(et(2026, 10, 7, 10, 0))
        bot.outage_note_contact()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("unreachable for about", self.sent[0][1])

    def test_no_outage_on_file_sends_nothing(self):
        self.at(et(2026, 10, 7, 10, 0))
        bot.outage_note_contact()
        self.assertEqual((self.sent, self.saved), ([], []))


class BlindWindowTests(Harness):
    def test_the_first_blind_window_alerts_at_once_without_a_duration(self):
        self.at(et(2026, 10, 7, 10, 0))
        bot.outage_note_blind()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("Unreachable for about", self.sent[0][1])
        self.assertIn("unreachable_since", self.saved[-1])

    def test_a_next_morning_blind_window_still_alerts_but_does_not_claim_a_long_outage(self):
        """Wall-clock gap since the last alert (18h) means due; trading time since the first failure is 15 minutes."""
        self.state = {"unreachable_since": self.iso(et(2026, 10, 6, 15, 45)),
                      "last_alert_utc": self.iso(et(2026, 10, 6, 15, 46))}
        self.at(et(2026, 10, 7, 9, 50))
        bot.outage_note_blind()
        self.assertEqual(len(self.sent), 1)                                  # still alerts: fail loud
        self.assertNotIn("Unreachable for about", self.sent[0][1])           # and no 18 hours

    def test_a_genuinely_long_outage_reports_trading_time_so_far(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 7, 10, 0)),
                      "last_alert_utc": self.iso(et(2026, 10, 7, 10, 1))}
        self.at(et(2026, 10, 7, 13, 0))
        bot.outage_note_blind()
        self.assertIn("Unreachable for about 3.0h of trading time so far.", self.sent[0][1])

    def test_repeats_inside_the_backoff_are_suppressed_on_the_wall_clock_as_before(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 7, 10, 0)),
                      "last_alert_utc": self.iso(et(2026, 10, 7, 11, 30))}
        self.at(et(2026, 10, 7, 12, 0))                                       # 0.5h since the last alert
        bot.outage_note_blind()
        self.assertEqual(self.sent, [])

    def test_a_corrupt_last_alert_stamp_alerts_rather_than_skips(self):
        self.state = {"unreachable_since": self.iso(et(2026, 10, 7, 10, 0)), "last_alert_utc": "garbage"}
        self.at(et(2026, 10, 7, 10, 20))
        bot.outage_note_blind()
        self.assertEqual(len(self.sent), 1)


class FormattingTests(unittest.TestCase):
    def test_formats(self):
        f = bot._fmt_trading_time
        self.assertIsNone(f(None))
        self.assertEqual(f(0.0), "under a minute of trading time")
        self.assertEqual(f(0.25), "15 minutes of trading time")
        self.assertEqual(f(2.0), "2.0h of trading time")

    def test_unparseable_is_none_not_zero(self):
        self.assertIsNone(bot._market_hours_since("???"))
        self.assertIsNone(bot._market_hours_since(None))


if __name__ == "__main__":
    unittest.main()
