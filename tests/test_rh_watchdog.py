"""rh_watchdog.py: the cloud dead-man's switch for the Robinhood laptop, which had no tests.

Found by the 2026-09-30 cross-audit and pinned here:
  * GitHub's native */30 cron ran this only 1 to 3 times a day (one inside market hours), so the
    workflow now ALSO runs after every Arm A run (~26 a day). At that density the stale alert would
    mail on every run, so the new path alerts once per threshold (tests below).
  * An outage that runs past yesterday's close made every threshold long past, so the stateless
    window never matched again and the watchdog stayed silent all day. Durations now count the
    current session only.
  * An alert that reached no channel exited 0 and left a green run saying "sent via: NOTHING".

The scenarios build a real git history of rh_status.json snapshots (the duration check walks it),
fix the clock, and read what main() decided. Each guard is shown able to say no as well as yes.

Run:  python -m unittest discover -s tests -v      (needs git and PyYAML)
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import rh_watchdog as W   # noqa: E402  (sets harmless Alpaca key defaults itself)

bot = W.bot
NOW = datetime(2026, 9, 30, 14, 0)          # a Wednesday, EDT, 270 minutes after the open


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def snap(dt, degraded=None):
    return {"ts": stamp(dt), "degraded": degraded}


def fixed_datetime(naive_et):
    base = naive_et.replace(tzinfo=bot.ET_TZ)

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return base.astimezone(tz) if tz else base
    return FixedDT


class Repo:
    """A throwaway git repo whose history is a list of rh_status.json snapshots, oldest first."""

    def __init__(self, snaps):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = self.tmp.name
        self._git("init", "-q", "-b", "main")
        if snaps:
            stream = b""
            for i, s in enumerate(snaps):
                body = json.dumps(s).encode()
                msg = b"s%d" % i
                stream += (b"commit refs/heads/main\ncommitter t <t@t> %d +0000\ndata %d\n%s\n"
                           b"M 100644 inline rh_status.json\ndata %d\n%s\n"
                           % (1700000000 + i * 60, len(msg), msg, len(body), body))
            subprocess.run(["git", "fast-import", "--quiet"], cwd=self.dir, input=stream,
                           check=True, capture_output=True)
            self._git("reset", "-q", "--hard", "main")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True)

    def close(self):
        self.tmp.cleanup()


def degraded_history(now, minutes, every=10, healthy_before=True):
    """Healthy snapshot just before the outage, then degraded ones every `every` min up to ~now."""
    start = now - timedelta(minutes=minutes)
    snaps = [snap(start - timedelta(minutes=every))] if healthy_before else []
    t = start
    while t <= now:
        snaps.append(snap(t, "broker_unreachable"))
        t += timedelta(minutes=every)
    return snaps


def run_main(snaps, now=NOW, market_open=True, delivered=True, force=False, **consts):
    """Run W.main() against a fixed clock and a real snapshot history. Returns (rc, alerts, out)."""
    repo = Repo(snaps)
    sent = []

    def fake_alert(msg, urgent=False):
        sent.append((msg, urgent))
        return delivered

    out, cwd = io.StringIO(), os.getcwd()
    os.chdir(repo.dir)
    try:
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(W, "datetime", fixed_datetime(now)))
            st.enter_context(mock.patch.object(bot, "check_market", lambda: (market_open, "")))
            st.enter_context(mock.patch.object(W, "alert", fake_alert))
            st.enter_context(mock.patch.dict(os.environ, {"FORCE_ALERT": "true" if force else ""}))
            for k, v in consts.items():
                st.enter_context(mock.patch.object(W, k, v))
            st.enter_context(contextlib.redirect_stdout(out))
            rc = W.main()
    finally:
        os.chdir(cwd)
        repo.close()
    return rc, sent, out.getvalue()


class BasicBehaviourTests(unittest.TestCase):
    def test_a_closed_market_is_silent(self):
        rc, sent, out = run_main([snap(NOW - timedelta(minutes=500))], market_open=False)
        self.assertEqual((rc, sent), (0, []))
        self.assertIn("market closed", out)

    def test_the_grace_period_after_the_open_is_silent(self):
        now = datetime(2026, 9, 30, 9, 33)
        rc, sent, out = run_main([snap(datetime(2026, 9, 29, 15, 55))], now=now)
        self.assertEqual((rc, sent), (0, []))
        self.assertIn("grace", out)

    def test_a_fresh_healthy_heartbeat_is_silent(self):
        rc, sent, out = run_main([snap(NOW - timedelta(minutes=14))])
        self.assertEqual((rc, sent), (0, []))
        self.assertIn("bot healthy", out)

    def test_a_stale_heartbeat_alerts_once_and_is_not_urgent(self):
        rc, sent, _ = run_main([snap(NOW - timedelta(minutes=50))])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][1])
        self.assertIn("stopped reporting", sent[0][0])

    def test_a_missing_status_file_alerts(self):
        rc, sent, _ = run_main([])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("could not read", sent[0][0])

    def test_force_sends_an_urgent_test_alert(self):
        rc, sent, _ = run_main([snap(NOW)], force=True)
        self.assertEqual(rc, 0)
        self.assertTrue(sent[0][1])
        self.assertIn("TEST alert", sent[0][0])


class DegradedThresholdTests(unittest.TestCase):
    """The stateless crossing design: each of 60/180/360 fires on the run landing within the window
    after it. These pin the arithmetic at both edges, for the original 45 minute window."""

    def alerts_at(self, minutes, **consts):
        rc, sent, out = run_main(degraded_history(NOW, minutes), **consts)
        self.assertEqual(rc, 0)
        return sent, out

    def test_just_after_a_threshold_it_alerts_with_the_measured_minutes(self):
        sent, _ = self.alerts_at(70)
        self.assertEqual(len(sent), 1)
        self.assertIn("about 70 minutes", sent[0][0])
        self.assertFalse(sent[0][1])                     # index-only: never urgent

    def test_between_thresholds_it_is_silent_and_says_why(self):
        sent, out = self.alerts_at(130)
        self.assertEqual(sent, [])
        self.assertIn("no threshold crossed", out)

    def test_the_second_and_window_edges(self):
        self.assertEqual(len(self.alerts_at(190)[0]), 1)
        self.assertEqual(len(self.alerts_at(224)[0]), 1)      # 44 minutes after 180: inside 45
        self.assertEqual(self.alerts_at(226)[0], [])          # 46 after: outside

    def test_below_the_first_threshold_is_silent(self):
        self.assertEqual(self.alerts_at(40)[0], [])

    def test_a_history_it_cannot_measure_alerts_instead_of_assuming_zero(self):
        rc, sent, _ = run_main([snap(NOW, "broker_unreachable")])        # one commit: shallow/no history
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("could not measure", sent[0][0])

    def test_degraded_and_also_stale_is_a_dead_laptop_not_a_waiting_one(self):
        snaps = [snap(NOW - timedelta(minutes=80)), snap(NOW - timedelta(minutes=70), "broker_unreachable"),
                 snap(NOW - timedelta(minutes=45), "broker_unreachable")]
        rc, sent, _ = run_main(snaps)
        self.assertEqual(len(sent), 1)
        self.assertIn("stopped reporting", sent[0][0])
        self.assertIn("DEGRADED", sent[0][0])


class OvernightCarryOverTests(unittest.TestCase):
    """An outage that ran past yesterday's close and is still going."""

    def history(self, now):
        y = datetime(2026, 9, 29, 15, 0)
        snaps = [snap(y)]                                                  # healthy, ends the walk
        snaps += [snap(datetime(2026, 9, 29, 15, 50), "broker_unreachable"),
                  snap(datetime(2026, 9, 29, 15, 55), "broker_unreachable")]
        t = datetime(2026, 9, 30, 9, 30)
        while t <= now:
            snaps.append(snap(t, "broker_unreachable"))
            t += timedelta(minutes=5)
        return snaps

    def test_the_setup_really_is_a_carry_over(self):
        """Control: the wall-clock duration is many hours, so the old logic would be past every
        threshold and silent. If this stops being true the next test proves nothing."""
        now = datetime(2026, 9, 30, 10, 40)
        repo = Repo(self.history(now))
        cwd = os.getcwd()
        os.chdir(repo.dir)
        try:
            with mock.patch.object(W, "datetime", fixed_datetime(now)):
                raw = W.degraded_minutes(now.replace(tzinfo=bot.ET_TZ))
        finally:
            os.chdir(cwd)
            repo.close()
        self.assertGreater(raw, 1000)
        self.assertTrue(all(raw - t >= W.CROSS_WINDOW_MIN for t in W.DEGRADED_ALERT_MIN))

    def test_it_alerts_on_the_minutes_of_this_session(self):
        now = datetime(2026, 9, 30, 10, 40)                                # 70 minutes after the open
        rc, sent, _ = run_main(self.history(now), now=now)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("about 70 minutes", sent[0][0])

    def test_and_stays_quiet_between_thresholds_of_the_new_day(self):
        now = datetime(2026, 9, 30, 11, 50)                                # 140 minutes in
        self.assertEqual(run_main(self.history(now), now=now)[1], [])


