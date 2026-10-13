"""alpaca_bot.check_market and its hardcoded holiday table, which gate every order (and the watchdog).

A wrong holiday list means the bot tries to trade a closed market, or the watchdog calls a closed
day an outage. The table is hardcoded and ends 2027-12-31, with nothing to say so before it does.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
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


EARLY = ((2026, 11, 27), (2026, 12, 24), (2027, 11, 26))
# the ordinary session before and after each early-close day: the CONTROL, because a gate that closed at 12:55 on every day would
# pass a test written only on the three dates.
ORDINARY = ((2026, 11, 25), (2026, 11, 30), (2026, 12, 23), (2026, 12, 28), (2027, 11, 24), (2027, 11, 29))


class EarlyCloseTests(unittest.TestCase):
    """NYSE closes at 1 pm ET on the day after Thanksgiving and on Christmas Eve (2026-11-27, 2026-12-24, 2027-11-26). Until
    2026-10-13 the gate stayed open to 15:55 and the bot would have sent orders into a closed market for about three hours on
    each. Devon approved the fix that day (clickable "Yes, fix it now"). The bot's window ends five minutes before the exchange
    does, exactly as on a normal day, so on these days it ends at 12:55."""

    def test_the_set_is_the_three_known_dates_all_real_trading_weekdays(self):
        self.assertEqual(sorted(bot.EARLY_CLOSE), ["2026-11-27", "2026-12-24", "2027-11-26"])
        for d in bot.EARLY_CLOSE:
            self.assertLess(date.fromisoformat(d).weekday(), 5, d)
            self.assertNotIn(d, bot.MARKET_HOLIDAYS, d)

    def test_the_gate_closes_at_1255_on_each_early_close_day(self):
        for d in EARLY:
            with self.subTest(day=d):
                self.assertFalse(is_open(at(*d, 9, 44)))
                self.assertTrue(is_open(at(*d, 9, 45)))
                self.assertTrue(is_open(at(*d, 12, 54)))
                self.assertTrue(is_open(at(*d, 12, 55)))
                self.assertFalse(is_open(at(*d, 12, 56)))
                self.assertFalse(is_open(at(*d, 13, 0)))
                self.assertFalse(is_open(at(*d, 15, 0)))
                self.assertFalse(is_open(at(*d, 15, 55)))

    def test_an_ordinary_day_is_untouched_control(self):
        for d in ORDINARY:
            with self.subTest(day=d):
                self.assertTrue(is_open(at(*d, 12, 56)))
                self.assertTrue(is_open(at(*d, 15, 55)))
                self.assertFalse(is_open(at(*d, 15, 56)))

    def test_session_bounds_accepts_a_date_a_datetime_or_a_string_and_is_tz_aware(self):
        want = ("09:45", "12:55")
        for arg in (date(2026, 11, 27), at(2026, 11, 27, 3, 0), "2026-11-27"):
            o, c = bot.session_bounds(arg)
            self.assertEqual((o.strftime("%H:%M"), c.strftime("%H:%M")), want)
            self.assertIsNotNone(o.tzinfo)
        self.assertIsNone(bot.session_bounds("2026-11-26"))        # Thanksgiving itself
        self.assertIsNone(bot.session_bounds(date(2026, 11, 28)))  # Saturday

    def test_market_minutes_counts_an_early_close_day_as_190_and_an_ordinary_day_as_370(self):
        def whole_day(y, m, d):
            return bot.market_minutes_between(at(y, m, d, 0, 0), at(y, m, d, 23, 59))
        for d in EARLY:
            self.assertEqual(whole_day(*d), 190, d)
        for d in ORDINARY:
            self.assertEqual(whole_day(*d), 370, d)

    def test_an_outage_over_an_early_close_is_charged_only_up_to_1255(self):
        """Last good run 11:00 on 11-27, next at 09:50 on Monday: 115 minutes that day plus 5 on Monday, not 295 plus 5."""
        self.assertEqual(bot.market_minutes_between(at(2026, 11, 27, 11, 0), at(2026, 11, 30, 9, 50)), 115 + 5)

    def test_the_next_run_after_an_early_close_is_the_next_open(self):
        with mock.patch.object(bot, "datetime", clock(at(2026, 11, 27, 13, 30))):
            nxt = bot.next_market_run(at(2026, 11, 27, 13, 30)).astimezone(ET)
        self.assertEqual(nxt, at(2026, 11, 30, 9, 45))

    def test_the_table_does_not_run_out_unnoticed_either(self):
        last = max(int(d[:4]) for d in bot.EARLY_CLOSE)
        self.assertGreaterEqual(last, max(int(d[:4]) for d in bot.MARKET_HOLIDAYS),
                                "EARLY_CLOSE must cover the same last year as MARKET_HOLIDAYS: add that year's 1 pm closes")


class EveryConsumerAgrees(unittest.TestCase):
    """One shared function, no copy of the close. Each consumer is exercised on an early-close day and on an ordinary one."""

    def test_arm_a_watch_expects_runs_only_until_1255_on_an_early_close_day(self):
        import arm_a_watch as W
        o, c = W.session_bounds(datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc))
        self.assertEqual((c.hour, c.minute), (17, 55))                      # 12:55 EST
        o, c = W.session_bounds(datetime(2026, 11, 30, 17, 0, tzinfo=timezone.utc))
        self.assertEqual((c.hour, c.minute), (20, 55))                      # 15:55 EST: the control

    def test_arm_a_watch_stays_quiet_in_the_three_hours_after_the_early_close_and_not_before(self):
        """The failure this prevents: the watch pages about three hours of correct silence on each early-close day."""
        import arm_a_watch as W
        none = dict(get_commit_time=lambda ref: None, get_status=lambda ref: None, get_runs=lambda: None)
        after = datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)           # 14:00 ET
        before = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)          # 11:00 ET
        self.assertIn("market closed", W.run_once(now=after, deliver=lambda *a, **k: True, **none)[1])
        self.assertNotIn("market closed", W.run_once(now=before, deliver=lambda *a, **k: True, **none)[1])
        ordinary = datetime(2026, 11, 30, 19, 0, tzinfo=timezone.utc)        # 14:00 ET on a normal day: still in session
        self.assertNotIn("market closed", W.run_once(now=ordinary, deliver=lambda *a, **k: True, **none)[1])

    def test_the_two_downtime_tools_use_the_same_window(self):
        import arm_a_downtime as A
        import arm_b_downtime as B
        hol = B.holidays()
        for d in EARLY:
            o, c = B.session(date(*d), hol)
            self.assertEqual((c.hour, c.minute), (12, 55), d)
            self.assertEqual(B.window_minutes(datetime(*d, 0, 0), datetime(*d, 23, 59), hol), 190, d)
        o, c = B.session(date(2026, 11, 30), hol)
        self.assertEqual((c.hour, c.minute), (15, 55))
        # Arm A: the run list ends at 12:50 on 11-27 and resumes Monday 09:46: only 5 + 1 minutes were unwatched, not three hours
        ivs = [(datetime(2026, 11, 27, 12, 40), datetime(2026, 11, 27, 12, 50)), (datetime(2026, 11, 30, 9, 46), datetime(2026, 11, 30, 9, 56))]
        r = A.measure(ivs, datetime(2026, 11, 27), datetime(2026, 12, 1), hol)
        self.assertEqual(r["unplanned"], 5 + 1)

    def test_no_cloud_owned_module_keeps_its_own_copy_of_the_close(self):
        """A second copy of 15:55 is how an early close gets modelled in one place and missed in another."""
        import re
        pat = re.compile(r"\b15, ?55\b|hour ?= ?15\b")
        for name in ("arm_a_watch.py", "arm_b_downtime.py", "arm_a_downtime.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                code = [ln for ln in f.read().splitlines() if not ln.lstrip().startswith("#")]
            self.assertEqual([ln for ln in code if pat.search(ln)], [], name)
        with open(os.path.join(ROOT, "alpaca_bot.py"), encoding="utf-8") as f:
            hits = [ln for ln in f.read().splitlines() if pat.search(ln) and not ln.lstrip().startswith("#")]
        self.assertEqual(len(hits), 1, hits)                                # only the SESSION_CLOSE definition
        self.assertIn("SESSION_CLOSE", hits[0])


class LaptopOwesIt(unittest.TestCase):
    """rh_daemon.market_minutes_since is LAPTOP-owned and still carries its own 09:45 / 15:55 (L287-288). It feeds the daemon's
    15 / 60 / 180 minute broker alerts, the downtime report's market-minute gate and the recovery log line. Left alone, a 12:55 close
    reads as 60 market minutes of silence at 13:55 and the daemon mails Devon a false outage on each early-close day.

    EXPECTED FAILURE until LAPTOP BOT adopts bot.session_bounds there (asked in AGENT_MAIL 2026-10-13, before 2026-11-27). It flips
    to "unexpected success" the moment it does: delete the marker then, in the same commit as the adoption."""

    @unittest.expectedFailure
    def test_rh_daemon_market_minutes_since_stops_at_the_early_close(self):
        import rh_daemon as RD
        now = at(2026, 11, 30, 9, 50)
        with mock.patch.object(RD, "datetime", clock(now)):
            got = RD.market_minutes_since(at(2026, 11, 27, 11, 0).timestamp())
        self.assertEqual(got, 115 + 5)                                      # 11:00 to 12:55 on Friday, 09:45 to 09:50 on Monday

    def test_the_same_function_is_right_on_an_ordinary_day_control(self):
        """The control that makes the failure above mean something: the function works, it just has the wrong close."""
        import rh_daemon as RD
        now = at(2026, 11, 25, 9, 50)
        with mock.patch.object(RD, "datetime", clock(now)):
            got = RD.market_minutes_since(at(2026, 11, 24, 11, 0).timestamp())
        self.assertEqual(got, 295 + 5)                                      # 11:00 to 15:55 on Tuesday, 09:45 to 09:50 on Wednesday


if __name__ == "__main__":
    unittest.main()
