"""slack_notify.scan_channel: the diagnostic for "I wrote in Slack and nothing arrived".

pull() prints "read OK, no new channel messages" for a channel with a human thread reply in it, because
conversations.history returns top-level posts only. scan_channel is the view that shows what is really
there, so a human who says "I typed it" can be believed or answered with evidence. The tests pin what it
must show (a human thread reply, an attachment-only post) and what it must NOT leak (a bot post's text),
and that it reads the one allowed channel only.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import slack_notify as S  # noqa: E402

PARENT_TS = "1790000000.000100"
REPLY_TS = "1790000100.000200"


class Api:
    def __init__(self, history, replies=None, replies_ok=True):
        self.history, self.replies, self.replies_ok, self.calls = history, replies or {}, replies_ok, []

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "conversations.history":
            return {"ok": True, "messages": self.history}
        if method == "conversations.replies":
            return {"ok": True, "messages": self.replies[params["ts"]]} if self.replies_ok else None
        raise AssertionError("unexpected API method %s" % method)


def configured():
    return mock.patch.multiple(S, BOT_TOKEN="xoxb-test", CHANNEL_ID=S.INGEST_CHANNEL)


def bot(ts, text, **kw):
    return dict({"ts": ts, "bot_id": "B1", "text": text}, **kw)


def human(ts, text, **kw):
    return dict({"ts": ts, "user": "U999", "text": text}, **kw)


class ScanTests(unittest.TestCase):
    def scan(self, history, replies=None, **kw):
        api = Api(history, replies, **kw)
        with configured(), mock.patch.object(S, "_api", api):
            return S.scan_channel(), api

    def test_a_human_thread_reply_is_listed_and_pull_would_not_see_it(self):
        history = [bot(PARENT_TS, "ARM A WATCH: late", reply_count=1, thread_ts=PARENT_TS), bot("1790000200.000300", "digest")]
        replies = {PARENT_TS: [bot(PARENT_TS, "ARM A WATCH: late"), human(REPLY_TS, "test from devon", thread_ts=PARENT_TS)]}
        rows, _ = self.scan(history, replies)
        reply = [r for r in rows if r["ts"] == REPLY_TS]
        self.assertEqual(len(reply), 1)
        self.assertEqual(len([r for r in rows if r["ts"] == PARENT_TS]), 1, "the thread parent must be listed once, not again with its replies")
        self.assertEqual(reply[0]["who"], "HUMAN")
        self.assertEqual(reply[0]["where"], "thread " + PARENT_TS)
        self.assertEqual(reply[0]["peek"], "test from devon")
        # control: the production reader, fed the same channel, files nothing, which is the gap being diagnosed
        with configured(), mock.patch.object(S, "_api", Api(history, replies)):
            self.assertEqual(S.read_channel(), [])

    def test_control_a_top_level_human_post_is_what_pull_files(self):
        history = [human("1790000300.000400", "hello bots")]
        rows, _ = self.scan(history)
        self.assertEqual([(r["who"], r["where"]) for r in rows], [("HUMAN", "channel")])
        with configured(), mock.patch.object(S, "_api", Api(history)):
            self.assertEqual([m["text"] for m in S.read_channel()], ["hello bots"])

    def test_a_bots_text_is_never_returned_only_its_size(self):
        rows, _ = self.scan([bot("1790000400.000500", "REALIZED net -$13.01 secret-looking text")])
        self.assertEqual(rows[0]["who"], "BOT")
        self.assertEqual(rows[0]["peek"], "")
        self.assertGreater(rows[0]["chars"], 0)

    def test_human_text_is_clipped(self):
        rows, _ = self.scan([human("1790000500.000600", "x" * 500)])
        self.assertEqual(len(rows[0]["peek"]), 60)
        self.assertEqual(rows[0]["chars"], 500)

    def test_an_attachment_only_post_shows_files_and_no_text(self):
        rows, _ = self.scan([human("1790000600.000700", "", files=[{"id": "F1"}], subtype="file_share")])
        self.assertEqual((rows[0]["files"], rows[0]["chars"], rows[0]["subtype"]), (1, 0, "file_share"))

    def test_an_unreadable_thread_is_reported_not_skipped(self):
        history = [bot(PARENT_TS, "alert", reply_count=2)]
        rows, _ = self.scan(history, replies_ok=False)
        self.assertTrue(any(r["subtype"] == "replies unreadable" for r in rows))

    def test_rows_come_back_oldest_first(self):
        rows, _ = self.scan([human("1790000900.000900", "b"), human("1790000800.000800", "a")])
        self.assertEqual([r["peek"] for r in rows], ["a", "b"])

    def test_a_refused_channel_reads_nothing(self):
        api = Api([human("1790000300.000400", "x")])
        with mock.patch.multiple(S, BOT_TOKEN="xoxb-test", CHANNEL_ID="C_SOMEONE_ELSE"), mock.patch.object(S, "_api", api):
            self.assertIsNone(S.scan_channel())
        self.assertEqual(api.calls, [])

    def test_an_unset_token_reads_nothing(self):
        api = Api([])
        with mock.patch.multiple(S, BOT_TOKEN="", CHANNEL_ID=""), mock.patch.object(S, "_api", api):
            self.assertIsNone(S.scan_channel())
        self.assertEqual(api.calls, [])

    def test_the_thread_fetch_is_capped(self):
        history = [bot("17900%05d.000100" % i, "a", reply_count=1) for i in range(20)]
        replies = {m["ts"]: [m] for m in history}
        _, api = self.scan(history, replies)
        self.assertEqual(sum(1 for c in api.calls if c[0] == "conversations.replies"), 8)


class PeekDepth(unittest.TestCase):
    """mode=peek looked through only the newest 20 messages, so a bot alert from two days ago could not be read back."""

    def test_the_dispatch_form_has_a_limit_and_passes_it_to_the_step(self):
        with open(os.path.join(ROOT, ".github", "workflows", "slack-test.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("      limit:", text)
        self.assertIn("PEEK_LIMIT:", text)

    def test_the_cli_reads_the_depth_from_the_environment_and_says_how_deep_it_looked(self):
        src = open(os.path.join(ROOT, "slack_notify.py"), encoding="utf-8").read()
        self.assertIn('os.environ.get("PEEK_LIMIT")', src)
        self.assertIn("peek_bot(pat, limit=depth)", src)
        self.assertIn("looking through the newest %d", src)

    def test_peek_bot_passes_the_depth_to_slack(self):
        api = Api([bot("1790000000.000100", "ARM A alert")])
        with configured(), mock.patch.object(S, "_api", api):
            S.peek_bot("ARM A", limit=150)
        self.assertEqual(api.calls[0][1]["limit"], "150")
        with configured(), mock.patch.object(S, "_api", api):
            S.peek_bot("ARM A", limit=9999)
        self.assertEqual(api.calls[1][1]["limit"], "200")                  # Slack's maximum


class WorkflowWiring(unittest.TestCase):
    def test_the_dispatch_offers_scan_and_runs_the_scan_command(self):
        text = open(os.path.join(ROOT, ".github", "workflows", "slack-test.yml"), encoding="utf-8").read()
        self.assertIn("scan]", text)
        self.assertIn('python slack_notify.py --scan "${SAY_TEXT:-50}"', text)


if __name__ == "__main__":
    unittest.main()
