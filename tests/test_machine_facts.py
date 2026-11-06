"""The three laptop-local facts no other watcher can see.

Devon retired both daily-check sessions on 2026-10-17 ("four sessions is too much") and a
GitHub daily digest replaced them. Most of what the laptop check reported was already
covered - login expiry, a failing selftest, broker trouble, a downtime gap, an Arm A
silence all email from the daemon, and staleness is the watchdog's job. Three things were
covered by NOTHING, verified by grep before any of this was written:

  1. WHY the machine went down. Every mention of event 41, bugcheck or ConnectedStandby in
     rh_daemon.py was a comment; rh_watchdog.py and daily_digest.py have none. The HMB
     experiment's verdict is "does it still crash with the host memory buffer off", and
     ConnectedStandbyInProgress is how the old Modern Standby cause (true on 08-04, 09-02,
     09-21) is told from a new one (false on both 10-08 events).
  2. keep_awake() failing. It only ever logged. Modern Standby is where this laptop died
     three times out of three, and 09-21 hung there and stayed dead twelve hours.
  3. The two settings the hypothesis turns on. A Windows update re-enabling HMB would
     invalidate the experiment silently, and the next crash would be read against a
     hypothesis that was no longer being tested.

These now live in the daemon, which runs on the only machine that can see them, and are
published in rh_status.json["machine"] for the digest. The tests below pin the parsing, the
dampener and the publication - and, deliberately, that an unreadable source comes out as
None so a reader treats it as a flag rather than a pass.

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rh_daemon as D

EVENT_41 = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>
<System><Provider Name='Microsoft-Windows-Kernel-Power'/><EventID>41</EventID>
<TimeCreated SystemTime='2026-10-08T15:32:19.3775310Z'/></System>
<EventData>
<Data Name='BugcheckCode'>%s</Data>
<Data Name='SleepInProgress'>%s</Data>
<Data Name='ConnectedStandbyInProgress'>%s</Data>
</EventData></Event>"""


def fake_wevtutil(bugcheck="0", sleep="6", standby="false", rc=0, out=None):
    class R:
        returncode = rc
        stdout = EVENT_41 % (bugcheck, sleep, standby) if out is None else out
        stderr = ""
    return lambda *a, **k: R()


