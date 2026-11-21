"""review.py: the weekly email must carry the realized gain/loss section, and a failure of that
section must never take the rest of the review down with it.

review.py runs only on Fridays, so its realized section went unproven for days after the daily
email path was verified live. This pins it without a broker: every Alpaca call is faked, and the
expected text is built from the SAME shared reader the production path uses, so the test cannot
drift into asserting a number of its own.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")      # alpaca_bot reads these at import
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import alpaca_bot as bot  # noqa: E402
import realized as R      # noqa: E402
import review             # noqa: E402


def fake_alpaca_get(path, *a, **k):
    if path.startswith("/v2/account/portfolio/history"):
        return {"equity": [240.0, 236.0, 235.79]}
    if path.startswith("/v2/account"):
        return {"equity": "235.79", "cash": "11.20"}
    if path.startswith("/v2/orders"):
        return [{"filled_at": "2026-09-29T14:00:00Z", "side": "buy"},
                {"filled_at": "2026-09-30T14:00:00Z", "side": "sell"},
                {"filled_at": None, "side": "buy"}]
    if path.startswith("/v2/positions"):
        return [{"symbol": "SPY", "market_value": "50.00",
                 "unrealized_pl": "1.00", "unrealized_plpc": "0.02"}]
    raise AssertionError("unexpected Alpaca call: " + path)


def a_block(**over):
    with open(os.path.join(ROOT, "realized_a.json"), encoding="utf-8") as f:
        block = dict(json.load(f)["summary"])
    block.update(over)
    return block


def run_review(realized_for_run):
    """Runs review.main() against fakes; returns (printed report, [(subject, body)] emailed)."""
    sent = []
    out = io.StringIO()
    with mock.patch.object(bot, "alpaca_get", fake_alpaca_get), \
            mock.patch.object(bot, "yf_ohlcv", lambda sym: ([100.0 + i for i in range(10)], None)), \
            mock.patch.object(bot, "send_email", lambda s, b: sent.append((s, b)) or True), \
            mock.patch.object(bot, "realized_for_run", realized_for_run), \
            contextlib.redirect_stdout(out):
        review.main()
    return out.getvalue(), sent


class ReviewRealizedSectionTests(unittest.TestCase):
    def test_every_shared_reader_line_is_in_the_emailed_report(self):
        block = a_block()
        _, sent = run_review(lambda: block)
        self.assertEqual(len(sent), 1)
        body = sent[0][1]
        expected = R.repo_report_lines(a_block=block)
        self.assertGreaterEqual(len(expected), 4)          # Arm A, Arm B, BOTH ARMS, disclaimer at least
        for line in expected:
            self.assertIn(line, body)

    def test_section_is_labelled_as_sold_only_unlike_the_equity_change(self):
        """The whole reason for the section: equity includes unrealized paper P&L, which is not taxable."""
        _, sent = run_review(lambda: a_block())
        self.assertIn("REALIZED GAIN/LOSS (sold positions only, unlike the equity change above):", sent[0][1])
        self.assertIn("Not a tax document", sent[0][1])

    def test_report_and_email_are_the_same_text(self):
        printed, sent = run_review(lambda: a_block())
        self.assertEqual(printed.strip(), sent[0][1].strip())

    def test_the_rest_of_the_review_is_still_there(self):
        _, sent = run_review(lambda: a_block())
        body = sent[0][1]
        for needle in ("Equity: $235.79", "This week:", "Trades this week: 2 filled (1 buys, 1 sells)",
                       "Open positions (unrealized):", "SPY"):
            self.assertIn(needle, body)

    def test_a_non_ok_state_is_never_shown_as_a_clean_number(self):
        """The state is part of the number. A stale ledger must say so in the weekly email too."""
        block = a_block(state="stale", reason="activity history unavailable")
        _, sent = run_review(lambda: block)
        arm_a = [ln for ln in sent[0][1].splitlines() if "Arm A (Alpaca)" in ln]
        self.assertEqual(len(arm_a), 1)
        self.assertIn("stale", arm_a[0].lower())
        self.assertEqual(arm_a[0].strip(), R.repo_report_lines(a_block=block)[0])

    def test_the_stale_check_can_say_ok(self):
        """Positive control: an ok block must NOT be reported as stale, or the test above proves nothing."""
        arm_a = [ln for ln in run_review(lambda: a_block(state="ok"))[1][0][1].splitlines()
                 if "Arm A (Alpaca)" in ln]
        self.assertNotIn("stale", arm_a[0].lower())

    def test_a_realized_failure_does_not_take_the_review_down(self):
        def boom():
            raise RuntimeError("alpaca activities exploded")
        _, sent = run_review(boom)
        self.assertEqual(len(sent), 1, "the weekly email must still go out")
        body = sent[0][1]
        self.assertIn("Realized gain/loss totals UNAVAILABLE", body)
        self.assertIn("alpaca activities exploded", body)     # says WHY, never silently zero
        self.assertIn("Equity: $235.79", body)                # and the rest is intact


class PerfComparisonTests(unittest.TestCase):
    """The equity-vs-SPY block must fail CLOSED: a failed/empty portfolio-history fetch must never
    fabricate a 0.00% week that then prints a flattering "BEAT SPY". Regression for the fail-open
    LAPTOP BOT found 2026-11-09 (review.py: `wk_start = eqs[0] if eqs else equity`)."""

    def test_empty_equity_history_suppresses_the_comparison(self):
        lines, wk_pct = review._perf_lines(equity=235.79, eqs=[], spy_pct=-2.70)
        self.assertIsNone(wk_pct)
        text = "\n".join(lines)
        self.assertIn("equity history unavailable", text)
        # The whole defect: a missing leg must NOT turn into a BEAT.
        self.assertNotIn("BEAT", text)
        self.assertNotIn("TRAILED", text)

    def test_present_history_still_produces_a_result(self):
        """Positive control: with real points the comparison line must appear, or the test above
        proves nothing (it would pass on code that never prints a result at all)."""
        # SPY +4.81% (fakes), Arm A 240 -> 235.79 = -1.75%, gap well outside the tie band.
        lines, wk_pct = review._perf_lines(equity=235.79, eqs=[240.0, 236.0, 235.79], spy_pct=4.81)
        self.assertAlmostEqual(wk_pct, (235.79 - 240.0) / 240.0 * 100, places=4)
        self.assertIn("TRAILED", "\n".join(lines))

    def test_close_to_close_uses_last_history_point_not_live_equity(self):
        """The Arm A leg runs eqs[0]->eqs[-1], matching the SPY close-to-close leg; a live intraday
        `equity` argument that differs from eqs[-1] must not change the number."""
        lines, wk_pct = review._perf_lines(equity=999.0, eqs=[100.0, 105.0, 110.0], spy_pct=0.0)
        self.assertAlmostEqual(wk_pct, 10.0, places=4)  # (110-100)/100, not touched by equity=999

    def test_margin_inside_the_spread_is_not_a_decisive_result(self):
        """A gap under the measurement spread must read as a tie, not a BEAT/TRAILED headline
        (the 2026-11-06 audit quoted a 0.19-pt BEAT that was inside a 0.43-pt spread)."""
        # Arm A 100->99.80 = -0.20%; SPY -0.10%; gap -0.10 pt, inside the 0.50-pt band.
        lines, _ = review._perf_lines(equity=99.80, eqs=[100.0, 99.80], spy_pct=-0.10)
        text = "\n".join(lines)
        self.assertIn("roughly matched", text)
        self.assertNotIn("BEAT SPY by", text)
        self.assertNotIn("TRAILED SPY by", text)

    def test_tie_band_still_lets_a_real_gap_through(self):
        """Positive control for the band: a gap larger than the band must still print BEAT/TRAILED."""
        lines, _ = review._perf_lines(equity=103.0, eqs=[100.0, 103.0], spy_pct=0.0)  # +3.00 pt gap
        self.assertIn("BEAT", "\n".join(lines))

    def test_review_main_does_not_print_a_beat_when_history_is_empty(self):
        """End to end through main(): patch portfolio-history to return no points; the emailed body
        must say the comparison was suppressed and must NOT contain a BEAT line."""
        def no_history(path, *a, **k):
            if path.startswith("/v2/account/portfolio/history"):
                return {"equity": []}
            return fake_alpaca_get(path, *a, **k)
        sent = []
        out = io.StringIO()
        with mock.patch.object(bot, "alpaca_get", no_history), \
                mock.patch.object(bot, "yf_ohlcv", lambda sym: ([100.0 + i for i in range(10)], None)), \
                mock.patch.object(bot, "send_email", lambda s, b: sent.append((s, b)) or True), \
                mock.patch.object(bot, "realized_for_run", lambda: a_block()), \
                contextlib.redirect_stdout(out):
            review.main()
        body = sent[0][1]
        self.assertIn("equity history unavailable", body)
        self.assertNotIn("BEAT", body)
        self.assertIn("SPY this week:", body)  # SPY leg still reported, just no comparison


if __name__ == "__main__":
    unittest.main()
