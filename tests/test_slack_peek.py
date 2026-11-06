"""slack_notify.peek_bot: the read-only view of what a BOT alert actually said.

Why it exists: read_channel() deliberately drops the bot's own posts, so an alert email body was
invisible to every session and nobody could confirm what a live alert rendered. This is the
narrow opposite view, and the tests pin the narrowness: bot posts only, matching lines only,
one allowed channel only. Each guard is shown to be able to say NO, not just YES.

Run:  python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import slack_notify as S  # noqa: E402

ALERT = ("*ORDER PLACED*\nBought 1 SPY\n"
         "REALIZED (tax year 2026)\n"
         "Arm A: gains $0.11 / losses -$13.12 / net -$13.01\n"
         "unrelated line about the weather")


def bot(ts, text, style="bot_id"):
    m = {"ts": ts, "text": text}
    if style == "bot_id":
        m["bot_id"] = "B123"
    else:
        m["subtype"] = "bot_message"
    return m


def human(ts, text):
    return {"ts": ts, "user": "U999", "text": text}


class Api:
    """Stands in for slack_notify._api and records what was asked."""
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        return {"ok": True, "messages": self.messages}


def configured():
    return mock.patch.multiple(S, BOT_TOKEN="xoxb-test", CHANNEL_ID=S.INGEST_CHANNEL)


class PeekBotTests(unittest.TestCase):
    def run_peek(self, messages, pattern="realized|Arm A"):
        api = Api(messages)
        with configured(), mock.patch.object(S, "_api", api):
            return S.peek_bot(pattern), api

    def test_returns_only_the_matching_lines_of_a_bot_post(self):
        res, _ = self.run_peek([bot("1780000000.0", ALERT)])
        self.assertEqual(len(res), 1)
        ts, first, hits = res[0]
        self.assertEqual(first, "*ORDER PLACED*")
        self.assertEqual(hits, ["REALIZED (tax year 2026)",
                                "Arm A: gains $0.11 / losses -$13.12 / net -$13.01"])
        self.assertNotIn("unrelated line about the weather", " ".join(hits))   # never the whole message

    def test_a_humans_message_with_the_same_text_is_never_returned(self):
        """The guard that stops this being a way to read what Devon typed."""
        res, _ = self.run_peek([human("1780000001.0", ALERT)])
        self.assertEqual(res, [])

    def test_both_bot_shapes_are_recognised(self):
        res, _ = self.run_peek([bot("1", ALERT, "bot_id"), bot("2", ALERT, "subtype")])
        self.assertEqual(len(res), 2)

    def test_the_filter_can_say_no(self):
        """Positive control: a pattern nothing matches yields an empty list, not everything."""
        res, _ = self.run_peek([bot("1", ALERT)], pattern="zzz-not-present")
        self.assertEqual(res, [])

    def test_matching_is_case_insensitive(self):
        res, _ = self.run_peek([bot("1", ALERT)], pattern="ARM a")
        self.assertEqual(len(res), 1)

    def test_a_bot_post_with_no_matching_line_is_omitted(self):
        res, _ = self.run_peek([bot("1", "Heartbeat ok\nnothing to see")])
        self.assertEqual(res, [])

    def test_mixed_channel_returns_only_the_bot_posts(self):
        res, _ = self.run_peek([human("3", ALERT), bot("2", ALERT), human("1", ALERT)])
        self.assertEqual([r[0] for r in res], ["2"])

    def test_matching_lines_per_message_are_capped(self):
        text = "\n".join("Arm A line %d" % i for i in range(40))
        res, _ = self.run_peek([bot("1", text)], pattern="Arm A")
        self.assertEqual(len(res[0][2]), 12)

    def test_it_asks_only_for_the_one_allowed_channel(self):
        _, api = self.run_peek([bot("1", ALERT)])
        self.assertEqual(api.calls[0][0], "conversations.history")
        self.assertEqual(api.calls[0][1]["channel"], S.INGEST_CHANNEL)

    def test_a_disallowed_channel_is_refused_before_any_api_call(self):
        api = Api([bot("1", ALERT)])
        with mock.patch.multiple(S, BOT_TOKEN="xoxb-test", CHANNEL_ID="C_SOMEONE_ELSE"), \
                mock.patch.object(S, "_api", api):
            self.assertIsNone(S.peek_bot("Arm A"))
        self.assertEqual(api.calls, [])

    def test_unconfigured_returns_none_and_calls_nothing(self):
        api = Api([bot("1", ALERT)])
        with mock.patch.multiple(S, BOT_TOKEN="", CHANNEL_ID=""), mock.patch.object(S, "_api", api):
            self.assertIsNone(S.peek_bot("Arm A"))
        self.assertEqual(api.calls, [])

    def test_an_api_failure_is_none_not_an_empty_list(self):
        """None means 'could not read'; [] means 'read fine, nothing matched'. They must differ."""
        with configured(), mock.patch.object(S, "_api", lambda *a, **k: None):
            self.assertIsNone(S.peek_bot("Arm A"))

    def test_a_bad_regex_is_none_not_a_crash(self):
        with configured(), mock.patch.object(S, "_api", Api([bot("1", ALERT)])):
            self.assertIsNone(S.peek_bot("("))

    def test_limit_is_clamped(self):
        _, api = self.run_peek([])
        with configured(), mock.patch.object(S, "_api", api):
            S.peek_bot("x", limit=10 ** 6)
            S.peek_bot("x", limit=0)
        self.assertEqual([c[1]["limit"] for c in api.calls[1:]], ["200", "1"])


class PeekCliTests(unittest.TestCase):
    def test_cli_exits_1_and_says_why_when_unconfigured(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID")}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "slack_notify.py"), "--peek", "Arm A"],
                           capture_output=True, text=True, cwd=ROOT, env=env)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unavailable", r.stdout)

    def test_usage_mentions_peek(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "slack_notify.py"), "--nonsense"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--peek", r.stdout)


if __name__ == "__main__":
    unittest.main()