class ReadingTheLastUncleanShutdown(unittest.TestCase):
    def test_a_bugcheck_is_parsed_with_its_hex(self):
        """0x154 UNEXPECTED_STORE_EXCEPTION, the 2026-10-08 04:54 ET crash."""
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(bugcheck="340", standby="false")):
            got = D.last_unclean_shutdown()
        self.assertEqual(got["bugcheck"], 340)
        self.assertEqual(got["bugcheck_hex"], "0x154")
        self.assertIs(got["connected_standby"], False)
        self.assertEqual(got["logged_at"], "2026-10-08T15:32:19")

    def test_connected_standby_true_is_the_old_cause(self):
        """08-04, 09-02 and 09-21 all had this true; both 10-08 events had it false.
        The whole value of the field is telling those two populations apart."""
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(bugcheck="30", standby="true")):
            got = D.last_unclean_shutdown()
        self.assertIs(got["connected_standby"], True)
        self.assertEqual(got["bugcheck_hex"], "0x1e")

    def test_no_bugcheck_code_is_reported_as_zero_not_as_missing(self):
        """A hang or a power loss. 'No code' is an ANSWER and must not read as unknown."""
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(bugcheck="0", sleep="6")):
            got = D.last_unclean_shutdown()
        self.assertEqual((got["bugcheck"], got["sleep_in_progress"]), (0, 6))

    def test_a_failed_query_is_None_so_a_reader_sees_a_flag(self):
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(rc=1)):
            self.assertIsNone(D.last_unclean_shutdown())

    def test_an_empty_log_is_None_not_a_crash(self):
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(out="")):
            self.assertIsNone(D.last_unclean_shutdown())

    def test_malformed_xml_is_swallowed(self):
        """It runs at startup; it must never be the reason the daemon fails to start."""
        logged = []
        with mock.patch.object(D.subprocess, "run", fake_wevtutil(out="<not-xml")), \
             mock.patch.object(D, "log", logged.append):
            self.assertIsNone(D.last_unclean_shutdown())
        self.assertTrue(any("could not read" in m for m in logged), logged)

    def test_a_timeout_is_swallowed(self):
        def boom(*a, **k):
            raise D.subprocess.TimeoutExpired("wevtutil", 30)
        with mock.patch.object(D.subprocess, "run", boom), mock.patch.object(D, "log", lambda m: None):
            self.assertIsNone(D.last_unclean_shutdown())

    def test_it_asks_for_exactly_one_newest_event_41(self):
        """Pin the query: newest-first, count 1, XML. A wrong /rd would report an OLD crash."""
        seen = {}

        def capture(cmd, **k):
            seen["cmd"] = cmd
            return fake_wevtutil()(cmd, **k)
        with mock.patch.object(D.subprocess, "run", capture):
            D.last_unclean_shutdown()
        self.assertIn("/q:*[System[(EventID=41)]]", seen["cmd"])
        self.assertIn("/c:1", seen["cmd"])
        self.assertIn("/rd:true", seen["cmd"])
        self.assertNotIn("powershell", " ".join(seen["cmd"]).lower())

    @unittest.skipUnless(os.name == "nt", "Windows event log")
    def test_it_works_against_the_real_log(self):
        """Control: if the mocked tests passed and this returned nothing, they prove little."""
        got = D.last_unclean_shutdown()
        if got is None:
            self.skipTest("no Kernel-Power 41 on this machine")
        self.assertIn("bugcheck", got)
        self.assertIsInstance(got["connected_standby"], bool)


class MachineGuards(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "winreg")
    def test_it_reads_the_two_settings_the_hypothesis_turns_on(self):
        got = D.machine_guards()
        self.assertIn("hmb_policy", got)
        self.assertIn("fast_startup", got)

    # winreg exists only on Windows. These two patch it, so they must be SKIPPED
    # elsewhere, not merely expected to pass: a bare `import winreg` in a test body is
    # an ImportError on ubuntu-latest and turned CI red on 2026-10-20 (run #109, exit 1,
    # 18 s, commit 65d342f). The suite was 594 green on this laptop at the time, which is
    # exactly why a Windows-only dependency inside a test needs the guard on the test and
    # not on the machine that happens to run it.
    @unittest.skipUnless(os.name == "nt", "winreg is Windows-only")
    def test_an_absent_value_is_None_which_is_a_real_answer(self):
        """HMB unset means the default, which is ON - so None must not read as 'disabled'."""
        import winreg
        with mock.patch.object(winreg, "QueryValueEx", side_effect=FileNotFoundError):
            self.assertEqual(D.machine_guards(), {"hmb_policy": None, "fast_startup": None})

    @unittest.skipUnless(os.name == "nt", "winreg is Windows-only")
    def test_it_never_raises_when_the_registry_refuses(self):
        import winreg
        with mock.patch.object(winreg, "OpenKey", side_effect=OSError("denied")), \
             mock.patch.object(D, "log", lambda m: None):
            self.assertIsInstance(D.machine_guards(), dict)

    def test_it_returns_a_dict_on_every_platform(self):
        """Runs EVERYWHERE, including where winreg does not exist.

        That is the real contract persist() depends on: off Windows the import inside
        machine_guards() fails, the except catches it, and the caller still gets a dict
        it can publish. Without a test that runs on Linux, nothing covered that path -
        and the path is what CI executes.
        """
        with mock.patch.object(D, "log", lambda m: None):
            self.assertIsInstance(D.machine_guards(), dict)


