"""slack_live.py: the one-command live read of #trading-bots that every "check mail" from CLOUD starts with.

Devon 2026-10-22: "you should always check slack as well when i ask you to check mail". CLOUD has no Slack token, the relay
into the mailbox lags, so the only live read is a GitHub job; this wraps dispatching it and waiting for THAT run. These tests
use a fake `gh` and pin: the right run is waited for, the log is filtered to what matters (never the secrets or the command
echo), a failed job is exit 1, and "could not start" is exit 2, never a quiet 0.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import slack_live as L  # noqa: E402

LOG = "\n".join([
    "post\tPost to Slack\t2026-10-25T18:37:53.1234567Z ##[group]Run if [ \"read\" = \"mail\" ]; then",
    "post\tPost to Slack\t2026-10-25T18:37:53.1234567Z python slack_notify.py --pull 25",
    "post\tPost to Slack\t2026-10-25T18:37:53.1234567Z   SLACK_BOT_TOKEN: ***",
    "post\tPost to Slack\t2026-10-25T18:37:53.1234567Z   SLACK_CHANNEL_ID: ***",
    "post\tPost to Slack\t2026-10-25T18:37:53.1234567Z shell: /usr/bin/bash -e {0}",
    "post\tPost to Slack\t2026-10-25T18:37:54.1234567Z   [slack 1792697424.371089] <U0BTE7872PJ> (in a thread) testing",
    "post\tPost to Slack\t2026-10-25T18:37:54.2234567Z   [slack: filed 1 message(s) into AGENT_MAIL.md]",
    "post\tPost to Slack\t2026-10-25T18:37:54.3234567Z   [slack: read OK, no new channel messages (looked at 21 message(s) since the last relay)]",
    "post\tPost to Slack\t2026-10-25T18:37:55.0000000Z   [10-25 14:21 ET] *Weekly bot audit*",
    "post\tPost to Slack\t2026-10-25T18:37:55.0000001Z       Arm A trailed SPY by 0.25 points",
    "post\tPost to Slack\t2026-10-25T18:37:55.0000002Z   [peek: 2 bot message(s) matched 'ARM A', looking through the newest 150]",
    "post\tPost to Slack\t2026-10-25T18:37:55.0000003Z   [scan: 50 message(s) read, 2 human, 1 in threads]",
    "post\tPost to Slack\t2026-10-25T18:37:56.0000000Z Post job cleanup.",
])


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


class FakeGh:
    """Stands in for slack_live.gh and records every call."""
    def __init__(self, dispatch_out="https://github.com/ddava000/trading-bot/actions/runs/777\n", dispatch_rc=0,
                 view_json='{"status": "completed", "conclusion": "success"}', log=LOG, list_json='[{"databaseId": 555, "createdAt": "x"}]'):
        self.calls, self.dispatch_out, self.dispatch_rc = [], dispatch_out, dispatch_rc
        self.view_json, self.log, self.list_json = view_json, log, list_json

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("workflow", "run"):
            return completed(self.dispatch_rc, self.dispatch_out, "boom" if self.dispatch_rc else "")
        if args[:2] == ("run", "list"):
            return completed(0, self.list_json)
        if args[:2] == ("run", "view") and "--log" in args:
            return completed(0, self.log)
        if args[:2] == ("run", "view"):
            return completed(0, self.view_json)
        raise AssertionError(args)


def run_main(argv, gh):
    out = io.StringIO()
    with mock.patch.object(L, "gh", gh), mock.patch.object(L.time, "sleep", lambda s: None), contextlib.redirect_stdout(out):
        rc = L.main(argv)
    return rc, out.getvalue()


class Filtering(unittest.TestCase):
    def test_the_readers_own_lines_and_peeked_bodies_are_kept(self):
        got = L.interesting(LOG)
        self.assertIn("  [slack 1792697424.371089] <U0BTE7872PJ> (in a thread) testing", got)
        self.assertIn("  [slack: read OK, no new channel messages (looked at 21 message(s) since the last relay)]", got)
        self.assertTrue(any("[scan: 50 message(s) read" in g for g in got))
        self.assertTrue(any("[peek: 2 bot message(s)" in g for g in got))
        self.assertTrue(any("[10-25 14:21 ET]" in g for g in got))
        self.assertTrue(any("Arm A trailed SPY" in g for g in got))

    def test_secrets_the_command_echo_and_actions_noise_are_dropped(self):
        text = "\n".join(L.interesting(LOG))
        for noise in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "python slack_notify.py", "shell:", "##[group]", "Post job cleanup"):
            self.assertNotIn(noise, text)

    def test_the_refused_channel_line_is_kept_even_though_it_names_a_secret_variable(self):
        line = ("post	Post to Slack	2026-10-25T18:37:54.0000000Z   [slack REFUSED: SLACK_CHANNEL_ID is C_X, but this bot may only read "
                "C0BSHTPCQ22 (#trading-bots).]")
        self.assertEqual(len(L.interesting(line)), 1)
        self.assertIn("SLACK_CHANNEL_ID is C_X", L.interesting(line)[0])

    def test_a_reader_failure_line_is_kept(self):
        line = "post\tPost to Slack\t2026-10-25T18:37:54.0000000Z   [slack read unavailable - token/channel unset, or channel refused]"
        self.assertEqual(len(L.interesting(line)), 1)


class Dispatching(unittest.TestCase):
    def test_the_run_id_comes_from_the_dispatch_url_not_from_a_race_with_other_runs(self):
        gh = FakeGh()
        rc, out = run_main(["read"], gh)
        self.assertEqual(rc, 0)
        self.assertIn("as run 777", out)
        views = [c for c in gh.calls if c[:2] == ("run", "view")]
        self.assertTrue(views and all(c[2] == "777" for c in views))
        self.assertFalse([c for c in gh.calls if c[:2] == ("run", "list")], "no run list needed when the dispatch printed the id")

    def test_an_older_gh_that_prints_no_url_falls_back_to_the_newest_run(self):
        gh = FakeGh(dispatch_out="")
        rc, out = run_main(["read"], gh)
        self.assertEqual(rc, 0)
        self.assertIn("as run 555", out)

    def test_each_mode_sends_the_right_inputs(self):
        for argv, want in ((["read"], ["mode=read"]), (["scan"], ["mode=scan"]), (["scan", "80"], ["mode=scan", "text=80"]),
                           (["peek", "ARM A", "120"], ["mode=peek", "text=ARM A", "limit=120"]),
                           (["peek", "ARM A"], ["mode=peek", "text=ARM A"]), (["say", "hello", "there"], ["mode=say", "text=hello there"])):
            gh = FakeGh()
            run_main(argv, gh)
            sent = [c for c in gh.calls if c[:2] == ("workflow", "run")][0]
            for w in want:
                self.assertIn(w, sent, (argv, sent))
            self.assertIn("slack-test.yml", sent)

    def test_no_argument_means_read(self):
        gh = FakeGh()
        run_main([], gh)
        self.assertIn("mode=read", [c for c in gh.calls if c[:2] == ("workflow", "run")][0])


class Outcomes(unittest.TestCase):
    def test_success_prints_the_filtered_log(self):
        rc, out = run_main(["read"], FakeGh())
        self.assertEqual(rc, 0)
        self.assertIn("(in a thread) testing", out)
        self.assertNotIn("SLACK_BOT_TOKEN", out)

    def test_a_failed_job_is_exit_1_and_says_so(self):
        rc, out = run_main(["read"], FakeGh(view_json='{"status": "completed", "conclusion": "failure"}'))
        self.assertEqual(rc, 1)
        self.assertIn("did not work", out)

    def test_a_dispatch_that_fails_is_exit_2_not_a_quiet_zero(self):
        rc, out = run_main(["read"], FakeGh(dispatch_rc=1))
        self.assertEqual(rc, 2)
        self.assertIn("could not dispatch", out)

    def test_no_gh_installed_is_exit_2_with_the_fix(self):
        def missing(*a):
            raise FileNotFoundError("gh")
        rc, out = run_main(["read"], missing)
        self.assertEqual(rc, 2)
        self.assertIn("gh auth login", out)

    def test_a_job_that_never_finishes_is_exit_2_and_names_the_run(self):
        gh = FakeGh(view_json='{"status": "queued", "conclusion": null}')
        ticks = iter(range(0, 100000, 100))
        with mock.patch.object(L.time, "time", lambda: next(ticks)):
            rc, out = run_main(["read"], gh)
        self.assertEqual(rc, 2)
        self.assertIn("did not finish", out)
        self.assertIn("777", out)

    def test_bad_usage_is_exit_2_and_posts_nothing(self):
        for argv in (["peek"], ["say"], ["frobnicate"]):
            gh = FakeGh()
            rc, _ = run_main(argv, gh)
            self.assertEqual(rc, 2, argv)
            self.assertFalse([c for c in gh.calls if c[:2] == ("workflow", "run")], argv)


class Hygiene(unittest.TestCase):
    def test_the_file_is_ascii_so_a_windows_console_cannot_choke_on_it(self):
        with open(os.path.join(ROOT, "slack_live.py"), "rb") as f:
            self.assertTrue(f.read().isascii())

    def test_it_is_in_the_ascii_check_list(self):
        with open(os.path.join(ROOT, "check_ascii.py"), encoding="utf-8") as f:
            self.assertIn('"slack_live.py"', f.read())

    def test_only_say_can_post(self):
        with open(os.path.join(ROOT, "slack_live.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count('"mode=" + mode'), 1)
        self.assertIn('if mode not in ("read", "scan", "peek", "say")', src)


if __name__ == "__main__":
    unittest.main()
