"""Arm A's workflow: the run-start marker and the bounded brief step (Devon 2026-11-05, chosen from clickable options; the hang found the same day).

1. RUN-START MARKER. Right after checkout the job pushes ONE parentless commit to the branch `arm-a-start`, message "arm-a run-start run=<id> ...".
   arm_a_watch reads it through git as exact proof that a run STARTED (the Actions API served stale state on 2026-10-22 and 2026-11-04). It must never
   touch main, never cost the bot a run, and hold only the newest marker. These tests read the YAML AND EXECUTE the step's real script against a
   throwaway local git remote, so a quoting mistake in the shell cannot hide behind a green YAML check.
2. BRIEF STEP. Run 42040796649 (09:00 ET, 2026-11-05) hung 14 minutes in `gh workflow run brief.yml` until the job timeout cancelled the run. The
   step runs BEFORE the bot on every run, so a hang is a lost run. It now has a hard `timeout`, a step timeout and continue-on-error. A fake `gh`
   that never returns proves the guard works.

Run:  python -m unittest discover -s tests -v      (needs PyYAML, git and bash)
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "alpaca-bot.yml")
BASH = shutil.which("bash")


def steps():
    with open(WORKFLOW, encoding="utf-8") as f:
        return yaml.safe_load(f)["jobs"]["trade"]["steps"]


def step(name):
    return next(s for s in steps() if s.get("name") == name)


def never_blocks(s, max_minutes):
    """A step that may fail but cannot hold the run: continue-on-error AND a step timeout of at most `max_minutes`."""
    t = s.get("timeout-minutes")
    return s.get("continue-on-error") is True and isinstance(t, (int, float)) and 0 < t <= max_minutes


class Order(unittest.TestCase):
    def test_the_marker_comes_straight_after_checkout_and_before_the_brief_and_the_bot(self):
        names = [s.get("name") or s.get("uses") for s in steps()]
        self.assertEqual(names[0], "Checkout code")
        self.assertEqual(names[1], "Mark run start")                       # before anything that can hang, so it proves the JOB started
        self.assertLess(names.index("Mark run start"), names.index("Ensure fresh research brief"))
        self.assertLess(names.index("Ensure fresh research brief"), names.index("Run Alpaca bot"))


class MarkerStepShape(unittest.TestCase):
    def test_it_cannot_block_or_fail_the_run(self):
        self.assertTrue(never_blocks(step("Mark run start"), 3), step("Mark run start"))

    def test_it_writes_only_to_the_marker_branch_and_never_to_main(self):
        script = step("Mark run start")["run"]
        self.assertIn("refs/heads/arm-a-start", script)
        self.assertIn("--force", script)
        self.assertIn("commit-tree", script)
        self.assertIn("timeout 60 git push", script)
        for forbidden in ("refs/heads/main", "origin main", ":main", "HEAD:main", "git push origin HEAD"):
            self.assertNotIn(forbidden, script)

    def test_the_marker_names_the_run_and_the_start_time(self):
        script = step("Mark run start")["run"]
        for token in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "date -u"):
            self.assertIn(token, script)

    def test_control_the_detector_can_say_no(self):
        self.assertFalse(never_blocks({"run": "x"}, 3))
        self.assertFalse(never_blocks({"run": "x", "continue-on-error": True}, 3))               # no timeout
        self.assertFalse(never_blocks({"run": "x", "timeout-minutes": 2}, 3))                    # can fail the run
        self.assertFalse(never_blocks({"run": "x", "continue-on-error": True, "timeout-minutes": 14}, 3))   # too long to be a guard


@unittest.skipUnless(BASH and shutil.which("git"), "needs bash and git")
class MarkerStepRuns(unittest.TestCase):
    """The step's REAL script, executed with bash against a local bare repo standing in for GitHub."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.work = os.path.join(self.tmp, "work")
        self.git("init", "-q", "--bare", self.origin, cwd=self.tmp)
        self.git("clone", "-q", self.origin, self.work, cwd=self.tmp)
        with open(os.path.join(self.work, "status.json"), "w") as f:
            f.write("{}")
        self.git("add", "status.json", cwd=self.work)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init", cwd=self.work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)
        self.main_sha = self.git("rev-parse", "refs/heads/main", cwd=self.origin).strip()

    def git(self, *args, cwd):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout

    def run_step(self, run_id, attempt="1", cwd=None):
        env = dict(os.environ, GITHUB_RUN_ID=str(run_id), GITHUB_RUN_ATTEMPT=attempt)
        return subprocess.run([BASH, "-e", "-c", step("Mark run start")["run"]], cwd=cwd or self.work, env=env, capture_output=True, text=True, timeout=120)

    def marker(self):
        return self.git("log", "-1", "--format=%B", "refs/heads/arm-a-start", cwd=self.origin).strip()

    def test_the_step_pushes_a_parentless_marker_with_the_run_id_and_leaves_main_alone(self):
        r = self.run_step(424242)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.marker()
        self.assertRegex(m, r"^arm-a run-start run=424242 attempt=1 started=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/arm-a-start", cwd=self.origin).strip(), "1")      # parentless: one commit
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin).strip(), self.main_sha)            # main untouched
        self.assertEqual(self.git("status", "--porcelain", cwd=self.work).strip(), "")                                # no working-tree change

    def test_the_next_run_replaces_the_marker_so_only_the_newest_survives(self):
        self.assertEqual(self.run_step(100).returncode, 0)
        self.assertEqual(self.run_step(101, attempt="2").returncode, 0)
        self.assertIn("run=101 attempt=2", self.marker())
        self.assertEqual(self.git("rev-list", "--count", "refs/heads/arm-a-start", cwd=self.origin).strip(), "1")
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin).strip(), self.main_sha)

    def test_it_needs_no_git_identity_of_its_own(self):
        """A GitHub runner has no user.name or user.email configured: commit-tree would die without the inline identity."""
        env = dict(os.environ, GITHUB_RUN_ID="5", GITHUB_RUN_ATTEMPT="1", HOME=self.tmp, XDG_CONFIG_HOME=self.tmp,
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            env.pop(k, None)
        subprocess.run(["git", "config", "--local", "--unset-all", "user.name"], cwd=self.work, capture_output=True)
        subprocess.run(["git", "config", "--local", "--unset-all", "user.email"], cwd=self.work, capture_output=True)
        r = subprocess.run([BASH, "-e", "-c", step("Mark run start")["run"]], cwd=self.work, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("run=5 ", self.marker())

    def test_when_the_push_cannot_land_the_step_fails_fast_and_main_is_untouched(self):
        """Fails (the workflow's continue-on-error absorbs it) rather than hanging or damaging anything."""
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "does-not-exist.git"), cwd=self.work)
        t0 = time.time()
        r = self.run_step(7)
        self.assertNotEqual(r.returncode, 0)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin).strip(), self.main_sha)