class PublishedForTheDigest(unittest.TestCase):
    def test_persist_copies_the_block_into_the_status_file(self):
        saved = {}
        machine = {"keep_awake": True, "guards": {"hmb_policy": 0, "fast_startup": 1},
                   "last_unclean_shutdown": {"bugcheck": 340, "bugcheck_hex": "0x154",
                                             "connected_standby": False,
                                             "sleep_in_progress": 0,
                                             "logged_at": "2026-10-08T15:32:19"}}
        led = {"positions": [], "holds": {}, "orders_today": 0, "cash": 1.0}
        with mock.patch.object(D, "_MACHINE", machine), \
             mock.patch.object(D, "save_ledger", lambda l: None), \
             mock.patch.object(D, "log", lambda m: None), \
             mock.patch.object(D, "_push_status", lambda r: None), \
             mock.patch.object(D, "email_trades", lambda *a, **k: None), \
             mock.patch.object(D, "_load", lambda p, d: d), \
             mock.patch.object(D, "_save",
                               lambda p, o: saved.update(o) if p == D.STATUS_F else None):
            D.persist(led, {"snapshot": {"equity": 300.0}}, [])
        self.assertEqual(saved["machine"]["guards"]["hmb_policy"], 0)
        self.assertEqual(saved["machine"]["last_unclean_shutdown"]["bugcheck_hex"], "0x154")

    def test_an_empty_block_is_simply_absent_rather_than_a_false_all_clear(self):
        saved = {}
        led = {"positions": [], "holds": {}, "orders_today": 0, "cash": 1.0}
        with mock.patch.object(D, "_MACHINE", {}), \
             mock.patch.object(D, "save_ledger", lambda l: None), \
             mock.patch.object(D, "log", lambda m: None), \
             mock.patch.object(D, "_push_status", lambda r: None), \
             mock.patch.object(D, "email_trades", lambda *a, **k: None), \
             mock.patch.object(D, "_load", lambda p, d: d), \
             mock.patch.object(D, "_save",
                               lambda p, o: saved.update(o) if p == D.STATUS_F else None):
            D.persist(led, {"snapshot": {"equity": 300.0}}, [])
        self.assertNotIn("machine", saved)


