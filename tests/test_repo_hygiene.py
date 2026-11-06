"""Repo hygiene: the checks that would have caught this repo's own silent failures.

  * weekly-audit.yml was an INVALID workflow file from 2026-06-12 to 2026-08-26 and never ran
    once, while a local task quietly produced the audit everyone credited to it.
  * Two personal email addresses lingered in mailbox prose in a PUBLIC repo after the code
    literals were removed.
  * A printable non-ASCII character in a success print turned a DELIVERED email into a
    reported failure on a Windows console that no GitHub runner can reproduce.

Every check that hunts for something also proves it CAN find it (a positive control), because a
scan that has never been seen to report "present" says nothing when it reports "absent". The
controls are built at runtime so this file never contains the strings it looks for.

Run:  python -m unittest discover -s tests -v      (needs PyYAML; uses git and bash if present)
"""
import collections
import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout")
    return [f for f in out.stdout.split("\n") if f]


class _NoDupLoader(yaml.SafeLoader):
    pass


def _no_dup_mapping(loader, node, deep=False):
    keys = [loader.construct_object(k) for k, _ in node.value]
    dups = [k for k, c in collections.Counter(keys).items() if c > 1]
    if dups:
        raise ValueError("duplicate keys %s" % dups)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_NoDupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)

WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))


