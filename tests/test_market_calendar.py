"""alpaca_bot.check_market and its hardcoded holiday table, which gate every order (and the watchdog).

A wrong holiday list means the bot tries to trade a closed market, or the watchdog calls a closed
day an outage. The table is hardcoded and ends 2027-12-31, with nothing to say so before it does.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

ET = bot.ET_TZ


def at(y, m, d, h=10, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)


def clock(now):
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now
    return FixedDT


def is_open(now):
    with mock.patch.object(bot, "datetime", clock(now)):
        return bot.check_market()[0]


class SessionWindowTests(unittest.TestCase):
    def test_the_bots_window_is_0945_to_1555_inclusive(self):
        self.assertFalse(is_open(at(2026, 9, 30, 9, 44)))
        self.assertTrue(is_open(at(2026, 9, 30, 9, 45)))
        self.assertTrue(is_open(at(2026, 9, 30, 15, 55)))
        self.assertFalse(is_open(at(2026, 9, 30, 15, 56)))

    def test_weekends_are_closed(self):
        self.assertFalse(is_open(at(2026, 10, 3, 11)))      # Saturday
        self.assertFalse(is_open(at(2026, 10, 4, 11)))      # Sunday
        self.assertTrue(is_open(at(2026, 10, 2, 11)))       # the Friday before: control

    def test_the_window_holds_after_the_clocks_change(self):
        self.assertTrue(is_open(at(2026, 11, 2, 9, 45)))    # first Monday after the fall-back
        self.assertFalse(is_open(at(2026, 11, 2, 9, 44)))
        self.assertTrue(is_open(at(2026, 3, 9, 9, 45)))     # first Monday after the spring-forward


class HolidayTableTests(unittest.TestCase):
    HOLIDAYS = sorted(bot.MARKET_HOLIDAYS)

    def test_every_listed_holiday_is_closed_all_day_and_the_day_after_is_not(self):
        for d in self.HOLIDAYS:
            y, m, dd = map(int, d.split("-"))
            with self.subTest(day=d):
                self.assertFalse(is_open(at(y, m, dd, 11)))

    def test_every_holiday_is_a_weekday(self):
        """A holiday on a weekend means a mistyped date (the real observed date was missed)."""
        for d in self.HOLIDAYS:
            self.assertLess(date.fromisoformat(d).weekday(), 5, d)

    def test_ten_holidays_a_year(self):
        for y in ("2026", "2027"):
            self.assertEqual(len([d for d in self.HOLIDAYS if d.startswith(y)]), 10, y)

    def test_the_observed_date_cases_are_right(self):
        """July 4 on a Saturday or Sunday, Juneteenth on a Saturday, Christmas on a Saturday."""
        for d in ("2026-07-03", "2027-07-05", "2027-06-18", "2027-12-24"):
            self.assertIn(d, bot.MARKET_HOLIDAYS)
        for d in ("2026-07-04", "2027-07-04", "2027-06-19", "2027-12-25"):
            self.assertNotIn(d, bot.MARKET_HOLIDAYS)          # the weekend itself is not the holiday

    def test_ordinary_days_around_a_holiday_are_open(self):
        self.assertTrue(is_open(at(2026, 11, 25, 11)))
        self.assertTrue(is_open(at(2026, 11, 27, 11)))        # the day after Thanksgiving, before its 1pm close

    def test_the_table_does_not_run_out_unnoticed(self):
        """The table is hardcoded and the bot would trade a closed market on the first uncovered
        holiday. Fails 150 days before the last covered year ends, which is the cue to extend it."""
        last_year = max(int(d[:4]) for d in bot.MARKET_HOLIDAYS)
        self.assertGreater(date(last_year, 12, 31), date.today() + timedelta(days=150),
                           "MARKET_HOLIDAYS ends %d: add the next year's NYSE holidays" % last_year)


class NextRunTests(unittest.TestCase):
    def next_run(self, now):
        with mock.patch.object(bot, "datetime", clock(now)):
            return bot.next_market_run(now).astimezone(ET)

    def test_after_the_close_it_is_the_next_sessions_open(self):
        self.assertEqual(self.next_run(at(2026, 9, 30, 17)), at(2026, 10, 1, 9, 45))

    def test_it_skips_a_holiday(self):
        self.assertEqual(self.next_run(at(2026, 11, 25, 17)), at(2026, 11, 27, 9, 45))   # Thursday is Thanksgiving

    def test_it_skips_a_weekend_and_a_monday_holiday(self):
        self.assertEqual(self.next_run(at(2026, 9, 4, 17)), at(2026, 9, 8, 9, 45))       # Labor Day Monday

    def test_before_the_open_it_is_today(self):
        self.assertEqual(self.next_run(at(2026, 9, 30, 8)), at(2026, 9, 30, 9, 45))


class KnownGapTests(unittest.TestCase):
    @unittest.expectedFailure     # remove when half-days are modelled (needs Devon's OK: it changes live gating)
    def test_the_engine_stops_at_the_early_close_on_half_days(self):
        """NYSE closes at 1pm ET on the day after Thanksgiving and on Christmas Eve (2026-11-27,
        2026-12-24). The gate stays open until 15:55, so the bot would send orders into a closed
        market for about three hours on those days. Known and commented in alpaca_bot.py; pinned here
        so it cannot be forgotten. Not fixed unilaterally: it is a change to live gating."""
        for d in ((2026, 11, 27), (2026, 12, 24)):
            self.assertFalse(is_open(at(*d, 14, 0)), d)


if __name__ == "__main__":
    unittest.main()
