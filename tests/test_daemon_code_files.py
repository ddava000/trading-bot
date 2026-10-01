"""Every repo module the Robinhood daemon can import must be in its CODE_FILES, or be a KNOWN gap.

rh_daemon.py's CODE_FILES decides which pulled files make the daemon restart and which go through its
self-test gate before being trusted. Python caches imports, so a module the daemon imports lazily and
that is NOT listed keeps running its OLD version after a pull, and a bad push to it reaches the live
process with no gate. Found 2026-10-01 (laptop, 00:05 ET entry): realized.py was imported by the
daemon yesterday and left off the list, so the daemon ran a stale copy of the cloud's module for a
day. The proposed rule: when either session makes a module a daemon import, it joins CODE_FILES in
the SAME commit. This test is that rule, applied to the whole import chain.

KNOWN_UNGATED is the honest list of today's exceptions, each with its reason. The test fails if a NEW
module appears in the chain without being listed, and ALSO if a module is listed here but has since
been added to CODE_FILES (so the list cannot rot). The daemon is read from source and never imported.

Run:  python -m unittest discover -s tests -v
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY_POINTS = ("rh_daemon", "rh_bot")

# module -> why it is not gated today. Cloud-owned modules the daemon reaches lazily.
KNOWN_UNGATED = {
    "slack_notify": "imported lazily inside rh_daemon.notify() for the Slack mirror of alerts; cached after "
                    "the first call, so the running daemon keeps the version it first loaded. Cloud changes "
                    "it often. Decision for the laptop: add it (a restart per push) or accept the gap.",
    "mail_check": "reached only through slack_notify._who_checks (the Slack mailbox mirror), which the daemon "
                  "never calls; it matters only if slack_notify is gated.",
}


def local_modules():
    return {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}


def local_imports(module, local):
    """Repo modules imported ANYWHERE in `module` (top level or inside a function)."""
    with open(os.path.join(ROOT, module + ".py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names} & local
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found |= {node.module.split(".")[0]} & local
    return found


def reachable(entry_points=ENTRY_POINTS):
    local = local_modules()
    seen, stack = set(), list(entry_points)
    while stack:
        mod = stack.pop()
        if mod in seen or mod not in local:
            continue
        seen.add(mod)
        stack.extend(local_imports(mod, local))
    return seen


def code_files():
    with open(os.path.join(ROOT, "rh_daemon.py"), encoding="utf-8") as f:
        m = re.search(r"^CODE_FILES\s*=\s*\(([^)]*)\)", f.read(), re.M)
    assert m, "cannot find CODE_FILES in rh_daemon.py"
    return {x[:-3] for x in re.findall(r'"([^"]+\.py)"', m.group(1))}


class DaemonCodeFilesTests(unittest.TestCase):
    def test_the_import_walk_finds_what_we_know_the_daemon_imports(self):
        """Control: if the walk found nothing, 'no ungated module' would mean nothing."""
        r = reachable()
        for mod in ("rh_daemon", "rh_bot", "alpaca_bot", "realized", "slack_notify"):
            self.assertIn(mod, r)

    def test_the_entry_points_and_the_shared_engine_are_gated(self):
        cf = code_files()
        for mod in ("rh_daemon", "rh_bot", "alpaca_bot", "realized"):
            self.assertIn(mod, cf, mod)

    def test_every_module_the_daemon_can_import_is_gated_or_a_listed_gap(self):
        ungated = reachable() - code_files()
        new = sorted(ungated - set(KNOWN_UNGATED))
        self.assertEqual(new, [],
                         "the daemon can import %s but its CODE_FILES does not list it, so a change would "
                         "neither restart the daemon nor pass its self-test gate. Add it to CODE_FILES in "
                         "the same commit that made it an import (or, with a reason, to KNOWN_UNGATED)." % new)

    def test_the_known_gaps_list_does_not_rot(self):
        gated_now = sorted(m for m in KNOWN_UNGATED if m in code_files())
        self.assertEqual(gated_now, [], "now in CODE_FILES, remove from KNOWN_UNGATED: %s" % gated_now)
        gone = sorted(m for m in KNOWN_UNGATED if m not in reachable())
        self.assertEqual(gone, [], "no longer imported by the daemon, remove from KNOWN_UNGATED: %s" % gone)

    def test_the_check_can_say_no(self):
        """Positive control: a module that is reachable and not gated is exactly what it reports."""
        fake_gated = code_files() - {"realized"}
        self.assertIn("realized", reachable() - fake_gated)


if __name__ == "__main__":
    unittest.main()
