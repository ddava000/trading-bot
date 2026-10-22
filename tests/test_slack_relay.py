"""slack_relay.sh: a Slack message is filed into the mailbox even when another session appended to it meanwhile.

2026-10-22: the Arm A run's Slack step appended to the END of AGENT_MAIL.md, committed, and pushed through
push_retry.sh, which answers a rejected push with `pull --rebase`. Another session had appended to the same end of the
same file since the run's checkout, the rebase CONFLICTED, was aborted, and the relay was discarded under a green run.
REAL git here: a bare remote, the runner's clone, and a second clone that plays the other session. The control shows
the old path really fails on this setup, so the new path passing means something.

Run:  python -m unittest discover -s tests -v      (needs git and bash)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, ".github", "scripts", "slack_relay.sh")
PUSH_RETRY = os.path.join(ROOT, ".github", "scripts", "push_retry.sh")
BASH, GIT = shutil.which("bash"), shutil.which("git")

# Stands in for slack_notify.py --pull-ingest: files one message unless the cursor (a marker in the file) says it is filed.
STUB = '''import os, sys
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENT_MAIL.md")
if "FAIL" in os.environ.get("STUB_MODE", ""):
    sys.exit(1)
t = open(p, encoding="utf-8").read()
if os.environ.get("STUB_MODE") != "quiet" and "## slack relay: devon says hi" not in t:
    with open(p, "a", encoding="utf-8", newline="\\n") as f:
        f.write("\\n## slack relay: devon says hi\\n")
'''


@unittest.skipUnless(BASH and GIT, "needs bash and git")
class RelayBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        self.runner = os.path.join(t, "runner")
        self.other = os.path.join(t, "other")
        self.counter = os.path.join(t, "rejections_left")
        self.git(t, "init", "-q", "--bare", "-b", "main", self.remote)
        self.git(t, "clone", "-q", self.remote, self.runner)
        self.put(self.runner, "AGENT_MAIL.md", "# mailbox\n\n## first entry\nbody\n")
        self.put(self.runner, "slack_notify.py", STUB)
        self.put(self.runner, "trade_log.jsonl", "")
        self.git(self.runner, "add", "-A")
        self.git(self.runner, "commit", "-q", "-m", "seed")
        self.git(self.runner, "push", "-q", "origin", "main")
        self.git(t, "clone", "-q", self.remote, self.other)

    def git(self, cwd, *args, check=True):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                           cwd=cwd, capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError("git %s failed: %s" % (" ".join(args), r.stderr))
        return r.stdout

    def put(self, repo, name, text, append=False):
        with open(os.path.join(repo, name), "a" if append else "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def other_session_appends(self, text="\n## laptop entry\nmore\n"):
        self.git(self.other, "pull", "-q", "--rebase", "origin", "main")
        self.put(self.other, "AGENT_MAIL.md", text, append=True)
        self.git(self.other, "add", "AGENT_MAIL.md")
        self.git(self.other, "commit", "-q", "-m", "laptop entry")
        self.git(self.other, "push", "-q", "origin", "main")

    def remote_mailbox(self):
        return self.git(self.runner, "--git-dir", self.remote, "show", "main:AGENT_MAIL.md")

    def relay(self, cwd=None, **env):
        e = dict(os.environ, RELAY_SLEEP="0", PYTHON=sys.executable, **env)
        return subprocess.run([BASH, RELAY], cwd=cwd or self.runner, env=e, capture_output=True, text=True)

    def reject_pushes(self, n):
        with open(self.counter, "w") as f:
            f.write(str(n))
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nc="%s"\nn=$(cat "$c" 2>/dev/null || echo 0)\n'
                    'if [ "$n" -gt 0 ]; then echo $((n-1)) > "$c"; echo "Internal Server Error" >&2; exit 1; fi\nexit 0\n'
                    % self.counter.replace("\\", "/"))
        os.chmod(hook, 0o755)

    # -- the control: the old path loses the relay on exactly this setup ---------------------------------------
    def test_control_the_old_commit_and_push_retry_path_loses_the_relay_when_another_session_appended(self):
        subprocess.run([sys.executable, "slack_notify.py"], cwd=self.runner, check=True)         # the old step: append in place
        self.git(self.runner, "add", "AGENT_MAIL.md")
        self.git(self.runner, "commit", "-q", "-m", "relay")
        self.other_session_appends()                                                             # the race
        env = dict(os.environ, PUSH_RETRY_DELAYS="0 0")
        r = subprocess.run([BASH, PUSH_RETRY, "slack relay", "warn"], cwd=self.runner, env=env, capture_output=True, text=True)
        self.assertIn("::warning", r.stdout)                                                     # it said so, in a warning
        self.assertNotIn("devon says hi", self.remote_mailbox(), "the setup must reproduce the loss, or the test proves nothing")

    # -- the fix ------------------------------------------------------------------------------------------------
    def test_a_relay_lands_even_though_another_session_appended_to_the_end_of_the_file_meanwhile(self):
        self.other_session_appends()
        r = self.relay()
        self.assertEqual(r.returncode, 0, r.stderr)
        box = self.remote_mailbox()
        self.assertIn("devon says hi", box)
        self.assertIn("laptop entry", box)                                  # the other session's entry survived
        self.assertEqual(box.count("devon says hi"), 1)

    def test_history_stays_linear_and_the_commit_is_the_relays(self):
        self.other_session_appends()
        self.relay()
        log = self.git(self.runner, "--git-dir", self.remote, "log", "--format=%s", "main").splitlines()
        self.assertIn("laptop entry", log)
        self.assertTrue(log[0].startswith("AGENT_MAIL: relay Slack messages from Devon"))
        self.assertEqual(self.git(self.runner, "--git-dir", self.remote, "rev-list", "--merges", "main").strip(), "")

    def test_the_runners_own_working_tree_is_not_touched(self):
        self.put(self.runner, "trade_log.jsonl", '{"trade": 1}\n')                    # uncommitted trade state mid-run
        head_before = self.git(self.runner, "rev-parse", "HEAD")
        self.other_session_appends()
        self.relay()
        self.assertEqual(self.git(self.runner, "rev-parse", "HEAD"), head_before)
        with open(os.path.join(self.runner, "trade_log.jsonl"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"trade": 1}\n')
        with open(os.path.join(self.runner, "AGENT_MAIL.md"), encoding="utf-8") as f:
            self.assertNotIn("devon says hi", f.read())
        self.assertEqual(self.git(self.runner, "status", "--porcelain", "--", "AGENT_MAIL.md").strip(), "")

    def test_the_throwaway_worktree_is_removed(self):
        self.relay()
        self.assertEqual(self.git(self.runner, "worktree", "list").count("\n"), 1)

    def test_nothing_to_file_makes_no_commit_and_exits_zero(self):
        before = self.git(self.runner, "--git-dir", self.remote, "rev-parse", "main")
        r = self.relay(STUB_MODE="quiet")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.git(self.runner, "--git-dir", self.remote, "rev-parse", "main"), before)

    def test_a_failing_pull_is_not_fatal_and_files_nothing(self):
        before = self.git(self.runner, "--git-dir", self.remote, "rev-parse", "main")
        r = self.relay(STUB_MODE="FAIL")
        self.assertEqual(r.returncode, 0)
        self.assertIn("slack pull errored", r.stdout)
        self.assertEqual(self.git(self.runner, "--git-dir", self.remote, "rev-parse", "main"), before)

    def test_a_rejected_push_is_retried_on_the_newest_main_and_lands(self):
        self.reject_pushes(2)
        r = self.relay()
        self.assertEqual(r.returncode, 0)
        self.assertIn("devon says hi", self.remote_mailbox())
        self.assertIn("landed on attempt 3", r.stdout)

    def test_a_server_that_never_accepts_gives_a_warning_not_silence_and_still_exits_zero(self):
        self.reject_pushes(99)
        r = self.relay(RELAY_TRIES="3")
        self.assertEqual(r.returncode, 0)
        self.assertIn("::warning title=Slack relay not filed", r.stdout)
        self.assertNotIn("devon says hi", self.remote_mailbox())

    def test_the_second_filing_does_not_duplicate_a_message(self):
        self.reject_pushes(1)
        self.relay()
        self.assertEqual(self.remote_mailbox().count("devon says hi"), 1)

    def test_it_never_force_pushes_and_never_rebases_or_merges(self):
        with open(RELAY, encoding="utf-8") as f:
            code = "\n".join(l for l in f.read().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"push[^\n]*(--force| -f |\+HEAD)")
        for word in ("git pull", "rebase", "merge", "--autostash"):
            self.assertNotIn(word, code)


class Wiring(unittest.TestCase):
    def test_the_script_is_ascii_lf_and_valid_bash(self):
        with open(RELAY, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\r", raw)
        if BASH:
            r = subprocess.run([BASH, "-n", RELAY], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_arm_a_step_runs_it_and_no_longer_rebases_the_relay(self):
        with open(os.path.join(ROOT, ".github", "workflows", "alpaca-bot.yml"), encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        step = next(s for s in wf["jobs"]["trade"]["steps"] if "Pull Slack messages" in s.get("name", ""))
        self.assertEqual(step["run"].strip(), "bash .github/scripts/slack_relay.sh")
        self.assertNotIn("push_retry", step["run"])
        self.assertEqual(step.get("if"), "always()")
        self.assertIn("SLACK_BOT_TOKEN", step["env"])
        self.assertIn("SLACK_CHANNEL_ID", step["env"])


if __name__ == "__main__":
    unittest.main()
