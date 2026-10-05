"""mailbox_notify.py: Devon gets a Slack message whenever a push adds mail that needs a session woken.

Devon 2026-10-01: "they should all message me when i need to have a session check the mail". The tests build
a real git history (the notifier reads the diff between a push's two commits), replace Slack with a recorder,
and pin who gets named, who never does, what an ALIGNMENT CHECK adds, and that failure to deliver is loud.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mail_check as M          # noqa: E402
import mailbox_notify as N      # noqa: E402
import slack_notify as S        # noqa: E402


def e(stamp, frm, to, subject="s"):
    return "## [%s ET] %s -> %s  [%s]\nbody\n\n" % (stamp, frm, to, subject)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.cwd)
        self.git("init", "-q", "-b", "main")
        self.posts = []

    def git(self, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, body_to_append, first=False):
        mode = "w" if first else "a"
        with open("AGENT_MAIL.md", mode, encoding="utf-8") as f:
            f.write(("# AGENT_MAIL\n\n" if first else "") + body_to_append)
        self.git("add", "AGENT_MAIL.md")
        self.git("commit", "-q", "-m", "m")
        return self.git("rev-parse", "HEAD")

    def run_notify(self, before, after, enabled=True, post_ok=True):
        def rec(text, untrusted=False):
            self.posts.append(text)
            return post_ok
        out = io.StringIO()
        with mock.patch.object(S, "enabled", lambda: enabled), mock.patch.object(S, "post", rec), \
                contextlib.redirect_stdout(out):
            rc = N.main([before, after])
        return rc, out.getvalue()

    def notify_for(self, appended):
        """A baseline commit, then a push that adds `appended`; returns (rc, posts)."""
        base = self.commit(e("2026-10-01 10:00", "laptop", "laptop", "baseline"), first=True)
        tip = self.commit(appended)
        rc, _ = self.run_notify(base, tip)
        return rc, self.posts


class WhoGetsNamed(Repo):
    def test_mail_to_cloud_says_have_cloud_check_mail(self):
        rc, posts = self.notify_for(e("2026-10-01 11:00", "laptop", "cloud", "a request"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].splitlines()[0], "*Have CLOUD check mail.*")
        self.assertIn("LAPTOP BOT wrote (line", posts[0])
        self.assertIn("a request", posts[0])

    def test_a_broadcast_names_the_other_interactive_session_and_never_the_writer(self):
        _, posts = self.notify_for(e("2026-10-01 11:00", "cloud[35819496]", "all", "news"))
        self.assertEqual(posts[0].splitlines()[0], "*Have LAPTOP BOT check mail.*")
        self.posts.clear()
        self.setUp()
        _, posts = self.notify_for(e("2026-10-01 11:00", "laptop", "all", "news"))
        self.assertEqual(posts[0].splitlines()[0], "*Have CLOUD check mail.*")

    def test_each_wakeable_session_is_named_by_its_own_name(self):
        for to, name in (("cloud[daily]", "BOT DAILY CHECK"), ("laptop[daily]", "LAPTOP BOT DAILY CHECK"),
                         ("laptop", "LAPTOP BOT")):
            with self.subTest(to=to):
                self.setUp()
                _, posts = self.notify_for(e("2026-10-01 11:00", "audit", to, "x"))
                self.assertEqual(posts[0].splitlines()[0], "*Have %s check mail.*" % name)

    def test_mail_to_the_audit_alone_wakes_nobody_and_posts_nothing(self):
        rc, posts = self.notify_for(e("2026-10-01 11:00", "laptop", "audit", "for sunday"))
        self.assertEqual((rc, posts), (0, []))

    def test_a_session_writing_to_itself_posts_nothing(self):
        _, posts = self.notify_for(e("2026-10-01 11:00", "laptop", "laptop", "a note to self"))
        self.assertEqual(posts, [])

    def test_the_older_entries_already_in_the_file_are_not_announced_again(self):
        _, posts = self.notify_for(e("2026-10-01 11:00", "laptop", "cloud", "new one"))
        self.assertEqual(len(posts), 1)                                   # the baseline entry (to laptop) is not re-posted

    def test_a_push_of_many_entries_is_capped_and_says_how_many_more(self):
        body = "".join(e("2026-10-01 11:%02d" % i, "laptop", "cloud", "n%d" % i) for i in range(8))
        _, posts = self.notify_for(body)
        self.assertEqual(len(posts), N.MAX_POSTS + 1)
        self.assertIn("and 3 more", posts[-1])

    def test_an_unparseable_heading_is_skipped_not_fatal(self):
        rc, posts = self.notify_for("## [2026-10-01 11:00 ET] cloud/daily -> laptop  [bad]\nbody\n\n"
                                    + e("2026-10-01 11:01", "laptop", "cloud", "good"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 1)


class AlignmentChecks(Repo):
    def test_a_request_names_who_to_wake_and_lists_who_is_still_to_confirm(self):
        _, posts = self.notify_for(e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK 2026-10-01: reply"))
        text = posts[0]
        self.assertEqual(text.splitlines()[0], "*Have LAPTOP BOT check mail.*")
        self.assertIn("Still to confirm (4 of 5):", text)
        self.assertIn("- LAPTOP BOT: Devon says 'check mail' to LAPTOP BOT", text)
        self.assertIn("- BOT WEEKLY AUDIT: automatic: it reads mail itself every Sunday", text)

    def test_a_reply_posts_the_shrinking_list_even_though_nobody_needs_waking_for_it(self):
        base = self.commit(e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK 2026-10-01"), first=True)
        tip = self.commit(e("2026-10-01 17:05", "laptop", "cloud", "ALIGNED: LAPTOP BOT read ECOSYSTEM.md"))
        self.run_notify(base, tip)
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.posts[0].startswith("*Have"), self.posts[0])      # a confirmation wakes nobody
        self.assertIn("Still to confirm (3 of 5):", self.posts[0])
        self.assertNotIn("- LAPTOP BOT:", self.posts[0])

    def test_a_reply_addressed_to_all_does_not_ping_the_other_interactive_session_either(self):
        base = self.commit(e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK"), first=True)
        tip = self.commit(e("2026-10-01 17:05", "laptop", "all", "ALIGNED: LAPTOP BOT"))
        self.run_notify(base, tip)
        self.assertNotIn("Have CLOUD check mail", self.posts[0])

    def test_all_five_confirmed_says_so(self):
        base = self.commit(e("2026-10-01 17:00", "cloud[35819496]", "all", "ALIGNMENT CHECK 2026-10-01")
                           + e("2026-10-01 17:01", "cloud[daily]", "all", "ALIGNED: BOT DAILY CHECK")
                           + e("2026-10-01 17:02", "laptop[daily]", "all", "ALIGNED: LAPTOP BOT DAILY CHECK")
                           + e("2026-10-01 17:03", "audit", "all", "ALIGNED: BOT WEEKLY AUDIT"), first=True)
        tip = self.commit(e("2026-10-01 17:05", "laptop", "all", "ALIGNED: LAPTOP BOT"))
        self.run_notify(base, tip)
        self.assertIn("All five sessions have confirmed ALIGNED.", self.posts[0])


class FailureIsLoud(Repo):
    def test_slack_not_configured_is_a_red_run_not_a_quiet_one(self):
        base = self.commit(e("2026-10-01 10:00", "laptop", "laptop", "baseline"), first=True)
        tip = self.commit(e("2026-10-01 11:00", "laptop", "cloud", "x"))
        rc, out = self.run_notify(base, tip, enabled=False)
        self.assertEqual(rc, 1)
        self.assertIn("SLACK NOT CONFIGURED", out)

    def test_a_failed_post_is_a_red_run(self):
        base = self.commit(e("2026-10-01 10:00", "laptop", "laptop", "baseline"), first=True)
        tip = self.commit(e("2026-10-01 11:00", "laptop", "cloud", "x"))
        self.assertEqual(self.run_notify(base, tip, post_ok=False)[0], 1)

    def test_nothing_to_announce_is_green_even_without_slack(self):
        base = self.commit(e("2026-10-01 10:00", "laptop", "laptop", "baseline"), first=True)
        tip = self.commit(e("2026-10-01 11:00", "laptop", "audit", "x"))
        self.assertEqual(self.run_notify(base, tip, enabled=False)[0], 0)

    def test_an_unknown_before_commit_falls_back_to_the_previous_one(self):
        self.commit(e("2026-10-01 10:00", "laptop", "laptop", "baseline"), first=True)
        tip = self.commit(e("2026-10-01 11:00", "laptop", "cloud", "x"))
        for before in ("0" * 40, "f" * 40, ""):
            with self.subTest(before=before):
                self.posts.clear()
                self.assertEqual(self.run_notify(before, tip)[0], 0)
                self.assertEqual(len(self.posts), 1)


class WorkflowWiring(unittest.TestCase):
    def test_it_runs_on_pushes_that_touch_only_the_mailbox_and_passes_the_webhook(self):
        with open(os.path.join(ROOT, ".github", "workflows", "mailbox-notify.yml"), encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        on = wf.get(True) or wf.get("on")
        self.assertEqual(on["push"]["paths"], ["AGENT_MAIL.md"])
        step = wf["jobs"]["notify"]["steps"][-1]
        self.assertIn("SLACK_WEBHOOK_URL", step["env"])
        self.assertIn("mailbox_notify.py", step["run"])
        self.assertGreaterEqual(wf["jobs"]["notify"]["steps"][0]["with"]["fetch-depth"], 2)

    def test_no_concurrency_group_so_a_delayed_run_is_never_dropped_for_a_newer_one(self):
        with open(os.path.join(ROOT, ".github", "workflows", "mailbox-notify.yml"), encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        self.assertNotIn("concurrency", wf)
        self.assertNotIn("concurrency", wf["jobs"]["notify"])


if __name__ == "__main__":
    unittest.main()
