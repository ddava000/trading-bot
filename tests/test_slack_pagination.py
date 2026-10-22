"""slack_notify.read_channel / pull against a Slack that behaves the way the real one does.

THE BUG THESE TESTS EXIST FOR (found 2026-10-22). With `oldest` set, Slack's conversations.history returns the
OLDEST `limit` messages after it, not the newest. pull() asked for 25 after the last relay (2026-08-25); the
channel gets a few dozen bot posts a day, so within a week those 25 were all bot posts and every message
Devon typed afterwards sat beyond the page. Eight weeks of "[slack: read OK, no new channel messages]". The
earlier tests could not see it because their fake API ignored `oldest` and `limit` and returned everything.
FakeSlack below models both, plus cursor paging, and has a control showing it CAN reproduce the old failure.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import slack_notify as S  # noqa: E402

CURSOR = 1000.0


class FakeSlack:
    """Stands in for slack_notify._api. Messages are kept oldest-first; semantics measured live."""

    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda m: float(m["ts"]))
        self.calls = []
        self.fail_on_page = None

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        assert method == "conversations.history", method
        limit = int(params["limit"])
        if "oldest" in params:
            eligible = [m for m in self.messages if float(m["ts"]) > float(params["oldest"])]
            start = int(params.get("cursor") or 0)
            if self.fail_on_page is not None and start // max(1, limit) == self.fail_on_page:
                return None
            page = eligible[start:start + limit]
            more = start + limit < len(eligible)
            out = {"ok": True, "messages": list(reversed(page)), "has_more": more}
            if more:
                out["response_metadata"] = {"next_cursor": str(start + limit)}
            return out
        page = list(reversed(self.messages))[:limit]                       # no `oldest`: newest first
        return {"ok": True, "messages": page, "has_more": len(self.messages) > limit}


def bot(n):
    return {"ts": "%.6f" % (CURSOR + n), "bot_id": "B1", "subtype": "bot_message", "text": "alert %d" % n}


def human(n, text="hello"):
    return {"ts": "%.6f" % (CURSOR + n), "user": "U9", "text": text}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.mail = os.path.join(self.tmp.name, "AGENT_MAIL.md")
        self.archive = os.path.join(self.tmp.name, "AGENT_MAIL_ARCHIVE.md")
        with open(self.mail, "w", encoding="utf-8") as f:
            f.write("# mailbox\n\n## [2026-08-25 14:55 ET] slack -> all  [relayed from the Slack channel, slack-ts:%.6f]\nold\n" % CURSOR)
        with open(self.archive, "w", encoding="utf-8") as f:
            f.write("")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.multiple(S, MAILBOX=self.mail, _ARCHIVE=self.archive, BOT_TOKEN="xoxb-test",
                                                CHANNEL_ID=S.INGEST_CHANNEL))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stdout = None

    def use(self, messages):
        self.api = FakeSlack(messages)
        p = mock.patch.object(S, "_api", self.api)
        p.start()
        self.addCleanup(p.stop)
        return self.api


class TheFakeCanReproduceTheBug(Base):
    def test_control_one_page_of_25_after_the_cursor_is_all_bot_posts_while_a_human_message_exists(self):
        api = self.use([bot(n) for n in range(1, 301)] + [human(301, "Test")])
        first_page = api(  # the call the OLD read_channel made, once
            "conversations.history", {"channel": S.INGEST_CHANNEL, "limit": "25", "oldest": str(CURSOR)})["messages"]
        self.assertTrue(all(m.get("bot_id") for m in first_page), "the fake must reproduce the failure it guards against")
        self.assertTrue(any(m.get("text") == "Test" for m in api.messages))


class ReadChannelPaging(Base):
    def test_a_human_message_beyond_the_first_page_is_found(self):
        self.use([bot(n) for n in range(1, 301)] + [human(301, "Test")])
        got = S.read_channel(oldest=CURSOR)
        self.assertEqual([m["text"] for m in got], ["Test"])

    def test_every_page_is_followed_in_order_and_humans_come_back_oldest_first(self):
        self.use([human(5, "first")] + [bot(n) for n in range(6, 450)] + [human(450, "second")] + [bot(451)] + [human(452, "third")])
        self.assertEqual([m["text"] for m in S.read_channel(oldest=CURSOR)], ["first", "second", "third"])

    def test_a_message_on_the_page_boundary_is_not_dropped(self):
        for size in (S.PAGE_SIZE - 1, S.PAGE_SIZE, S.PAGE_SIZE + 1):
            with self.subTest(bots_before=size):
                self.use([bot(n) for n in range(1, size)] + [human(size, "edge")])
                self.assertEqual([m["text"] for m in S.read_channel(oldest=CURSOR)], ["edge"])

    def test_a_channel_with_only_bot_posts_since_the_cursor_is_an_empty_list(self):
        self.use([bot(n) for n in range(1, 450)])
        self.assertEqual(S.read_channel(oldest=CURSOR), [])

    def test_nothing_after_the_cursor_is_an_empty_list(self):
        self.use([])
        self.assertEqual(S.read_channel(oldest=CURSOR), [])

    def test_a_failure_on_a_later_page_is_none_not_a_partial_calm(self):
        api = self.use([bot(n) for n in range(1, 500)] + [human(500)])
        api.fail_on_page = 1
        self.assertIsNone(S.read_channel(oldest=CURSOR))

    def test_more_messages_than_the_cap_is_incomplete_and_none(self):
        with mock.patch.object(S, "PAGE_SIZE", 10), mock.patch.object(S, "MAX_PAGES", 3):
            self.use([bot(n) for n in range(1, 40)] + [human(40)])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertIsNone(S.read_channel(oldest=CURSOR))
        self.assertIn("INCOMPLETE", buf.getvalue())

    def test_exactly_the_cap_is_complete(self):
        with mock.patch.object(S, "PAGE_SIZE", 10), mock.patch.object(S, "MAX_PAGES", 3):
            self.use([bot(n) for n in range(1, 30)] + [human(30, "last")])
            self.assertEqual([m["text"] for m in S.read_channel(oldest=CURSOR)], ["last"])

    def test_without_a_cursor_it_is_one_call_for_the_newest(self):
        api = self.use([bot(n) for n in range(1, 100)] + [human(100, "newest")])
        got = S.read_channel(limit=25)
        self.assertEqual([m["text"] for m in got], ["newest"])
        self.assertEqual(len(api.calls), 1)
        self.assertNotIn("oldest", api.calls[0][1])


class TheQuietLineCarriesItsEvidence(Base):
    def test_a_quiet_read_says_how_many_messages_it_looked_at(self):
        self.use([bot(n) for n in range(1, 450)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(S.pull(25), [])
        self.assertIn("looked at 449 message(s)", buf.getvalue())

    def test_the_count_is_of_what_slack_returned_not_of_what_was_kept(self):
        self.use([bot(n) for n in range(1, 11)] + [human(11, "x")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            S.read_channel(oldest=CURSOR)
        self.assertEqual(S._READ_STATS["looked_at"], 11)


class PullEndToEnd(Base):
    def test_a_human_message_after_many_bot_posts_is_filed_once_and_the_cursor_moves(self):
        self.use([bot(n) for n in range(1, 301)] + [human(301, "Test")])
        msgs = S.pull(25, ingest=True)
        self.assertEqual([m["text"] for m in msgs], ["Test"])
        text = open(self.mail, encoding="utf-8").read()
        self.assertIn("slack-ts:%.6f" % (CURSOR + 301), text)
        self.assertIn("Test", text)
        self.assertEqual(S.pull(25, ingest=True), [])                       # a second pull files nothing new
        self.assertEqual(text.count("slack-ts:%.6f" % (CURSOR + 301)), 1)

    def test_an_incomplete_read_files_nothing_and_returns_none(self):
        with mock.patch.object(S, "PAGE_SIZE", 10), mock.patch.object(S, "MAX_PAGES", 2):
            self.use([bot(n) for n in range(1, 40)] + [human(40)])
            before = open(self.mail, encoding="utf-8").read()
            self.assertIsNone(S.pull(25, ingest=True))
        self.assertEqual(open(self.mail, encoding="utf-8").read(), before)


if __name__ == "__main__":
    unittest.main()