class WorkflowTests(unittest.TestCase):
    def test_there_are_workflows_to_check(self):
        self.assertGreaterEqual(len(WORKFLOWS), 5)      # a glob that matches nothing would pass everything

    def test_every_workflow_parses_with_no_duplicate_keys(self):
        """PyYAML silently keeps the LAST of two duplicate keys, so a doubled env entry would
        override the first without any error."""
        for path in WORKFLOWS:
            with self.subTest(workflow=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    doc = yaml.load(f, _NoDupLoader)
                self.assertIsInstance(doc, dict)
                self.assertTrue(doc.get("jobs"), "no jobs")
                self.assertTrue(doc.get(True) or doc.get("on"), "no trigger")   # PyYAML reads `on:` as True

    def test_the_duplicate_key_detector_can_actually_fail(self):
        with self.assertRaises(ValueError):
            yaml.load("a: 1\na: 2\n", _NoDupLoader)

    def test_a_plain_scalar_with_a_colon_is_rejected_by_the_parser(self):
        """The exact defect that kept weekly-audit.yml from ever running."""
        bad = 'run: claude -p "Reply with exactly: CLOUD AUDIT OK"\n'
        with self.assertRaises(yaml.YAMLError):
            yaml.safe_load("steps:\n  - " + bad)

    def test_every_embedded_shell_script_is_valid_bash(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        checked = 0
        for path in WORKFLOWS:
            with open(path, encoding="utf-8") as f:
                doc = yaml.load(f, _NoDupLoader)
            for jname, job in (doc.get("jobs") or {}).items():
                for i, step in enumerate(job.get("steps") or []):
                    script = step.get("run")
                    if not script:
                        continue
                    script = re.sub(r"\$\{\{.*?\}\}", "X", script)      # Actions expressions are not bash
                    with self.subTest(workflow=os.path.basename(path), job=jname, step=i):
                        r = subprocess.run([bash, "-n"], input=script.encode("utf-8"), capture_output=True)
                        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[:300])
                        checked += 1
        self.assertGreater(checked, 5)

    def test_the_bash_check_can_actually_fail(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not available")
        r = subprocess.run([bash, "-n"], input=b'if [ -n "x" ]; then echo hi\n', capture_output=True)
        self.assertNotEqual(r.returncode, 0)


class PythonTests(unittest.TestCase):
    def test_every_python_file_compiles(self):
        files = [f for f in tracked_files() if f.endswith(".py")]
        self.assertGreater(len(files), 10)
        for f in files:
            with self.subTest(file=f):
                with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                    compile(fh.read(), f, "exec")

    def test_shared_modules_have_no_printable_non_ascii(self):
        """Shared modules are imported on the Windows laptop, where a cp1252 console raises on
        non-ASCII the moment it reaches a print. A GitHub runner can never reproduce it."""
        import check_ascii
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = check_ascii.main()
        self.assertEqual(rc, 0, buf.getvalue())

    def test_the_ascii_check_can_actually_fail(self):
        import check_ascii
        with open(os.path.join(ROOT, "_ascii_probe.py"), "w", encoding="utf-8") as f:
            f.write('print("arrow ' + chr(0x2192) + ' here")\n')
        try:
            with mock_shared(check_ascii, ["_ascii_probe.py"]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = check_ascii.main()
            self.assertEqual(rc, 1)
        finally:
            os.remove(os.path.join(ROOT, "_ascii_probe.py"))


@contextlib.contextmanager
def mock_shared(mod, files):
    saved = mod.SHARED
    mod.SHARED = files
    try:
        yield
    finally:
        mod.SHARED = saved


# Patterns are assembled from fragments so this file never contains a match for itself.
_AT = "@"
PATTERNS = {
    "slack bot token": r"xoxb-[0-9A-Za-z-]{10,}",
    "slack webhook": r"hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}",
    "alpaca key": r"\b(?:PK|AK)[A-Z0-9]{18,}\b",
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "github token": r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    "personal email": r"[\w.+-]+" + _AT + r"(?:gmail|yahoo|outlook|hotmail|icloud|proton|aol)\.(?:com|me)\b",
}
PLANTS = {
    "slack bot token": "xoxb" + "-" + "1234567890" + "-abcdefghij",
    "slack webhook": "hooks.slack" + ".com/services/" + "T0ABCDEF12" + "/" + "B0ABCDEF12" + "/xyz",
    "alpaca key": "PK" + "ABCDEFGHIJKLMNOPQRST",
    "anthropic key": "sk-ant" + "-" + "abcdefghijklmnopqrstuvwxyz",
    "github token": "gh" + "p_" + "a" * 36,
    "personal email": "someone" + _AT + "gmail" + ".com",
}


class SecretScanTests(unittest.TestCase):
    def test_each_pattern_catches_its_own_planted_example(self):
        """Without this, a zero-hit scan could mean 'clean' or 'the regex is broken'."""
        for name, pat in PATTERNS.items():
            with self.subTest(pattern=name):
                self.assertTrue(re.search(pat, "prefix " + PLANTS[name] + " suffix"), name)

    def test_no_credentials_or_personal_addresses_in_tracked_files(self):
        """The repo is PUBLIC. Devon's addresses live in GitHub secrets, never in text."""
        hits = []
        scanned = 0
        for f in tracked_files():
            if f.endswith((".png", ".jpg", ".pdf", ".ico")):
                continue
            try:
                with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for name, pat in PATTERNS.items():
                for m in re.finditer(pat, text):
                    hits.append("%s: %s (%s...)" % (f, name, m.group(0)[:4]))
        self.assertGreater(scanned, 20)
        self.assertEqual(hits, [], "secret or personal address in a PUBLIC repo:\n" + "\n".join(hits[:10]))


def stray_controls(text):
    """Control characters other than tab, LF and CR. A path like %LOCALAPPDATA%\\Python\\bin,
    written through a string that treats the backslash-b as an escape, silently becomes a
    BACKSPACE character and the text reads 'Pythonin' with nothing visibly wrong."""
    return [(i, hex(ord(c))) for i, c in enumerate(text) if ord(c) < 32 and c not in "\t\n\r"]


class ControlCharacterTests(unittest.TestCase):
    def test_the_detector_can_actually_find_one(self):
        self.assertEqual(stray_controls("Python" + chr(8) + "in"), [(6, "0x8")])
        self.assertEqual(stray_controls("plain text\nwith\ttabs\r\n"), [])

    def test_no_stray_control_characters_in_tracked_text(self):
        """Found the hard way: a Windows path in STANDING FACTS lost a backslash-b to an
        escape sequence while a durable document was being edited by script."""
        found = []
        # journal.md and the *.jsonl logs are WRITTEN BY THE BOTS from model output, and one
        # 2026-07-03 journal line carries a stray backspace where the model emitted a backslash
        # before "both". That is harmless machine-written history, not something a person or a
        # script edited by hand, so rewriting it would falsify the record for no benefit. This
        # check exists for the docs and code that people and scripts DO edit.
        machine_written = {"journal.md"}
        for f in tracked_files():
            if f in machine_written or not f.endswith((".md", ".py", ".yml", ".json", ".txt", ".ps1")):
                continue
            try:
                with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            hits = stray_controls(text)
            if hits:
                found.append("%s: %s" % (f, hits[:3]))
        self.assertEqual(found, [], "\n".join(found))


class DataFileTests(unittest.TestCase):
    def test_committed_json_state_files_are_valid(self):
        for name in ("experiment.json", "status.json", "rh_status.json", "rh_deposits.json",
                     "realized_a.json", "realized_b.json", "baseline.json", "holds.json"):
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                continue
            with self.subTest(file=name):
                with open(path, encoding="utf-8") as f:
                    json.load(f)

    def test_experiment_json_carries_what_a_cold_reader_needs(self):
        with open(os.path.join(ROOT, "experiment.json"), encoding="utf-8") as f:
            d = json.load(f)
        for key in ("rule", "arm_A", "arm_B", "decide_no_earlier_than", "question_being_answered"):
            self.assertIn(key, d)
        self.assertIn("in-window", d["rule"].lower())        # the rule that once pointed at the wrong window

    def test_realized_ledgers_carry_the_disclaimer(self):
        for name in ("realized_a.json", "realized_b.json"):
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            with self.subTest(file=name):
                self.assertIn("NOT a tax document", d.get("note", ""))
                self.assertIn(d["summary"]["state"], ("ok", "unverified", "stale", "unknown"))


if __name__ == "__main__":
    unittest.main()
