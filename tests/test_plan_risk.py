"""The research plan must never be able to raise position size by FAILING.

2026-09-27: the Anthropic credit balance ran out and stopped brief.py. The first fix made
alpaca_bot.load_plan hold a stale plan's risk instead of falling back to full size. This file pins
that, and the second hole found on 2026-09-30: when the brief's OWN model call fails it used to write
a plan dated TODAY with risk_scale 1.0 and an empty avoid list. load_plan only guards against a
missing or old plan, so a today-dated fallback was trusted and the bot traded at full size, with the
previous plan's avoid list thrown away. Across 79 recorded plan days the brief never once allowed
full size (min 0.30, max 0.65), so 1.0 is a size the bot has never actually run at.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402

# brief.py imports the anthropic SDK at module level, but only get_plan() uses it and the tests
# replace that. CI installs requirements-alpaca.txt only, so stub the package when it is absent.
try:
    import anthropic  # noqa: F401
except ImportError:
    import types
    sys.modules["anthropic"] = types.ModuleType("anthropic")

import brief          # noqa: E402

ET = bot.ET_TZ
TODAY = datetime(2026, 9, 30, 10, 0, tzinfo=ET)


def good_plan(**over):
    d = {"regime": "cautious", "risk_scale": 0.4, "avoid_symbols": ["TSLA"], "favor_symbols": ["QQQ"],
         "notes": "n", "journal_entry": "j", "date": "2026-09-30"}
    d.update(over)
    return d


class Workdir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)

    def put(self, name, obj=None, raw=None):
        with open(name, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(obj))

    def read_plan(self):
        with open("daily_plan.json", encoding="utf-8") as f:
            return json.load(f)


class LoadPlanTests(Workdir):
    def load(self, now=TODAY):
        with contextlib.redirect_stdout(io.StringIO()):
            return bot.load_plan(now)

    def test_todays_plan_is_used_as_written(self):
        self.put("daily_plan.json", good_plan())
        p = self.load()
        self.assertEqual((p["risk"], p["stale"], p["avoid"], p["favor"]), (0.4, False, {"TSLA"}, ["QQQ"]))

    def test_a_stale_plan_holds_its_own_lower_risk_and_avoid_list_but_drops_favourites(self):
        self.put("daily_plan.json", good_plan(date="2026-09-29"))
        p = self.load()
        self.assertTrue(p["stale"])
        self.assertEqual((p["risk"], p["avoid"], p["favor"]), (0.4, {"TSLA"}, []))
        self.assertTrue(p["regime"].startswith("stale:"))

    def test_a_stale_full_size_plan_is_capped_at_the_standing_limit(self):
        self.put("daily_plan.json", good_plan(date="2026-09-29", risk_scale=1.0))
        self.assertEqual(self.load()["risk"], bot.NO_PLAN_RISK)

    def test_missing_and_corrupt_plans_fall_back_to_the_standing_limit_never_full_size(self):
        self.assertEqual((self.load()["risk"], self.load()["stale"]), (bot.NO_PLAN_RISK, True))
        self.put("daily_plan.json", raw="{not json")
        self.assertEqual(self.load()["risk"], bot.NO_PLAN_RISK)
        self.put("daily_plan.json", good_plan(risk_scale="high"))
        self.assertEqual(self.load()["risk"], bot.NO_PLAN_RISK)

    def test_a_todays_plan_with_no_risk_scale_is_not_a_licence_for_full_size(self):
        p = good_plan()
        del p["risk_scale"]
        self.put("daily_plan.json", p)
        self.assertEqual(self.load()["risk"], bot.NO_PLAN_RISK)

    def test_out_of_range_risk_is_clamped_into_zero_to_one(self):
        self.put("daily_plan.json", good_plan(risk_scale=7))
        self.assertEqual(self.load()["risk"], 1.0)
        self.put("daily_plan.json", good_plan(risk_scale=-3))
        self.assertEqual(self.load()["risk"], 0.0)

    def test_the_stale_guard_can_say_yes_to_a_fresh_full_size_plan(self):
        """Positive control: today's explicit 1.0 is respected, so the cap above is about
        staleness and not a blanket ceiling."""
        self.put("daily_plan.json", good_plan(risk_scale=1.0))
        self.assertEqual(self.load()["risk"], 1.0)


class BriefFallbackTests(Workdir):
    def run_brief(self, plan_or_exc, prior=None, prior_raw=None):
        if prior is not None:
            self.put("daily_plan.json", prior)
        if prior_raw is not None:
            self.put("daily_plan.json", raw=prior_raw)
        sent = []

        def fake_get_plan(_ctx):
            if isinstance(plan_or_exc, Exception):
                raise plan_or_exc
            return plan_or_exc, True

        with mock.patch.object(brief, "get_plan", fake_get_plan), \
                mock.patch.object(brief, "gather_context", lambda et, mode: "ctx"), \
                mock.patch.object(bot, "send_email", lambda s, b: sent.append((s, b)) or True), \
                contextlib.redirect_stdout(io.StringIO()):
            brief.main()
        return self.read_plan(), sent

    def test_a_failed_call_with_no_prior_plan_never_writes_full_size(self):
        plan, _ = self.run_brief(RuntimeError("credit balance is too low"))
        self.assertEqual(plan["risk_scale"], bot.NO_PLAN_RISK)
        self.assertTrue(plan["fallback"])
        self.assertEqual(plan["avoid_symbols"], [])

    def test_a_more_defensive_prior_plan_is_kept_with_its_avoid_list(self):
        plan, _ = self.run_brief(RuntimeError("x"), prior=good_plan(risk_scale=0.3, avoid_symbols=["IWM", "TSLA"]))
        self.assertEqual((plan["risk_scale"], plan["avoid_symbols"]), (0.3, ["IWM", "TSLA"]))

    def test_a_less_defensive_prior_plan_is_capped(self):
        for prior_risk in (0.65, 1.0):
            with self.subTest(prior=prior_risk):
                plan, _ = self.run_brief(RuntimeError("x"), prior=good_plan(risk_scale=prior_risk))
                self.assertEqual(plan["risk_scale"], bot.NO_PLAN_RISK)

    def test_a_malformed_or_corrupt_prior_plan_still_cannot_produce_full_size(self):
        p = good_plan(avoid_symbols="not a list")
        del p["risk_scale"]
        plan, _ = self.run_brief(RuntimeError("x"), prior=p)
        self.assertEqual(plan["risk_scale"], bot.NO_PLAN_RISK)
        plan, _ = self.run_brief(RuntimeError("x"), prior_raw="{corrupt")
        self.assertEqual((plan["risk_scale"], plan["avoid_symbols"]), (bot.NO_PLAN_RISK, []))

    def test_the_whole_chain_a_failed_brief_then_the_bots_load_plan(self):
        """The real hazard end to end: what the engine will size with after the brief failed today."""
        plan, _ = self.run_brief(RuntimeError("credit balance is too low"), prior=good_plan(risk_scale=1.0))
        when = datetime.strptime(plan["date"], "%Y-%m-%d").replace(hour=10, tzinfo=ET)
        with contextlib.redirect_stdout(io.StringIO()):
            loaded = bot.load_plan(when)
        self.assertFalse(loaded["stale"])                  # it IS dated today, which is why the guard has to be here
        self.assertLessEqual(loaded["risk"], bot.NO_PLAN_RISK)

    def test_a_failed_brief_is_visible_in_the_subject_and_the_journal(self):
        _, sent = self.run_brief(RuntimeError("x"))
        self.assertIn("RESEARCH FAILED", sent[0][0])
        with open("journal.md", encoding="utf-8") as f:
            self.assertIn("FALLBACK", f.read())

    def test_a_successful_brief_is_unchanged(self):
        plan, sent = self.run_brief(good_plan(risk_scale=0.45, avoid_symbols=["IWM"]))
        self.assertEqual((plan["risk_scale"], plan["avoid_symbols"]), (0.45, ["IWM"]))
        self.assertNotIn("FAILED", sent[0][0])
        self.assertNotIn("fallback", plan)

    def test_a_model_that_returns_a_nonsense_risk_gets_the_standing_limit_not_full_size(self):
        plan, _ = self.run_brief(good_plan(risk_scale="high"))
        self.assertEqual(plan["risk_scale"], bot.NO_PLAN_RISK)

    def test_the_fallback_builder_alone(self):
        p = brief.fallback_plan("boom", {"risk_scale": 0.2, "avoid_symbols": ["A"]})
        self.assertEqual((p["risk_scale"], p["avoid_symbols"], p["favor_symbols"]), (0.2, ["A"], []))
        self.assertIn("boom", p["notes"])
        self.assertEqual(brief.fallback_plan("x", None)["risk_scale"], bot.NO_PLAN_RISK)


if __name__ == "__main__":
    unittest.main()