class KeepAwakeAlert(unittest.TestCase):
    """It used to only log, and the retired daily check was the only reader."""

    def setUp(self):
        self.sent, self.led = [], {}
        for name, fn in (("notify", lambda s, b, untrusted=False: (self.sent.append(s), True)[1]),
                         ("log", lambda m: None),
                         ("_remember_alerts",
                          lambda l, **kw: l.setdefault("alerts", {}).update(kw))):
            p = mock.patch.object(D, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_a_held_hold_says_nothing(self):
        self.assertFalse(D.alert_keep_awake(self.led, True))
        self.assertEqual(self.sent, [])

    @unittest.skipUnless(os.name == "nt", "Windows-only condition")
    def test_a_failed_hold_alerts_and_names_the_twelve_hour_outage(self):
        D.alert_keep_awake(self.led, False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("KEEP-AWAKE IS NOT HELD", self.sent[0])

    @unittest.skipUnless(os.name == "nt", "Windows-only condition")
    def test_a_restart_loop_cannot_turn_it_into_an_alert_per_restart(self):
        """The dampener this file has got wrong five times, every time in a module global."""
        for _ in range(5):
            D.alert_keep_awake(self.led, False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("keep_awake_alert_day", self.led.get("alerts", {}))

    @unittest.skipUnless(os.name == "nt", "Windows-only condition")
    def test_it_re_alerts_the_next_day(self):
        D.alert_keep_awake(self.led, False)
        self.led["alerts"]["keep_awake_alert_day"] = "1999-01-01"
        D.alert_keep_awake(self.led, False)
        self.assertEqual(len(self.sent), 2)

    def test_it_never_raises(self):
        """Called at startup; it must not be the reason the daemon fails to start."""
        with mock.patch.object(D, "now_et", side_effect=RuntimeError("clock")):
            self.assertFalse(D.alert_keep_awake({}, False))


class TheDowntimeMailNamesTheCause(unittest.TestCase):
    """report_downtime's rebooted branch used to send Devon to Event Viewer."""

    def mail(self, machine):
        sent = []
        with mock.patch.object(D, "_MACHINE", machine), \
             mock.patch.object(D, "_load", lambda p, d: {"ts": "2026-10-08T11:20"}), \
             mock.patch.object(D, "market_minutes_since", lambda ts: 31), \
             mock.patch.object(D, "_implied_boot_epoch", lambda: time.time() - 300), \
             mock.patch.object(D, "now_et", D.now_et), \
             mock.patch.object(D, "save_ledger", lambda l: None), \
             mock.patch.object(D, "log", lambda m: None), \
             mock.patch.object(D, "_remember_alerts",
                               lambda l, **kw: l.setdefault("alerts", {}).update(kw)), \
             mock.patch.object(D, "notify",
                               lambda s, b, untrusted=False: (sent.append((s, b)), True)[1]):
            D.report_downtime({})
        return sent[0][1] if sent else ""

    def test_a_bugcheck_is_named_in_the_mail(self):
        body = self.mail({"last_unclean_shutdown": {"bugcheck": 340, "bugcheck_hex": "0x154",
                                                    "connected_standby": True,
                                                    "sleep_in_progress": 0,
                                                    "logged_at": "2026-10-08T15:32:19"}})
        self.assertIn("BUGCHECK 0x154", body)
        self.assertIn("connected standby True", body)

    def test_no_bugcheck_says_hang_or_power_and_points_at_1074_first(self):
        """2026-10-08's 42-minute gap was Devon's own memory test, and the first version
        of this mail sent him hunting a crash that did not exist."""
        body = self.mail({"last_unclean_shutdown": {"bugcheck": 0, "bugcheck_hex": "0x0",
                                                    "connected_standby": False,
                                                    "sleep_in_progress": 6,
                                                    "logged_at": "2026-10-08T15:32:19"}})
        self.assertIn("NO bugcheck code", body)
        self.assertIn("1074", body)

    def test_an_unreadable_log_says_UNKNOWN_not_nothing(self):
        body = self.mail({"last_unclean_shutdown": None})
        self.assertIn("UNKNOWN", body)
        self.assertIn("not a clean bill", body)


if __name__ == "__main__":
    unittest.main()


class TheAuthClassDoesNotBackOffLikeAQuotaOutage(unittest.TestCase):
    """2026-10-21: the backoff curve ran for 13 minutes after Devon had fixed the login.

    The curve protects the Claude quota - each broker snapshot is a real agent turn, and
    retrying every 60 s through a usage-limit outage burns the resource being waited on.
    An auth failure fails AT AUTHENTICATION, before any model call, so it costs nothing,
    and the fix is a human running `claude auth login`. Failures at 09:45 drove the curve
    to its 900 s cap by 10:00:39; the login was fixed about 10:02; the daemon learned at
    10:16:11. 31 blind market-minutes, about 13 of them this wait.
    """

    REAL_AUTH_ERROR = "Failed to authenticate: OAuth session expired and could not be refreshed"

    def test_the_real_error_string_from_the_incident_is_classified_as_needing_a_human(self):
        self.assertTrue(D.needs_human(self.REAL_AUTH_ERROR))
        self.assertFalse(D.self_heals(self.REAL_AUTH_ERROR))

    def test_a_usage_limit_is_not_the_auth_class(self):
        msg = "You've hit your session limit - resets 1:40pm (America/Chicago)"
        self.assertFalse(D.needs_human(msg))
        self.assertTrue(D.self_heals(msg))

    def test_the_auth_cap_is_far_below_the_quota_cap(self):
        self.assertLess(D.AUTH_BACKOFF_MAX, D.RECONCILE_BACKOFF_MAX)
        self.assertLessEqual(D.AUTH_BACKOFF_MAX, 120)

    def test_the_incident_curve_would_have_been_capped(self):
        """Replay the five failures: the quota curve reaches 900 s, the auth cap holds 120."""
        waits = []
        for fails in range(1, 6):
            quota = min(D.FAST_PASS_SEC * (2 ** min(fails - 1, 4)), D.RECONCILE_BACKOFF_MAX)
            waits.append(min(quota, D.AUTH_BACKOFF_MAX)
                         if D.needs_human(self.REAL_AUTH_ERROR) else quota)
        self.assertEqual(max(waits), D.AUTH_BACKOFF_MAX)
        self.assertLessEqual(max(waits), 120, "a fixed login must be noticed in ~2 min, not 15")

    def test_neither_classifier_fires_on_an_unknown_error(self):
        """An unknown cause must keep the quota curve: it might be anything."""
        self.assertFalse(D.needs_human("broker returned 503"))
        self.assertFalse(D.self_heals("broker returned 503"))

    def test_the_classification_has_ONE_definition(self):
        """It used to be inline in _maybe_alert_broker and absent from the backoff."""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "rh_daemon.py"), encoding="utf-8") as f:
            src = f.read()
        inline = src.count('"oauth" in')
        self.assertEqual(inline, 1, "the oauth test must exist in exactly one place, not %d" % inline)
        tree = ast.parse(src)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertIn("needs_human", names)
        self.assertIn("self_heals", names)

    def _main_ast(self):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "rh_daemon.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        return ast, next(n for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name == "main")

    def test_the_auth_cap_lives_WHERE_main_actually_gets_its_wait(self):
        """A mutation run caught the original of this: the test replayed the curve itself.

        test_the_incident_curve_would_have_been_capped computes the waits in the TEST, so
        deleting the cap from main() left it green - the same miss as the
        retry_pending_downtime call site a week earlier, and the same lesson: when the fix
        is "call X from Y", assert the CALL.

        Rewritten 2026-10-21 when the decision moved out of main() into retry_wait(). The
        first version asserted needs_human and AUTH_BACKOFF_MAX appeared inside main(),
        which was true of the inline code and false of the better structure - so it failed
        on a change that improved what it was protecting. The guarantee belongs on whichever
        function main() takes its wait FROM, not on main().
        """
        ast, main = self._main_ast()
        called = [c.func.id for c in ast.walk(main)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        self.assertIn("retry_wait", called, "main() must get its wait from retry_wait")

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "rh_daemon.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        rw = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "retry_wait")
        inner = [c.func.id for c in ast.walk(rw)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        self.assertIn("needs_human", inner, "retry_wait must classify the error")
        self.assertIn("self_heals", inner, "and must let quota evidence win")
        names = [n.id for n in ast.walk(rw) if isinstance(n, ast.Name)]
        self.assertIn("AUTH_BACKOFF_MAX", names)

    def test_alert_keep_awake_is_called_from_the_main_LOOP(self):
        """Once at startup is not enough: a send that failed needs a later pass."""
        ast, main = self._main_ast()
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.While))
        called = [c.func.id for stmt in loop.body for c in ast.walk(stmt)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        self.assertIn("alert_keep_awake", called)


class QuotaEvidenceBeatsTheAuthCap(unittest.TestCase):
    """FINDING (cloud, 2026-10-21), reproduced: the auth cap ran AFTER the quota override.

    e906b0c7 capped the retry wait at AUTH_BACKOFF_MAX whenever needs_human(err), and that
    line sits after `wait = reset_wait`. On a message matching BOTH classes the cap
    rewrote a parsed quota wait:
        "Failed to authenticate while checking quota: You've hit your session limit -
         resets 1:40pm (America/Chicago)"
        needs_human True, self_heals True, quota_reset_wait 14845 s -> capped to 120 s
    124 retries through a quota outage instead of one, which is the exact burn the curve
    exists to prevent. My fix would have made the worst case worse.

    Latent, not observed: the two real messages separate cleanly. But `auth` is a broad
    substring and the bridge's wording is not ours. The asymmetry settles it - reading a
    mixed message as QUOTA costs at most a quarter-hour of extra blindness on a login fix,
    reading it as AUTH costs hours of hammering the resource being waited for.

    These tests were written BEFORE the fix, call site first, because the two mutations
    that got away this morning were both wiring.
    """

    MIXED = ("Failed to authenticate while checking quota: You've hit your session limit "
             "- resets 1:40pm (America/Chicago)")
    AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"
    LIMIT = "You've hit your session limit - resets 1:40pm (America/Chicago)"

    def test_the_decision_is_ONE_function_that_main_calls(self):
        """Written first. The arithmetic must not live inline where only a test replays it."""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "rh_daemon.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertIn("retry_wait", names)
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        called = [c.func.id for c in ast.walk(main)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        self.assertIn("retry_wait", called, "main() must use it, not reimplement it")

    def test_a_mixed_message_keeps_the_quota_wait(self):
        got = D.retry_wait(5, self.MIXED, 14845)
        self.assertEqual(got, 14845, "quota evidence must win over the auth cap")

    def test_a_limit_message_whose_reset_does_not_parse_is_still_not_capped(self):
        """Why 'reset is None' alone is not enough as a guard."""
        got = D.retry_wait(5, "You've hit your session limit, try later", None)
        self.assertEqual(got, D.RECONCILE_BACKOFF_MAX)

    def test_a_pure_auth_failure_IS_capped(self):
        self.assertEqual(D.retry_wait(5, self.AUTH, None), D.AUTH_BACKOFF_MAX)
        self.assertEqual(D.retry_wait(1, self.AUTH, None), D.FAST_PASS_SEC)

    def test_a_plain_limit_message_uses_the_named_reset(self):
        self.assertEqual(D.retry_wait(5, self.LIMIT, 14845), 14845)

    def test_an_unknown_error_keeps_the_quota_curve(self):
        self.assertEqual(D.retry_wait(5, "broker returned 503", None), D.RECONCILE_BACKOFF_MAX)
        self.assertEqual(D.retry_wait(2, "broker returned 503", None), D.FAST_PASS_SEC * 2)

    def test_the_curve_still_doubles_and_caps_as_it_always_did(self):
        waits = [D.retry_wait(f, "broker returned 503", None) for f in range(1, 7)]
        self.assertEqual(waits, [60, 120, 240, 480, 900, 900])

    def test_auth_wording_WITH_a_named_reset_still_waits_for_the_reset(self):
        """Why `reset_wait is None` is load-bearing and not belt-and-braces.

        A mutation dropping it survived every other test here: the gap is a message with
        AUTH wording whose reset time parses but which self_heals does not match. If the
        bridge ever says that, a named reset is still hard evidence that waiting is the
        right move, and the cap must not override it.

        The reset is INJECTED rather than parsed from the string. My first version asserted
        quota_reset_wait() parsed it, which made the test depend on the wall clock: the
        parser rolls a time already past to tomorrow and then declines it as over six
        hours, so the test passed before 1:40pm Central and failed after. It failed on this
        laptop at 15:32 ET and would have failed on a UTC runner most of the day. retry_wait
        takes reset_wait as an argument, so the behaviour under test needs no parsing at all.
        """
        msg = "Failed to authenticate: token expired, resets 1:40pm (America/Chicago)"
        self.assertTrue(D.needs_human(msg))
        self.assertFalse(D.self_heals(msg), "fixture must not match the self-healing class")
        self.assertEqual(D.retry_wait(5, msg, 14845), 14845,
                         "a named reset outranks the auth cap")
