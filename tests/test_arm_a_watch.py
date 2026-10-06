"""arm_a_watch.py: the dead-man's switch for Arm A (Alpaca).

Devon 2026-10-06: build it. The reason: on 2026-10-05 GitHub's Actions incident left Arm A's 15:30 and 15:45
ET runs without a machine, so for about 28 minutes before the close no stop-loss could fire (the bot checks
its own stops inside each run) and nothing told anyone. Tests below pin the three signals, that each can say
no AND yes, that a normal day never alerts, that the real 10-05 timeline is caught inside market hours, that
an undelivered alert is a red run, and the wiring of both homes (the workflow and the laptop's exact mode).

Run:  python -m unittest discover -s tests -v      (needs git and PyYAML)
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arm_a_watch as A  # noqa: E402
import rh_watchdog as W  # noqa: E402

UTC = timezone.utc
# A Tuesday in EDT. 09:45 ET = 13:45Z, 15:55 ET = 19:55Z.
OPEN = datetime(2026, 10, 6, 13, 45, tzinfo=UTC)
CLOSE = datetime(2026, 10, 6, 19, 55, tzinfo=UTC)


def at(h, m, s=0, day=6):
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def run(created, status="completed", conclusion="success"):
    return {"status": status, "conclusion": conclusion, "created_at": created}


class SessionTests(unittest.TestCase):
    def test_a_weekday_session_is_0945_to_1555_et(self):
        self.assertEqual(A.session_bounds(at(15, 0)), (OPEN, CLOSE))

    def test_weekends_and_holidays_have_no_session(self):
        self.assertIsNone(A.session_bounds(at(15, 0, day=3)))        # Saturday
        self.assertIsNone(A.session_bounds(at(15, 0, day=4)))        # Sunday
        self.assertIsNone(A.session_bounds(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)))   # Thanksgiving

    def test_the_session_follows_the_clock_change(self):
        o, c = A.session_bounds(datetime(2026, 11, 2, 16, 0, tzinfo=UTC))       # EST after the fall back
        self.assertEqual((o.hour, o.minute, c.hour, c.minute), (14, 45, 20, 55))


class MeasureTests(unittest.TestCase):
    def test_each_signal_is_measured_in_minutes(self):
        now = at(15, 0)
        s = A.measure(now, OPEN, commit_utc=now - timedelta(minutes=9),
                      runs=[run(now - timedelta(minutes=6)), run(now - timedelta(minutes=21))])
        self.assertAlmostEqual(s["stale"], 9)
        self.assertAlmostEqual(s["no_trigger"], 6)
        self.assertEqual(s["queued"], 0.0)

    def test_the_longest_wait_among_queued_runs_is_used(self):
        now = at(15, 0)
        runs = [run(now - timedelta(minutes=4), "queued", None), run(now - timedelta(minutes=19), "queued", None),
                run(now - timedelta(minutes=30))]
        self.assertAlmostEqual(A.measure(now, OPEN, runs=runs)["queued"], 19)

    def test_unknown_is_none_never_zero(self):
        s = A.measure(at(15, 0), OPEN)
        self.assertEqual(s, {"queued": None, "no_trigger": None, "stale": None})
        self.assertIsNone(A.measure(at(15, 0), OPEN, commit_utc=at(14, 50))["queued"])

    def test_the_clock_starts_at_the_open_not_at_yesterdays_last_commit(self):
        now = at(13, 55)                                                   # ten minutes into the session
        yesterday = at(19, 40, day=5)
        s = A.measure(now, OPEN, commit_utc=yesterday, runs=[run(at(19, 30, day=5))])
        self.assertAlmostEqual(s["stale"], 10)
        self.assertAlmostEqual(s["no_trigger"], 10)

    def test_without_git_history_the_status_ts_plus_the_normal_lag_is_used(self):
        ts_et = (at(14, 40) - timedelta(minutes=A.COMMIT_LAG_MIN)).astimezone(A.bot.ET_TZ)
        status = {"ts": ts_et.strftime("%Y-%m-%dT%H:%M ET")}
        s = A.measure(at(15, 0), OPEN, status=status)
        self.assertAlmostEqual(s["stale"], 20, delta=1)

    def test_a_garbage_status_is_unknown(self):
        self.assertIsNone(A.measure(at(15, 0), OPEN, status={"ts": "yesterday"})["stale"])


class CrossingTests(unittest.TestCase):
    def sig(self, **kw):
        base = {"queued": 0.0, "no_trigger": 5.0, "stale": 10.0}
        base.update(kw)
        return base

    def names(self, cs):
        return [(c["signal"], c["threshold"], c["urgent"]) for c in cs]

    def test_nothing_crosses_inside_the_normal_range(self):
        self.assertEqual(A.crossings(self.sig()), [])

    def test_the_worst_normal_values_ever_measured_do_not_alert(self):
        """9 trading days, 237 gaps: worst commit gap 24.5 min (content age at commit ~11), triggers 15.3."""
        worst = {"queued": 0.0, "no_trigger": 15.5, "stale": 24.5 + 0.5}
        self.assertEqual(A.crossings(worst, window=10 ** 6), [])

    def test_each_threshold_is_exact_at_its_edge_and_first_is_not_urgent(self):
        self.assertEqual(self.names(A.crossings(self.sig(queued=5.9))), [])
        self.assertEqual(self.names(A.crossings(self.sig(queued=6.0))), [("queued", 6, False)])
        self.assertEqual(self.names(A.crossings(self.sig(queued=20.0), window=5)), [("queued", 20, True)])

    def test_the_second_threshold_is_urgent_on_every_signal(self):
        for name, ths in A.THRESHOLDS.items():
            with self.subTest(signal=name):
                cs = A.crossings(self.sig(**{name: float(ths[1])}), window=1)
                self.assertEqual([(c["threshold"], c["urgent"]) for c in cs], [(ths[1], True)])

    def test_stateless_window_fires_once_around_the_crossing_and_not_long_after(self):
        self.assertEqual(len(A.crossings(self.sig(stale=40.0), window=20)), 1)
        self.assertEqual(A.crossings(self.sig(stale=56.0), window=20), [])     # 21 past 35, before 75

    def test_every_threshold_fires_at_least_once_at_any_phase_of_a_15_minute_check_cycle(self):
        """The same brute force as the laptop watchdog: window 20 over spacing 15 never misses and at most doubles."""
        for name, ths in A.THRESHOLDS.items():
            for t in ths:
                for phase in range(15):
                    hits = sum(1 for k in range(0, 40)
                               if any(c["threshold"] == t for c in A.crossings(
                                   {**self.sig(), name: float(phase + k * 15)}, window=20)
                                   if c["signal"] == name))
                    self.assertIn(hits, (1, 2), (name, t, phase))

    def test_unmeasured_signals_are_skipped_not_alerted(self):
        self.assertEqual(A.crossings({"queued": None, "no_trigger": None, "stale": None}), [])

    def test_exact_mode_fires_once_per_threshold_until_recovery(self):
        fired = {}
        self.assertEqual(self.names(A.crossings(self.sig(stale=36.0), fired)), [("stale", 35, False)])
        self.assertEqual(A.crossings(self.sig(stale=50.0), fired), [])                  # not again
        self.assertEqual(self.names(A.crossings(self.sig(stale=76.0), fired)), [("stale", 75, True)])
        self.assertEqual(A.crossings(self.sig(stale=90.0), fired), [])
        A.crossings(self.sig(stale=10.0), fired)                                        # recovered
        self.assertEqual(fired, {})
        self.assertEqual(self.names(A.crossings(self.sig(stale=36.0), fired)), [("stale", 35, False)])   # new episode

    def test_exact_mode_signals_are_independent(self):
        fired = {}
        A.crossings(self.sig(stale=40.0), fired)
        cs = A.crossings(self.sig(stale=40.0, queued=7.0), fired)
        self.assertEqual(self.names(cs), [("queued", 6, False)])


class AlertTextTests(unittest.TestCase):
    def build(self, **kw):
        now = at(15, 40)
        runs = [run(at(15, 30, 9), "queued", None)]
        cs = A.crossings({"queued": 10.0, "no_trigger": 5.0, "stale": 10.0, **kw}, window=100)
        return A.build_alert(cs, now, runs)

    def test_first_level_is_late_and_not_urgent(self):
        subject, body, urgent = self.build()
        self.assertEqual((subject, urgent), ("ARM A WATCH: Arm A is late", False))
        self.assertIn("created at 11:30 ET has waited 10 minutes", body)
        self.assertIn("githubstatus.com", body)

    def test_second_level_is_the_alert_and_urgent(self):
        subject, _, urgent = self.build(queued=21.0)
        self.assertEqual((subject, urgent), ("ARM A WATCH ALERT: Arm A is not running", True))

    def test_it_says_why_it_matters_and_that_nothing_trades(self):
        body = self.build()[1]
        self.assertIn("NOT being protected", body)
        self.assertIn("Nothing here places, moves or cancels a trade", body)

    def test_plain_ascii_and_no_em_dash_and_names_the_right_arm(self):
        for kw in ({}, {"queued": 21.0}, {"stale": 80.0, "no_trigger": 60.0}):
            subject, body, _ = self.build(**kw)
            self.assertTrue((subject + body).isascii())
            self.assertNotIn("RH laptop", subject + body)

    def test_each_signal_has_its_own_explanation(self):
        text = {}
        for name in ("queued", "no_trigger", "stale"):
            c = A.crossings({"queued": 0.0, "no_trigger": 0.0, "stale": 0.0, name: float(A.THRESHOLDS[name][0])}, window=50)
            text[name] = A.describe(c[0], at(15, 40), [])
        self.assertIn("machine", text["queued"])
        self.assertIn("cron-job.org", text["no_trigger"])
        self.assertIn("status.json", text["stale"])


class RunOnceTests(unittest.TestCase):
    def go(self, now, commit=None, status=None, runs=None, deliver=None, **kw):
        sent = []

        def d(subject, body, urgent):
            sent.append((subject, urgent))
            return True if deliver is None else deliver
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc, line, delivered = A.run_once(now=now, deliver=d, get_commit_time=lambda ref: commit,
                                             get_status=lambda ref: status, get_runs=lambda: runs, **kw)
        return rc, line, sent

    def healthy(self, now):
        return dict(commit=now - timedelta(minutes=9), runs=[run(now - timedelta(minutes=8))])

    def test_outside_the_session_nothing_is_checked_and_nothing_sent(self):
        for now in (at(13, 30), at(20, 5), at(15, 0, day=4)):
            rc, line, sent = self.go(now, **self.healthy(now))
            self.assertEqual((rc, sent), (0, []))
            self.assertIn("market closed", line)

    def test_a_healthy_arm_a_is_quiet_and_the_line_shows_the_numbers(self):
        now = at(15, 0)
        rc, line, sent = self.go(now, **self.healthy(now))
        self.assertEqual((rc, sent), (0, []))
        self.assertIn("status commit age 9m", line)
        self.assertIn("last run created 8m ago", line)

    def test_a_run_waiting_seven_minutes_alerts_not_urgently(self):
        now = at(15, 37)
        runs = [run(now - timedelta(minutes=7), "queued", None), run(now - timedelta(minutes=22))]
        rc, line, sent = self.go(now, commit=now - timedelta(minutes=10), runs=runs)
        self.assertEqual((rc, sent), (0, [("ARM A WATCH: Arm A is late", False)]))
        self.assertIn("ALERTED", line)

    def test_no_trigger_for_a_while_alerts(self):
        now = at(15, 0)
        rc, _, sent = self.go(now, commit=now - timedelta(minutes=20), runs=[run(now - timedelta(minutes=23))])
        self.assertEqual([s for s, _ in sent], ["ARM A WATCH: Arm A is late"])

    def test_a_stale_status_alerts_even_when_runs_look_fine(self):
        now = at(15, 0)
        rc, _, sent = self.go(now, commit=now - timedelta(minutes=36), runs=[run(now - timedelta(minutes=3))])
        self.assertEqual(len(sent), 1)

    def test_a_failed_delivery_is_a_red_run(self):
        now = at(15, 37)
        rc, line, sent = self.go(now, commit=now - timedelta(minutes=10),
                                 runs=[run(now - timedelta(minutes=7), "queued", None)], deliver=False)
        self.assertEqual(rc, 1)
        self.assertIn("NOT DELIVERED", line)

    def test_nothing_measurable_is_a_red_run_not_a_calm_one(self):
        rc, line, sent = self.go(at(15, 0))
        self.assertEqual((rc, sent), (1, []))
        self.assertIn("could not measure anything", line)

    def test_partial_measurement_still_alerts_on_what_it_can_see(self):
        now = at(15, 0)
        rc, line, sent = self.go(now, commit=now - timedelta(minutes=40), runs=None)
        self.assertEqual(len(sent), 1)
        self.assertIn("longest wait for a machine unknown", line)

    def test_exact_mode_alerts_once_then_sends_one_recovery_notice(self):
        fired = {}
        runs = lambda now, w: [run(now - timedelta(minutes=w), "queued", None)]
        now = at(15, 37)
        _, _, s1 = self.go(now, commit=now - timedelta(minutes=10), runs=runs(now, 7), fired=fired)
        now = at(15, 38)
        _, _, s2 = self.go(now, commit=now - timedelta(minutes=11), runs=runs(now, 8), fired=fired)
        now = at(15, 55)
        _, line, s3 = self.go(now, commit=now - timedelta(minutes=5), runs=[run(now - timedelta(minutes=3))], fired=fired)
        now = at(15, 56 - 1)
        self.assertEqual([s for s, _ in s1], ["ARM A WATCH: Arm A is late"])
        self.assertEqual(s2, [])
        self.assertEqual([s for s, _ in s3], ["ARM A WATCH: Arm A is running again"])
        self.assertFalse(s3[0][1])
        self.assertIn("RECOVERED", line)

    def test_exact_mode_retries_a_failed_delivery_on_the_next_check(self):
        fired = {}
        now = at(15, 37)
        kw = dict(commit=now - timedelta(minutes=10), runs=[run(now - timedelta(minutes=7), "queued", None)], fired=fired)
        rc1, _, s1 = self.go(now, deliver=False, **kw)
        now2 = at(15, 38)
        kw["runs"] = [run(now2 - timedelta(minutes=8), "queued", None)]
        kw["commit"] = now2 - timedelta(minutes=11)
        rc2, _, s2 = self.go(now2, deliver=True, **kw)
        self.assertEqual(rc1, 1)
        self.assertEqual([s for s, _ in s2], ["ARM A WATCH: Arm A is late"])       # the lost alert is re-sent
        rc3, _, s3 = self.go(at(15, 39), deliver=True, commit=at(15, 39) - timedelta(minutes=12),
                             runs=[run(at(15, 39) - timedelta(minutes=9), "queued", None)], fired=fired)
        self.assertEqual(s3, [])                                                   # and then not again

    def test_exact_mode_gives_up_retrying_after_three_failures(self):
        fired = {}
        sent_counts = []
        for i in range(5):
            now = at(15, 37) + timedelta(minutes=i)
            self.go(now, deliver=False, commit=now - timedelta(minutes=10 + i),
                    runs=[run(now - timedelta(minutes=7 + i), "queued", None)], fired=fired)
            sent_counts.append(fired.get("_failed", {}).get(("queued", 6), 0))
        self.assertEqual(sent_counts, [1, 2, 3, 3, 3])

    def test_a_retry_counter_alone_is_not_an_open_episode(self):
        self.assertFalse(A._active({"_failed": {("queued", 6): 3}}))
        self.assertTrue(A._active({"queued": {6}, "_failed": {}}))
        self.assertFalse(A._active({}))

    def test_no_recovery_notice_when_there_was_no_episode(self):
        fired = {}
        now = at(15, 0)
        _, _, sent = self.go(now, fired=fired, **self.healthy(now))
        self.assertEqual(sent, [])


class October5Replay(unittest.TestCase):
    """The REAL timeline of 2026-10-05, from `gh run list` and the status.json commit log. Arm A's last real
    cycle committed at 19:26:53Z; the 19:30:09Z and 19:45:07Z runs never got a machine and were cancelled at
    19:45:12Z and 20:00:22Z. Market close is 19:55Z. This is logic on real inputs, not the incident itself."""
    LAST_COMMIT = datetime(2026, 10, 5, 19, 26, 53, tzinfo=UTC)

    def runs_at(self, now):
        r = []
        for created, ended in ((datetime(2026, 10, 5, 19, 15, 7, tzinfo=UTC), self.LAST_COMMIT + timedelta(seconds=5)),
                               (datetime(2026, 10, 5, 19, 30, 9, tzinfo=UTC), datetime(2026, 10, 5, 19, 45, 12, tzinfo=UTC)),
                               (datetime(2026, 10, 5, 19, 45, 7, tzinfo=UTC), datetime(2026, 10, 5, 20, 0, 22, tzinfo=UTC))):
            if created <= now:
                still = now < ended
                bad = created.minute in (30, 45) and created.hour == 19
                r.append(run(created, ("queued" if bad else "in_progress") if still else "completed",
                             None if still else ("failure" if bad else "success")))
        return r

    def first_alert(self, step_seconds=60):
        now = datetime(2026, 10, 5, 19, 28, tzinfo=UTC)
        fired = {}
        while now <= datetime(2026, 10, 5, 19, 55, tzinfo=UTC):
            sent = []
            with contextlib.redirect_stdout(io.StringIO()):
                A.run_once(now=now, deliver=lambda s, b, u: sent.append((s, u)) or True, fired=fired,
                           get_commit_time=lambda ref: self.LAST_COMMIT, get_status=lambda ref: None,
                           get_runs=lambda n=now: self.runs_at(n))
            if sent:
                return now, sent
            now += timedelta(seconds=step_seconds)
        return None, []

    def test_it_alerts_about_six_minutes_after_the_first_stuck_run_and_inside_market_hours(self):
        when, sent = self.first_alert()
        self.assertIsNotNone(when, "never alerted before the close")
        self.assertEqual(sent, [("ARM A WATCH: Arm A is late", False)])
        created = datetime(2026, 10, 5, 19, 30, 9, tzinfo=UTC)
        waited = (when - created).total_seconds() / 60
        self.assertGreaterEqual(waited, 6.0)                                   # never before the threshold
        self.assertLess(waited, 7.0)                                           # and within one 60 s tick of it
        self.assertLess(when, datetime(2026, 10, 5, 19, 55, tzinfo=UTC))

    def test_the_stale_signal_alone_would_have_been_too_late_which_is_why_the_queue_signal_exists(self):
        """Positive control for the design: with no runs list the same history alerts only after the close."""
        now = datetime(2026, 10, 5, 19, 28, tzinfo=UTC)
        alerted = None
        while now <= datetime(2026, 10, 5, 20, 5, tzinfo=UTC):
            sig = A.measure(now, datetime(2026, 10, 5, 13, 45, tzinfo=UTC), self.LAST_COMMIT)
            if A.crossings(sig, {}):
                alerted = now
                break
            now += timedelta(minutes=1)
        self.assertGreater(alerted, datetime(2026, 10, 5, 19, 55, tzinfo=UTC))

    def test_it_goes_urgent_if_the_wait_reaches_twenty_minutes(self):
        now = datetime(2026, 10, 5, 19, 50, 9, tzinfo=UTC)                      # 20 minutes after 19:30:09, before close
        sent = []
        with contextlib.redirect_stdout(io.StringIO()):
            A.run_once(now=now, deliver=lambda s, b, u: sent.append((s, u)) or True, fired={},
                       get_commit_time=lambda ref: self.LAST_COMMIT, get_status=lambda ref: None,
                       get_runs=lambda: [run(datetime(2026, 10, 5, 19, 30, 9, tzinfo=UTC), "queued", None)])
        self.assertIn(("ARM A WATCH ALERT: Arm A is not running", True), sent)


class Inputs(unittest.TestCase):
    def test_git_commit_time_reads_the_last_commit_that_touched_status_json(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            def git(*a, env=None):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                               cwd=d, check=True, capture_output=True, env=env)
            git("init", "-q", "-b", "main")
            for name, when in (("status.json", "2026-10-06T14:00:00+00:00"), ("other.txt", "2026-10-06T14:20:00+00:00")):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(when)
                git("add", name)
                env = dict(os.environ, GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when)
                git("commit", "-q", "-m", name, env=env)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(A.git_commit_time("HEAD"), datetime(2026, 10, 6, 14, 0, tzinfo=UTC))
                self.assertIsNone(A.git_commit_time("HEAD", path="never.json"))
            finally:
                os.chdir(cwd)

    def test_fetch_runs_parses_the_actions_api(self):
        body = json.dumps({"workflow_runs": [
            {"status": "queued", "conclusion": None, "created_at": "2026-10-05T19:30:09Z"},
            {"status": "completed", "conclusion": "success", "created_at": "2026-10-05T19:15:07Z"}]}).encode()

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return body
        seen = {}

        def fake_open(req, timeout=0):
            seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
            return R()
        with mock.patch.object(A.urllib.request, "urlopen", fake_open):
            runs = A.fetch_runs(repo="o/r", token="tok")
        self.assertIn("/repos/o/r/actions/workflows/alpaca-bot.yml/runs", seen["url"])
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual([(r["status"], r["created_at"].minute) for r in runs], [("queued", 30), ("completed", 15)])

    def test_fetch_runs_failure_is_none_not_an_empty_list(self):
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=OSError("down")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(A.fetch_runs())

    def test_the_status_reader_returns_none_for_a_missing_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(A.read_status(path="no_such_status_file.json"))


class Delivery(unittest.TestCase):
    def run_alert(self, **kw):
        sent = {"slack": [], "email": []}
        import slack_notify
        env = {"GMAIL_APP_PASSWORD": "pw", "GMAIL_USER": "bot@example.invalid", "ALERT_EMAIL": "devon@example.invalid",
               "SMS_TO": "", "NTFY_TOPIC": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(slack_notify, "post", lambda t, **k: sent["slack"].append(t) or True), \
                mock.patch.object(W, "_email", lambda frm, pw, to, subj, body: sent["email"].append((subj, body))), \
                contextlib.redirect_stdout(io.StringIO()):
            ok = W.alert("body text", **kw)
        return ok, sent

    def test_a_custom_subject_and_title_reach_email_and_slack(self):
        ok, sent = self.run_alert(urgent=True, subject="ARM A WATCH ALERT: Arm A is not running",
                                  title="ARM A WATCH ALERT: Arm A is not running")
        self.assertTrue(ok)
        self.assertEqual(sent["email"][0][0], "ARM A WATCH ALERT: Arm A is not running")
        self.assertTrue(sent["slack"][0].startswith("*ARM A WATCH ALERT: Arm A is not running*\n"))

    def test_the_laptop_watchdogs_own_wording_is_unchanged_without_them(self):
        _, sent = self.run_alert(urgent=False)
        self.assertEqual(sent["email"][0][0], "RH laptop bot is not reporting (not urgent)")
        self.assertTrue(sent["slack"][0].startswith("*RH laptop bot is not reporting (not urgent)*"))
        _, sent = self.run_alert(urgent=True)
        self.assertEqual(sent["email"][0][0], "ALERT: RH laptop bot needs attention")

    def test_default_deliver_routes_through_the_shared_alert(self):
        calls = []
        with mock.patch.object(W, "alert", lambda msg, urgent=False, subject=None, title=None: calls.append((subject, title, urgent)) or True):
            self.assertTrue(A.default_deliver("S", "B", True))
        self.assertEqual(calls, [("S", "S", True)])

    def test_force_sends_a_test_alert_and_reports_delivery_honestly(self):
        with mock.patch.object(A, "default_deliver", lambda s, b, u: True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["--force"]), 0)
        with mock.patch.object(A, "default_deliver", lambda s, b, u: False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["--force"]), 1)

    def test_a_plain_test_never_texts_and_only_the_urgent_test_does(self):
        calls = []
        with mock.patch.object(A, "default_deliver", lambda s, b, u: calls.append(u) or True),                 contextlib.redirect_stdout(io.StringIO()):
            A.main(["--force"])
            A.main(["--force-urgent"])
            with mock.patch.dict(os.environ, {"FORCE_ALERT": "true"}):
                A.main([])
            with mock.patch.dict(os.environ, {"FORCE_ALERT": "urgent"}):
                A.main([])
            with mock.patch.dict(os.environ, {"FORCE_ALERT": "no"}), mock.patch.object(A, "run_once", lambda **k: (0, "x", [])):
                A.main([])
        self.assertEqual(calls, [False, True, False, True])           # FORCE_ALERT=no is a normal check, not a test


class WorkflowWiring(unittest.TestCase):
    def wf(self, name):
        with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_it_follows_arm_a_by_its_exact_name_and_also_has_a_schedule(self):
        w, arm = self.wf("arm-a-watch.yml"), self.wf("alpaca-bot.yml")
        on = w.get(True) or w.get("on")
        self.assertEqual(on["workflow_run"]["workflows"], [arm["name"]])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        self.assertIn("schedule", on)
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(w["name"], "ARM A WATCH")

    def test_the_dispatch_test_button_offers_no_yes_and_urgent(self):
        on = self.wf("arm-a-watch.yml").get(True) or self.wf("arm-a-watch.yml").get("on")
        f = on["workflow_dispatch"]["inputs"]["force"]
        self.assertEqual((f["type"], f["options"], f["default"]), ("choice", ["no", "yes", "urgent"], "no"))

    def test_no_concurrency_group_so_a_delayed_run_is_never_dropped_for_a_newer_one(self):
        w = self.wf("arm-a-watch.yml")
        self.assertNotIn("concurrency", w)
        self.assertNotIn("concurrency", w["jobs"]["watch"])

    def test_it_may_read_actions_and_nothing_else_writes(self):
        w = self.wf("arm-a-watch.yml")
        self.assertEqual(w["permissions"], {"contents": "read", "actions": "read"})

    def test_history_is_deep_enough_to_find_the_last_status_commit(self):
        w = self.wf("arm-a-watch.yml")
        co = next(s for s in w["jobs"]["watch"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
        self.assertGreaterEqual(co["with"]["fetch-depth"], 100)

    def test_the_check_step_gets_the_channels_and_a_per_trigger_window(self):
        w = self.wf("arm-a-watch.yml")
        step = next(s for s in w["jobs"]["watch"]["steps"] if "arm_a_watch.py" in str(s.get("run", "")))
        for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_EMAIL", "SLACK_WEBHOOK_URL", "SMS_TO", "NTFY_TOPIC",
                  "GH_TOKEN", "FORCE_ALERT", "WATCH_WINDOW_MIN"):
            self.assertIn(k, step["env"])
        self.assertIn("schedule", step["env"]["WATCH_WINDOW_MIN"])

    def test_the_workflow_names_the_repo_it_watches_the_same_way_the_module_does(self):
        self.assertEqual(A.WORKFLOW_FILE, "alpaca-bot.yml")
        self.assertTrue(os.path.exists(os.path.join(ROOT, ".github", "workflows", A.WORKFLOW_FILE)))


if __name__ == "__main__":
    unittest.main()
