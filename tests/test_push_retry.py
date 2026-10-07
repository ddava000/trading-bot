"""push_retry.sh: Arm A's persist step must retry a rejected push and be LOUD when it never lands.

2026-10-07: GitHub rejected every write for about 9 minutes. The persist step ended in `|| true`, so one run's
status heartbeat push was rejected, the run stayed green, and status.json froze with no trace. Worse, a run that
had placed a trade would have lost its trade_log and holds ledger (the stop levels) the same silent way.

These tests use REAL git against a bare remote whose pre-receive hook rejects the first N pushes, which is what
a server error looks like to the client. Every guard is shown able to fail as well as succeed.

Run:  python -m unittest discover -s tests -v      (needs git and bash)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "push_retry.sh")
BASH = shutil.which("bash")
GIT = shutil.which("git")


@unittest.skipUnless(BASH and GIT, "needs bash and git")
class PushRetryBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.remote = os.path.join(self.tmp.name, "remote.git")
        self.work = os.path.join(self.tmp.name, "work")
        self.counter = os.path.join(self.tmp.name, "rejections_left")
        self.git(self.tmp.name, "init", "-q", "--bare", "-b", "main", self.remote)
        self.git(self.tmp.name, "clone", "-q", self.remote, self.work)
        # the workflow sets this before it commits; a rebase needs a committer identity
        self.git(self.work, "config", "user.name", "alpaca-bot")
        self.git(self.work, "config", "user.email", "alpaca-bot@users.noreply.github.com")
        self.write("seed.txt", "seed")
        self.commit("seed")
        self.git(self.work, "push", "-q", "origin", "main")
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/bin/sh\nc="%s"\nn=$(cat "$c" 2>/dev/null || echo 0)\n'
                    'if [ "$n" -gt 0 ]; then echo $((n-1)) > "$c"; echo "Internal Server Error" >&2; exit 1; fi\nexit 0\n'
                    % self.counter.replace("\\", "/"))
        os.chmod(hook, 0o755)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout

    def write(self, name, text):
        with open(os.path.join(self.work, name), "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, msg):
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", msg)

    def reject_next(self, n):
        with open(self.counter, "w", encoding="utf-8") as f:
            f.write(str(n))

    def run_script(self, mode="warn", delays="0 0 0 0", what="status heartbeat"):
        env = dict(os.environ, PUSH_RETRY_DELAYS=delays)
        r = subprocess.run([BASH, SCRIPT, what, mode], cwd=self.work, env=env, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def remote_has(self, subject):
        log = subprocess.run(["git", "log", "--format=%s", "main"], cwd=self.remote, capture_output=True, text=True).stdout
        return subject in log.split("\n")

    def test_a_clean_push_lands_first_time_and_says_nothing_loud(self):
        self.write("a.txt", "a")
        self.commit("status heartbeat A")
        rc, out = self.run_script()
        self.assertEqual(rc, 0)
        self.assertTrue(self.remote_has("status heartbeat A"))
        self.assertNotIn("::error", out + "")
        self.assertNotIn("::warning", out)

    def test_server_errors_are_retried_until_it_lands(self):
        self.write("a.txt", "a")
        self.commit("status heartbeat B")
        self.reject_next(2)
        rc, out = self.run_script()
        self.assertEqual(rc, 0)
        self.assertTrue(self.remote_has("status heartbeat B"))
        self.assertIn("landed on attempt 3 of 4", out)
        self.assertNotIn("::warning", out)

    def test_a_heartbeat_that_never_lands_is_green_but_leaves_a_warning_annotation(self):
        self.write("a.txt", "a")
        self.commit("status heartbeat C")
        self.reject_next(99)
        rc, out = self.run_script(mode="warn")
        self.assertEqual(rc, 0)
        self.assertFalse(self.remote_has("status heartbeat C"))
        self.assertIn("::warning title=Arm A heartbeat not saved::", out)
        self.assertIn("after 4 attempts", out)
        self.assertIn("githubstatus.com", out)

    def test_trade_state_that_never_lands_fails_the_step_loudly(self):
        """The case that matters: a trade_log or holds ledger the runner is about to throw away."""
        self.write("trade_log.jsonl", "{}")
        self.commit("trade log D")
        self.reject_next(99)
        rc, out = self.run_script(mode="fatal", what="trade log D")
        self.assertEqual(rc, 1)
        self.assertFalse(self.remote_has("trade log D"))
        self.assertIn("::error title=Arm A state not saved::", out)
        self.assertIn("'trade log D'", out)

    def test_trade_state_that_lands_on_a_retry_is_not_an_error(self):
        self.write("trade_log.jsonl", "{}")
        self.commit("trade log E")
        self.reject_next(3)
        rc, out = self.run_script(mode="fatal", what="trade log E")
        self.assertEqual(rc, 0)
        self.assertTrue(self.remote_has("trade log E"))
        self.assertNotIn("::error", out)

    def test_a_non_fast_forward_is_rebased_and_pushed(self):
        other = os.path.join(self.tmp.name, "other")
        self.git(self.tmp.name, "clone", "-q", self.remote, other)
        with open(os.path.join(other, "other.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "someone else pushed first")
        self.git(other, "push", "-q", "origin", "main")
        self.write("a.txt", "a")
        self.commit("status heartbeat F")
        rc, out = self.run_script()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.remote_has("status heartbeat F"))
        self.assertTrue(self.remote_has("someone else pushed first"))

    def test_a_rebase_conflict_is_aborted_and_leaves_the_branch_usable_and_the_failure_loud(self):
        """Both sides changed the same file. The old code left a half-done rebase and swallowed the failure."""
        self.write("status.json", "base")
        self.commit("base")
        self.git(self.work, "push", "-q", "origin", "main")
        other = os.path.join(self.tmp.name, "other")
        self.git(self.tmp.name, "clone", "-q", self.remote, other)
        with open(os.path.join(other, "status.json"), "w", encoding="utf-8") as f:
            f.write("theirs")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "their status")
        self.git(other, "push", "-q", "origin", "main")
        self.write("status.json", "ours")
        self.commit("trade log H")
        rc, out = self.run_script(mode="fatal", what="trade log H")
        self.assertEqual(rc, 1)
        self.assertIn("::error title=Arm A state not saved::", out)
        self.assertIn("aborting it", out)
        branch = self.git(self.work, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.assertEqual(branch, "main")                                    # not detached
        self.assertFalse(os.path.exists(os.path.join(self.work, ".git", "rebase-merge")))
        self.assertEqual(self.git(self.work, "log", "-1", "--format=%s").strip(), "trade log H")   # our commit is intact

    def test_it_never_force_pushes(self):
        """A history rewrite on the remote must survive: the script cannot overwrite it."""
        other = os.path.join(self.tmp.name, "other")
        self.git(self.tmp.name, "clone", "-q", self.remote, other)
        with open(os.path.join(other, "o.txt"), "w", encoding="utf-8") as f:
            f.write("keep me")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "must survive")
        self.git(other, "push", "-q", "origin", "main")
        self.write("a.txt", "a")
        self.commit("status heartbeat G")
        self.run_script()
        self.assertTrue(self.remote_has("must survive"))

    def test_the_default_delays_stay_inside_the_runs_14_minute_budget(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('PUSH_RETRY_DELAYS:-0 5 15 30', text)
        self.assertLessEqual(sum(int(x) for x in "0 5 15 30".split()), 60)


class Wiring(unittest.TestCase):
    def wf(self):
        with open(os.path.join(ROOT, ".github", "workflows", "alpaca-bot.yml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def step(self, name):
        return next(s for s in self.wf()["jobs"]["trade"]["steps"] if s.get("name", "").startswith(name))

    def test_the_script_exists_is_ascii_and_is_valid_bash(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.isascii())
        if BASH:
            r = subprocess.run([BASH, "-n", SCRIPT], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_shell_scripts_are_pinned_to_lf_so_a_windows_commit_cannot_break_the_runner(self):
        with open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8") as f:
            rules = [ln.split() for ln in f.read().splitlines() if ln.strip() and not ln.startswith("#")]
        self.assertIn(["*.sh", "text", "eol=lf"], rules)

    def test_the_persist_step_uses_it_and_trade_state_is_fatal(self):
        run = self.step("Persist trade log")["run"]
        self.assertIn(".github/scripts/push_retry.sh", run)
        self.assertIn("fatal", run)
        self.assertIn("warn", run)

    def test_no_push_in_the_arm_a_workflow_swallows_its_failure_any_more(self):
        for s in self.wf()["jobs"]["trade"]["steps"]:
            run = str(s.get("run", ""))
            self.assertNotIn("|| true", "\n".join(l for l in run.splitlines() if "git push" in l), s.get("name"))
            self.assertNotRegex(run, r"git push \|\|")

    def test_the_slack_relay_commit_uses_it_too(self):
        run = self.step("Pull Slack messages")["run"]
        self.assertIn("push_retry.sh", run)


if __name__ == "__main__":
    unittest.main()