class DenseTriggerTests(unittest.TestCase):
    """The workflow_run path: ~26 runs a day, so the stale alert fires once per threshold."""
    DENSE = dict(STALE_ALERT_MIN=(30, 120, 360), CROSS_WINDOW_MIN=20, GRACE_MIN=30)

    def stale_alerts(self, minutes, now=NOW, **extra):
        consts = dict(self.DENSE, **extra)
        return run_main([snap(now - timedelta(minutes=minutes))], now=now, **consts)[1]

    def test_it_alerts_when_the_heartbeat_first_goes_stale(self):
        self.assertEqual(len(self.stale_alerts(35)), 1)

    def test_it_does_not_repeat_on_every_run(self):
        """The crying-wolf guard: at 15 minute density a still-silent laptop must not mail again."""
        self.assertEqual(self.stale_alerts(55), [])
        self.assertEqual(self.stale_alerts(90), [])

    def test_it_reminds_at_the_next_threshold_and_not_between(self):
        self.assertEqual(len(self.stale_alerts(125)), 1)
        self.assertEqual(self.stale_alerts(150), [])

    def test_the_legacy_path_still_pings_every_run(self):
        """The sparse native schedule keeps its original behaviour."""
        rc, sent, _ = run_main([snap(NOW - timedelta(minutes=60))])
        self.assertEqual(len(sent), 1)

    def test_a_heartbeat_missing_since_yesterday_counts_from_the_open(self):
        y = snap(datetime(2026, 9, 29, 15, 55))
        first = datetime(2026, 9, 30, 10, 5)                               # 35 minutes after the open
        self.assertEqual(len(run_main([y], now=first, **self.DENSE)[1]), 1)
        later = datetime(2026, 9, 30, 10, 50)                              # 80 minutes after
        self.assertEqual(run_main([y], now=later, **self.DENSE)[1], [])

    def test_the_longer_grace_holds_the_first_check_until_about_ten(self):
        now = datetime(2026, 9, 30, 9, 50)                                 # 20 minutes after the open
        rc, sent, out = run_main([snap(datetime(2026, 9, 29, 15, 55))], now=now, **self.DENSE)
        self.assertEqual(sent, [])
        self.assertIn("grace", out)
        rc, sent, _ = run_main([snap(datetime(2026, 9, 29, 15, 55))], now=now)     # legacy 5 min grace
        self.assertEqual(len(sent), 1)

    def test_degraded_uses_the_narrow_window(self):
        sent = run_main(degraded_history(NOW, 70), **self.DENSE)[1]
        self.assertEqual(len(sent), 1)
        self.assertEqual(run_main(degraded_history(NOW, 85), **self.DENSE)[1], [])     # 25 > 20 after 60

    def test_every_threshold_fires_at_least_once_and_at_most_twice_at_either_density(self):
        """The sampling-density facts that drove the window sizes, by brute force over every phase
        of the run grid. A window narrower than the run spacing could MISS an alert; wider than twice
        the spacing would triple it."""
        for spacing, window in ((30, 45), (15, 20)):
            for threshold in (60, 180, 360):
                for phase in range(spacing):
                    hits = sum(1 for k in range(0, 40)
                               if W.crossed(phase + k * spacing, [threshold], window))
                    self.assertIn(hits, (1, 2), (spacing, window, threshold, phase))

    def test_the_crossing_helper_can_say_no(self):
        self.assertEqual(W.crossed(59, [60], 45), [])
        self.assertEqual(W.crossed(60, [60], 45), [60])
        self.assertEqual(W.crossed(104, [60], 45), [60])
        self.assertEqual(W.crossed(105, [60], 45), [])


