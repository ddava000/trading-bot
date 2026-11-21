"""Arm A's two fail-open guards must say when they are not working (2026-11-04, from the 2026-10-28 cold review).

1. The EARNINGS GUARD's published state (status.json "earnings_guard") came from the Yahoo crumb handshake alone. earnings_within swallowed every
   per-symbol failure (an exception, or Yahoo's 429 / error body whose `result` is null) and answered "no earnings", so a run in which every query
   failed still published "live". The answer is unchanged (fail-open, False); the state now becomes "degraded".
2. The NEWS TRIPWIRE (news_flags, every fast pass) returned [] on an error body or an exception without a trace, so it could be blind for a whole
   session unnoticed. The answer is unchanged ([]); a line now says so.

Neither changes what the bot does. A guard that fails open is a decision; a guard that fails open INVISIBLY is a fault nobody can see.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "x")
os.environ.setdefault("ALPACA_SECRET_KEY", "x")

import alpaca_bot as bot  # noqa: E402


class FakeReply:
    def __init__(self, payload=None, boom=None):
        self._payload, self._boom = payload, boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._payload


class FakeSession:
    crumb = "crumb"

    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises

    def get(self, url, timeout=None):
        if self.raises:
            raise self.raises
        return self.reply


def calendar(*days_ahead):
    now = time.time()
    return {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {"earningsDate": [{"raw": now + d * 86400} for d in days_ahead]}}}],
                             "error": None}}


class EarningsGuardState(unittest.TestCase):
    def setUp(self):
        bot._EARN_CACHE.clear()
        self.addCleanup(bot._EARN_CACHE.clear)
        old = bot.EARN_GUARD_STATE
        bot.EARN_GUARD_STATE = "live"
        self.addCleanup(setattr, bot, "EARN_GUARD_STATE", old)

    def ask(self, session, sym="AAA", days=2):
        out = io.StringIO()
        with mock.patch.object(bot, "yf_session", lambda: session), contextlib.redirect_stdout(out):
            return bot.earnings_within(sym, days), out.getvalue()

    def test_earnings_inside_the_window_is_true_and_the_guard_stays_live(self):
        hit, out = self.ask(FakeSession(FakeReply(calendar(1))))
        self.assertTrue(hit)
        self.assertEqual((bot.EARN_GUARD_STATE, out), ("live", ""))

    def test_a_good_answer_with_no_earnings_is_false_and_the_guard_stays_live(self):
        for payload in (calendar(), calendar(30)):                       # no dates at all, and a date far outside the window
            bot._EARN_CACHE.clear()
            hit, out = self.ask(FakeSession(FakeReply(payload)))
            self.assertFalse(hit)
            self.assertEqual((bot.EARN_GUARD_STATE, out), ("live", ""))

    def test_an_error_body_is_still_false_but_degrades_the_guard_and_says_so(self):
        body = {"quoteSummary": {"result": None, "error": {"code": "Too Many Requests", "description": "rate limited"}}}
        hit, out = self.ask(FakeSession(FakeReply(body)))
        self.assertFalse(hit)                                            # fail-open is unchanged
        self.assertEqual(bot.EARN_GUARD_STATE, "degraded")
        self.assertIn("earnings query for AAA failed", out)
        self.assertIn("NOT blocked", out)

    def test_a_reply_with_no_quote_summary_at_all_degrades_it_too(self):
        hit, _ = self.ask(FakeSession(FakeReply({"finance": {"error": {"code": "Unauthorized"}}})))
        self.assertFalse(hit)
        self.assertEqual(bot.EARN_GUARD_STATE, "degraded")

    def test_an_exception_or_a_non_json_body_degrades_it(self):
        for session in (FakeSession(raises=OSError("timeout")), FakeSession(FakeReply(boom=ValueError("not json")))):
            bot.EARN_GUARD_STATE = "live"
            bot._EARN_CACHE.clear()
            hit, out = self.ask(session)
            self.assertFalse(hit)
            self.assertEqual(bot.EARN_GUARD_STATE, "degraded")
            self.assertIn("failed", out)

    def test_one_failure_is_enough_and_a_later_good_answer_does_not_hide_it(self):
        self.ask(FakeSession(raises=OSError("timeout")), sym="AAA")
        hit, _ = self.ask(FakeSession(FakeReply(calendar(1))), sym="BBB")
        self.assertTrue(hit)                                              # the good answer still counts as an answer
        self.assertEqual(bot.EARN_GUARD_STATE, "degraded")                # and the run is still reported as degraded

    def test_a_failed_answer_is_cached_like_any_other_so_yahoo_is_not_hammered(self):
        session = FakeSession(raises=OSError("timeout"))
        self.ask(session)
        calls = []
        session.get = lambda *a, **k: calls.append(1) or FakeReply(calendar(1))
        self.assertFalse(self.ask(session)[0])                            # cached False: no second request
        self.assertEqual(calls, [])

    def test_the_published_status_carries_the_state(self):
        """The field status.json reads is this module global, so a degrade here is what the monitoring sees."""
        self.ask(FakeSession(FakeReply({"quoteSummary": {"result": None}})))
        self.assertEqual(bot.EARN_GUARD_STATE, "degraded")


class NewsTripwireVisibility(unittest.TestCase):
    def fetch(self, reply=None, raises=None):
        out = io.StringIO()

        def fake(path, *a, **k):
            if raises:
                raise raises
            return reply
        with mock.patch.object(bot, "alpaca_data_get", fake), contextlib.redirect_stdout(out):
            return bot.alpaca_news({"AAA"}, 16), out.getvalue()

    def test_an_error_body_returns_nothing_and_says_the_tripwire_is_blind(self):
        got, out = self.fetch({"message": "too many requests"})
        self.assertEqual(got, [])
        self.assertIn("too many requests", out)
        self.assertIn("tripwire is blind", out)

    def test_an_exception_or_a_non_dict_reply_says_so_too(self):
        got, out = self.fetch(raises=OSError("down"))
        self.assertEqual(got, [])
        self.assertIn("OSError", out)
        self.assertIn("tripwire is blind", out)
        got, out = self.fetch(["unexpected"])
        self.assertEqual(got, [])
        self.assertIn("list", out)

    def test_a_reply_without_a_news_field_names_that_instead_of_none(self):
        _, out = self.fetch({})
        self.assertIn("no news field", out)
        self.assertNotIn("None", out)

    def test_control_an_empty_news_list_is_a_quiet_normal_answer(self):
        got, out = self.fetch({"news": [], "next_page_token": None})
        self.assertEqual((got, out), ([], ""))

    def test_control_articles_come_back_unchanged(self):
        got, out = self.fetch({"news": [{"headline": "AAA recalls product", "summary": "x", "symbols": ["AAA"], "created_at": "2026-11-04T20:00:00Z"}]})
        self.assertEqual(got, [(["AAA"], "AAA recalls product x", "2026-11-04T20:00:00Z")])
        self.assertEqual(out, "")

    def test_the_tripwire_still_flags_a_danger_headline_and_still_returns_nothing_when_blind(self):
        art = {"news": [{"headline": "AAA announces SEC investigation", "summary": "", "symbols": ["AAA"], "created_at": "z"}]}
        self.assertIn("sec investigation", bot.DANGER_WORDS)
        with mock.patch.object(bot, "alpaca_data_get", lambda *a, **k: art):
            self.assertEqual(list(bot.news_flags({"AAA"}, 16)), ["AAA"])
        with mock.patch.object(bot, "alpaca_data_get", lambda *a, **k: {"message": "x"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bot.news_flags({"AAA"}, 16), {})


if __name__ == "__main__":
    unittest.main()