class BriefStepCannotHang(unittest.TestCase):
    def test_the_step_is_bounded_three_ways(self):
        s = step("Ensure fresh research brief")
        self.assertTrue(never_blocks(s, 5), s)
        calls = [ln for ln in s["run"].splitlines() if "gh workflow run" in ln]
        self.assertTrue(calls)
        for ln in calls:
            self.assertRegex(ln, r"timeout \d+ gh workflow run brief\.yml", ln)          # every call is wrapped; none is bare

    @unittest.skipUnless(BASH, "needs bash")
    def test_a_gh_that_never_returns_cannot_hold_the_step(self):
        """2026-11-05: the real gh hung 14 minutes. A fake gh that sleeps for a minute stands in; the step's `timeout 60` is shortened to 1 second
        for the test, which exercises the same wrapper and the same fallthrough."""
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "gh")
            with open(fake, "w", newline="\n") as f:
                f.write("#!/bin/sh\nsleep 60\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            with open(os.path.join(d, "daily_plan.json"), "w") as f:
                f.write('{"date": "2000-01-01"}')                      # a stale plan: the step takes the dispatch branch
            script = step("Ensure fresh research brief")["run"].replace("timeout 60 gh", "timeout 1 gh")
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"], GITHUB_REPOSITORY="o/r", GH_TOKEN="x")
            t0 = time.time()
            r = subprocess.run([BASH, "-e", "-c", script], cwd=d, env=env, capture_output=True, text=True, timeout=45)
            self.assertLess(time.time() - t0, 20)
            self.assertEqual(r.returncode, 0, r.stderr)                 # the step does not fail either
            self.assertIn("dispatching brief.yml", r.stdout)
            self.assertIn("brief dispatch did not complete", r.stdout)

    @unittest.skipUnless(BASH, "needs bash")
    def test_control_without_the_timeout_the_same_fake_gh_holds_the_step(self):
        """So the test above proves the wrapper and not a fast fake: the unguarded call really does wait."""
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "gh")
            with open(fake, "w", newline="\n") as f:
                f.write("#!/bin/sh\nsleep 4\n")
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
            t0 = time.time()
            subprocess.run([BASH, "-e", "-c", 'gh workflow run brief.yml --repo o/r || true'], cwd=d, env=env, capture_output=True, timeout=45)
            self.assertGreaterEqual(time.time() - t0, 3.5)


if __name__ == "__main__":
    unittest.main()