class DeliveryTests(unittest.TestCase):
    def test_an_alert_that_reached_no_channel_turns_the_run_red(self):
        rc, sent, _ = run_main([snap(NOW - timedelta(minutes=50))], delivered=False)
        self.assertEqual(len(sent), 1)
        self.assertEqual(rc, 1)

    def test_a_delivered_alert_is_green(self):
        self.assertEqual(run_main([snap(NOW - timedelta(minutes=50))], delivered=True)[0], 0)

    def test_the_force_test_button_is_honest_about_delivery(self):
        self.assertEqual(run_main([snap(NOW)], force=True, delivered=False)[0], 1)

    def test_real_alert_reports_nothing_sent_when_no_channel_is_configured(self):
        keys = ("GMAIL_APP_PASSWORD", "GMAIL_USER", "ALERT_EMAIL", "ALERT_TO", "SMS_TO",
                "NTFY_TOPIC", "SLACK_WEBHOOK_URL")
        env = {k: v for k, v in os.environ.items() if k not in keys}
        import slack_notify
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(slack_notify, "post", lambda *a, **k: False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(W.alert("x"))
            self.assertEqual(W.notify("x"), 1)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(slack_notify, "post", lambda *a, **k: True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(W.alert("x"))
            self.assertEqual(W.notify("x"), 0)


class EnvHelperTests(unittest.TestCase):
    def test_int_and_tuple_helpers_fall_back_on_junk_and_blank(self):
        with mock.patch.dict(os.environ, {"X_I": "20", "X_B": "", "X_J": "abc", "X_T": "30, 120,360",
                                          "X_TJ": "30,abc"}):
            self.assertEqual(W._env_int("X_I", 5), 20)
            self.assertEqual(W._env_int("X_B", 5), 5)
            self.assertEqual(W._env_int("X_J", 5), 5)
            self.assertEqual(W._env_int("X_MISSING", 5), 5)
            self.assertEqual(W._env_tuple("X_T", ()), (30, 120, 360))
            self.assertEqual(W._env_tuple("X_TJ", (9,)), (9,))
            self.assertEqual(W._env_tuple("X_MISSING", ()), ())

    def test_the_defaults_are_the_original_behaviour(self):
        self.assertEqual(W.CROSS_WINDOW_MIN, 45)
        self.assertEqual(W.GRACE_MIN, 5)
        self.assertEqual(W.STALE_ALERT_MIN, ())


class WorkflowWiringTests(unittest.TestCase):
    def load(self, name):
        with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_the_watchdog_follows_the_workflow_that_actually_exists_by_that_exact_name(self):
        """A rename of the Arm A workflow would silently orphan this trigger and put the watchdog
        back to one run a day with nothing failing."""
        wd, arm_a = self.load("rh-watchdog.yml"), self.load("alpaca-bot.yml")
        on = wd.get(True) or wd.get("on")
        self.assertEqual(on["workflow_run"]["workflows"], [arm_a["name"]])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        self.assertIn("schedule", on)                       # the independent second path stays

    def test_only_the_workflow_run_path_passes_overrides(self):
        wd = self.load("rh-watchdog.yml")
        env = next(s for s in wd["jobs"]["watchdog"]["steps"] if s.get("name", "").startswith("Check laptop"))["env"]
        for key in ("WATCHDOG_GRACE_MIN", "WATCHDOG_CROSS_WINDOW_MIN", "WATCHDOG_STALE_ALERT_MIN"):
            self.assertIn("github.event_name == 'workflow_run'", env[key])
            self.assertTrue(env[key].rstrip().endswith("|| '' }}"))       # empty on every other trigger

    def test_the_checkout_is_deep_enough_for_the_duration_walk(self):
        wd = self.load("rh-watchdog.yml")
        co = next(s for s in wd["jobs"]["watchdog"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
        self.assertGreaterEqual(co["with"]["fetch-depth"], 400)


if __name__ == "__main__":
    unittest.main()
