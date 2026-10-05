"""yf_vix() must say when it is guessing.

2026-10-04 weekly audit: yf_vix() returns a calm 20.0 on ANY failure, and Yahoo blocks datacenter addresses,
so a real volatility spike during an outage would pass the VIX>35 halt AND the 25/20 size scaling
unnoticed, and status.json showed only the number. The fix is visibility only: a `vix_live` flag published
beside `vix`. These tests pin that the fallback VALUE and thresholds did not change (that is Devon's call)
and that the flag cannot say "live" for a guess.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def chart(closes):
    return {"chart": {"result": [{"indicators": {"quote": [{"close": closes}]}}]}}


class VixLivenessTests(unittest.TestCase):
    def read(self, get):
        out = io.StringIO()
        with mock.patch.object(bot.requests, "get", get), contextlib.redirect_stdout(out):
            v = bot.yf_vix()
        return v, out.getvalue()

    def test_a_real_quote_is_live_and_returned_as_is(self):
        v, out = self.read(lambda *a, **k: FakeResp(chart([14.0, 15.2, 16.5])))
        self.assertEqual(v, 16.5)
        self.assertIs(bot.VIX_STATE["live"], True)
        self.assertNotIn("UNAVAILABLE", out)

    def test_the_latest_non_null_close_is_used(self):
        v, _ = self.read(lambda *a, **k: FakeResp(chart([14.0, 17.25, None])))
        self.assertEqual(v, 17.25)

    def test_a_network_failure_returns_the_unchanged_fallback_and_says_it_is_not_live(self):
        def boom(*a, **k):
            raise ConnectionError("blocked")
        v, out = self.read(boom)
        self.assertEqual(v, 20.0)                                  # the VALUE is unchanged: visibility only
        self.assertIs(bot.VIX_STATE["live"], False)
        self.assertIn("VIX UNAVAILABLE", out)
        self.assertIn("NOT enforcing", out)

    def test_a_malformed_or_empty_payload_is_also_not_live(self):
        for payload in ({}, {"chart": {"result": None}}, chart([None, None]), chart([])):
            with self.subTest(payload=payload):
                v, _ = self.read(lambda *a, payload=payload, **k: FakeResp(payload))
                self.assertEqual(v, 20.0)
                self.assertIs(bot.VIX_STATE["live"], False)

    def test_a_good_read_after_a_bad_one_flips_the_flag_back(self):
        self.read(lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        self.assertIs(bot.VIX_STATE["live"], False)
        self.read(lambda *a, **k: FakeResp(chart([18.0])))
        self.assertIs(bot.VIX_STATE["live"], True)

    def test_a_calm_looking_fallback_cannot_be_mistaken_for_a_real_twenty(self):
        """The guess equals a plausible real reading, so only the flag tells them apart."""
        v_real, _ = self.read(lambda *a, **k: FakeResp(chart([20.0])))
        live_real = bot.VIX_STATE["live"]
        v_guess, _ = self.read(lambda *a, **k: (_ for _ in ()).throw(ConnectionError("x")))
        self.assertEqual(v_real, v_guess)
        self.assertNotEqual(live_real, bot.VIX_STATE["live"])


class PublishedAndThresholdsUnchangedTests(unittest.TestCase):
    def source(self):
        with open(os.path.join(ROOT, "alpaca_bot.py"), encoding="utf-8") as f:
            return f.read()

    def test_status_json_publishes_the_flag_beside_the_value(self):
        self.assertIn('"vix": round(vix, 1), "vix_live": VIX_STATE["live"]', self.source())

    def test_the_halt_and_scaling_thresholds_are_untouched(self):
        src = self.source()
        self.assertIn("if vix > 35:", src)
        self.assertIn("vix_scale = 0.50 if vix > 25 else (0.75 if vix > 20 else 1.00)", src)


if __name__ == "__main__":
    unittest.main()
