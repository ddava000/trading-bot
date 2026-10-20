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
