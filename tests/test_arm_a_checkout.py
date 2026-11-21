"""Arm A's workflow must check out the CURRENT tip of the branch, not the commit that was current when the run was created.

WHY (2026-11-04, found while explaining passes of 2026-10-26 that left no status.json commit; the count is THREE, see below). actions/checkout, given no `ref`,
checks out the event's SHA: for a workflow_dispatch that is the tip at the moment the RUN WAS CREATED. Arm A's runs can wait 4 to 13
minutes for a machine behind the concurrency group (seven did on 10-26), so a run's working tree can be older than the previous pass's
commit. The logs of runs 40340139520 and 40357285384 show it (a third, 40362102978, found by LAPTOP BOT 2026-11-05, is below): they fetched +965a77ee... (the 14:30:09Z tip) and +2ee29a5f... (the
16:15:05Z tip), their jobs started at 14:38 and 16:24, the previous pass committed status.json in between (14:37:59, 16:23:52), and the
heartbeat push failed four times with `CONFLICT (content): Merge conflict in status.json` and was discarded. The mechanism needs a
predecessor commit AFTER the run was created, NOT a long wait. It selects THREE of the 25 in-session runs of that day: the two above and
40362102978 (created 16:45:06Z, job started 16:45:59Z, a 53 second wait; the previous pass committed at 16:45:48Z, 42 seconds after the
creation). My first count said two, from a filter on delayed runs that hid the third; LAPTOP BOT caught it. The day's 22 status.json ts values
lack exactly those three start minutes (10:38, 12:24, 12:45), and 25 - 22 = 3. Three pre-market runs also left no commit (09:00, 09:15, 09:30
ET), presumably by design before the session opens; not investigated. A pass that traded would have started from a stale holds.json and trade_log.jsonl, which are the stop levels.

Run:  python -m unittest discover -s tests -v      (needs PyYAML)
"""
import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "alpaca-bot.yml")


def checkout_steps(doc):
    return [s for job in doc["jobs"].values() for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]


def checks_out_the_current_tip(step):
    """True if the step names a ref that is fetched when the job runs. No `ref` means the creation-time SHA."""
    ref = str((step.get("with") or {}).get("ref") or "").strip()
    return bool(ref) and "github.sha" not in ref


class ArmACheckout(unittest.TestCase):
    def doc(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_the_arm_a_workflow_checks_out_the_branch_tip_when_the_job_starts(self):
        steps = checkout_steps(self.doc())
        self.assertEqual(len(steps), 1, "expected exactly one checkout step")
        self.assertTrue(checks_out_the_current_tip(steps[0]), steps[0])
        self.assertEqual(steps[0]["with"]["ref"], "${{ github.ref_name }}")        # the dispatched branch, whatever it is

    def test_control_the_detector_can_say_no(self):
        """The default (no ref), an empty ref and a pinned SHA are exactly the stale shapes."""
        self.assertFalse(checks_out_the_current_tip({"uses": "actions/checkout@v5"}))
        self.assertFalse(checks_out_the_current_tip({"uses": "actions/checkout@v5", "with": {"ref": ""}}))
        self.assertFalse(checks_out_the_current_tip({"uses": "actions/checkout@v5", "with": {"ref": "${{ github.sha }}"}}))
        self.assertTrue(checks_out_the_current_tip({"uses": "actions/checkout@v5", "with": {"ref": "main"}}))

    def test_the_run_is_still_serialised_and_still_commits_back(self):
        """The fix only makes sense next to these two facts; if either changes, the reasoning above must be redone."""
        doc = self.doc()
        self.assertEqual(doc["concurrency"], {"group": "alpaca-bot", "cancel-in-progress": False})
        self.assertEqual(doc["jobs"]["trade"]["permissions"]["contents"], "write")


if __name__ == "__main__":
    unittest.main()
