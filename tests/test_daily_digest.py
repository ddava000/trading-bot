"""daily_digest.py: the GitHub-run summary that replaced the two scheduled daily-check sessions (Devon 2026-10-17).

The sessions it replaces failed silently (one froze on 2026-10-14 and skipped two days, one was killed by the shared session limit), so the first
thing to pin is that THIS thing cannot fail the same way: it sends once per trading day on a trigger that really fires, it never calls an
unreadable source calm, and a delivery that reaches no channel makes the run red. Every guard is shown able to fail as well as pass.

THE BUG ITS FIRST REAL DAY FOUND (2026-10-19): the first version was due 10 minutes after the close and only the "first Arm A run to finish after
that" could send. Arm A's last run of the day starts 15:45 and ends about 15:55:45, so no run ever finished after the threshold, and the digest went out
only through the best-effort schedule, three hours late. The cadence test below pins that run as a trigger; a due time later than that run fails it.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ALPACA_API_KEY", "test-key-not-real")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret-not-real")

import daily_digest as D  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def et(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=ET).astimezone(UTC)


class WhenItSends(unittest.TestCase):
    TUE = (2026, 10, 13)            # ordinary trading day: the bot closes 15:55, the digest is due at the same moment

    def test_not_on_a_weekend_or_holiday(self):
        self.assertFalse(D.should_send(et(2026, 10, 17, 17, 0), False)[0])        # Saturday
        self.assertFalse(D.should_send(et(2026, 11, 26, 17, 0), False)[0])        # Thanksgiving

    def test_not_before_the_bots_own_close(self):
        self.assertFalse(D.should_send(et(*self.TUE, 15, 54, 59), False)[0])
        self.assertTrue(D.should_send(et(*self.TUE, 15, 55, 0), False)[0])

    def test_the_last_arm_a_run_of_the_day_is_a_trigger(self):
        """THE FIRST REAL DAY'S BUG. That run finishes about 45 seconds after the close; a later due time could never be reached by it."""
        last_run_finished = et(*self.TUE, 15, 55, 45)
        ok, why = D.should_send(last_run_finished, False)
        self.assertTrue(ok, why)

    def test_once_sent_no_later_trigger_sends_again(self):
        self.assertTrue(D.should_send(et(*self.TUE, 15, 56), False)[0])
        self.assertFalse(D.should_send(et(*self.TUE, 16, 12), True)[0])
        self.assertFalse(D.should_send(et(*self.TUE, 21, 40), True)[0])

    def test_any_trigger_late_in_the_evening_still_sends_if_nothing_went_out(self):
        self.assertTrue(D.should_send(et(*self.TUE, 20, 33), False)[0])                  # the throttled cron, three hours late

    def test_early_close_day_is_due_at_1255_not_1555(self):
        fri = (2026, 11, 27)
        self.assertTrue(D.should_send(et(*fri, 12, 55, 30), False)[0])
        self.assertFalse(D.should_send(et(*fri, 12, 54, 30), False)[0])
        self.assertFalse(D.should_send(et(*self.TUE, 12, 55, 30), False)[0])             # control: same clock on an ordinary day

    def test_force_always_sends_even_on_a_saturday_or_after_a_send(self):
        self.assertTrue(D.should_send(et(2026, 10, 17, 10, 0), False, force=True)[0])
        self.assertTrue(D.should_send(et(*self.TUE, 17, 0), True, force=True)[0])


class AlreadySentIsReadFromGitHub(unittest.TestCase):
    """No stored state: 'sent' means a run of this workflow today whose send step ended in success."""
    DAY = datetime(2026, 10, 13).date()

    def run_row(self, rid, created, status="completed"):
        return {"id": rid, "created_at": created, "status": status}

    def fetcher(self, runs, steps_by_run):
        def fetch(path):
            if path.startswith("actions/workflows/daily-digest.yml/runs"):
                return {"workflow_runs": runs}
            rid = int(path.split("/")[2])                        # actions/runs/<id>/jobs
            return {"jobs": [{"steps": [{"name": n, "conclusion": c} for n, c in steps_by_run.get(rid, [])]}]}
        return fetch

    def test_a_successful_send_step_today_means_sent(self):
        runs = [self.run_row(3, "2026-10-13T20:30:00Z"), self.run_row(2, "2026-10-13T19:56:00Z")]
        steps = {3: [("Decide whether a digest is due", "success"), (D.SEND_STEP, "skipped")], 2: [(D.SEND_STEP, "success")]}
        self.assertTrue(D.digest_already_sent(self.DAY, self.fetcher(runs, steps)))

    def test_skipped_or_failed_send_steps_do_not_count_so_a_failed_send_retries(self):
        runs = [self.run_row(3, "2026-10-13T20:30:00Z"), self.run_row(2, "2026-10-13T19:56:00Z")]
        steps = {3: [(D.SEND_STEP, "skipped")], 2: [(D.SEND_STEP, "failure")]}
        self.assertFalse(D.digest_already_sent(self.DAY, self.fetcher(runs, steps)))

    def test_a_send_before_the_due_time_does_not_count_for_the_real_digest(self):
        """A forced test at noon must not suppress the real one at the close."""
        runs = [self.run_row(1, "2026-10-13T16:00:00Z")]                                   # 12:00 ET
        self.assertFalse(D.digest_already_sent(self.DAY, self.fetcher(runs, {1: [(D.SEND_STEP, "success")]})))

    def test_an_unfinished_run_is_ignored_so_the_asker_does_not_count_itself(self):
        runs = [self.run_row(9, "2026-10-13T19:56:00Z", status="in_progress")]
        self.assertFalse(D.digest_already_sent(self.DAY, self.fetcher(runs, {9: [(D.SEND_STEP, "success")]})))

    def test_an_api_failure_means_not_sent_because_a_duplicate_beats_silence(self):
        def boom(path):
            raise OSError("api down")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(D.digest_already_sent(self.DAY, boom))

    def test_a_non_trading_day_is_never_marked_sent(self):
        self.assertFalse(D.digest_already_sent(datetime(2026, 10, 17).date(), self.fetcher([], {})))

    def test_decide_reads_github_only_when_it_has_to(self):
        calls = []
        orig = D.digest_already_sent
        D.digest_already_sent = lambda day, fetch=None: (calls.append(day), True)[1]
        try:
            self.assertEqual(D.decide(et(*WhenItSends.TUE, 16, 5))[0], False)               # sent per the stub
            self.assertTrue(D.decide(et(*WhenItSends.TUE, 16, 5), force=True)[0])            # force never asks
            self.assertEqual(len(calls), 1)
        finally:
            D.digest_already_sent = orig


def facts(**kw):
    f = {"date": "2026-10-13", "weekday": "Tue", "trading_day": True,
         "runs": {"by_name": {"Alpaca Trading Bot": {"total": 28, "bad": [], "cancelled": 0}}, "bad": []}, "ci": "success", "ci_when": "10-13 11:25",
         "incidents": [],
         "status": {"equity": 231.0, "day_pnl": -1.5, "vs_baseline_pct": -6.8, "halted": False, "vix_live": True},
         "arm_a_finished": 25, "arm_a_expected": 24, "arm_a_unplanned": 0.0,
         "rh": {"equity": 308.0, "ts": "2026-10-13T15:54", "degraded": None},
         "arm_b": {"degraded": 0.0, "no_push": 11.0, "edge": 11.0},
         "realized_lines": ["Arm A line", "Arm B line"], "mail_today": 3}
    f.update(kw)
    return f


class TheReport(unittest.TestCase):
    def flags(self, **kw):
        return D.build_report(facts(**kw))[1]

    def test_a_healthy_day_is_ok_and_names_both_arms(self):
        verdict, flags, subject, body = D.build_report(facts())
        self.assertEqual((verdict, flags), ("OK", []))
        self.assertIn("ARM A", body)
        self.assertIn("ARM B", body)
        self.assertIn("Arm A line", body)
        self.assertIn("2026-10-13", subject)
        self.assertIn("CI latest: success (10-13 11:25 ET)", body)

    def test_subject_and_body_are_plain_ascii_with_no_em_dash(self):
        _, _, subject, body = D.build_report(facts())
        self.assertTrue((subject + body).isascii())
        self.assertNotIn("—", subject + body)

    def test_a_failed_run_is_a_flag(self):
        f = self.flags(runs={"by_name": {"ci": {"total": 1, "bad": ["failure"], "cancelled": 0}}, "bad": [("ci", "failure", "14:15")]})
        self.assertTrue(any("FAILURE" in x and "ci" in x for x in f), f)

    def test_a_cancelled_run_is_reported_but_is_not_a_flag(self):
        """2026-10-19: a laptop watchdog run was cancelled during a GitHub Actions incident and the first digest cried wolf about it."""
        verdict, flags, _, body = D.build_report(facts(
            runs={"by_name": {"RH laptop watchdog": {"total": 30, "bad": [], "cancelled": 1}}, "bad": []},
            incidents=[("11:49", "12:56", "critical", "Degraded availability for GitHub Actions")]))
        self.assertEqual((verdict, flags), ("OK", []))
        self.assertIn("1 cancelled", body)
        self.assertIn("critical incident 11:49 to 12:56 ET", body)

    def test_red_ci_is_a_flag_and_a_missing_ci_is_a_flag_but_an_old_green_one_is_fine(self):
        self.assertTrue(any("CI latest run is failure" in x for x in self.flags(ci="failure")))
        self.assertTrue(any("COULD NOT READ the latest CI" in x for x in self.flags(ci=None)))
        self.assertEqual(self.flags(ci="success", ci_when="10-12 09:00"), [])         # CI runs only on pushes: yesterday's green is current

    def test_a_halted_arm_a_and_a_dead_vix_feed_are_flags(self):
        f = self.flags(status={"equity": 1, "day_pnl": 0, "vs_baseline_pct": 0, "halted": True, "vix_live": False})
        self.assertTrue(any("HALTED" in x for x in f))
        self.assertTrue(any("VIX" in x for x in f))

    def test_too_few_arm_a_runs_is_a_flag_and_the_floor_is_exact(self):
        self.assertTrue(any("RAN 17" in x for x in self.flags(arm_a_finished=17)))                    # under 75% of 24
        self.assertEqual(self.flags(arm_a_finished=18), [])                                          # exactly 75% passes

    def test_unplanned_arm_a_gaps_and_arm_b_blindness_have_thresholds(self):
        self.assertEqual(self.flags(arm_a_unplanned=14.0), [])
        self.assertTrue(self.flags(arm_a_unplanned=15.0))
        self.assertEqual(self.flags(arm_b={"degraded": 0.0, "no_push": 14.0, "edge": 0.0}), [])
        self.assertTrue(self.flags(arm_b={"degraded": 20.0, "no_push": 0.0, "edge": 0.0}))

    def test_edge_minutes_are_not_counted_as_blindness(self):
        self.assertEqual(self.flags(arm_b={"degraded": 0.0, "no_push": 40.0, "edge": 30.0}), [])

    def test_a_held_back_equity_reading_is_a_flag(self):
        f = self.flags(rh={"equity": 308.0, "ts": "x", "degraded": None, "broker_total_value_held_back": 318.6})
        self.assertTrue(any("held back" in x for x in f))

    def test_an_unreadable_source_is_never_called_calm(self):
        """Unknown is a flag, never a pass: the property the sessions it replaces did not have."""
        _, flags, _, body = D.build_report(facts(runs=None, ci=None, status=None, rh=None, arm_b=None, realized_lines=None,
                                                  arm_a_finished=None, arm_a_expected=None, arm_a_unplanned=None))
        self.assertGreaterEqual(len([x for x in flags if x.startswith("COULD NOT")]), 8)
        self.assertIn("ATTENTION", body)

    def test_the_digests_own_invocations_are_not_listed_unless_one_failed(self):
        quiet = D.build_report(facts(runs={"by_name": {"DAILY DIGEST": {"total": 28, "bad": [], "cancelled": 0}}, "bad": []}))[3]
        self.assertNotIn("DAILY DIGEST   ", quiet.split("GITHUB")[1])
        loud = D.build_report(facts(runs={"by_name": {"DAILY DIGEST": {"total": 28, "bad": ["failure"], "cancelled": 0}},
                                          "bad": [("DAILY DIGEST", "failure", "16:05")]}))
        self.assertTrue(any("DAILY DIGEST" in x for x in loud[1]))


class ANonTradingDay(unittest.TestCase):
    """A forced test digest on a weekend must not cry 'could not measure' about things that do not exist that day."""

    def test_no_cadence_or_blindness_flags_when_there_was_no_session(self):
        _, flags, _, body = D.build_report(facts(trading_day=False, arm_a_finished=None, arm_a_expected=None, arm_a_unplanned=None, arm_b=None))
        self.assertEqual(flags, [])
        self.assertIn("not a trading day", body)

    def test_the_same_unknowns_on_a_trading_day_are_still_flags_control(self):
        _, flags, _, _ = D.build_report(facts(trading_day=True, arm_a_finished=None, arm_a_expected=None, arm_a_unplanned=None, arm_b=None))
        self.assertEqual(len([x for x in flags if x.startswith("COULD NOT")]), 3)

    def test_gather_marks_a_saturday_and_a_weekday_correctly(self):
        os.chdir(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(D.gather(et(2026, 10, 17, 17, 0), [], ROOT)["trading_day"])
            self.assertTrue(D.gather(et(2026, 10, 16, 17, 0), [], ROOT)["trading_day"])


class TheRunsList(unittest.TestCase):
    def row(self, name, created, updated=None, status="completed", conclusion="success", rid=1):
        return {"name": name, "created_at": created, "updated_at": updated or created, "status": status, "conclusion": conclusion, "id": rid}

    def test_runs_are_grouped_by_their_eastern_date_and_bad_ones_listed(self):
        day = datetime(2026, 10, 13).date()
        raw = [self.row("ci", "2026-10-13T18:00:00Z"),
               self.row("ci", "2026-10-13T19:00:00Z", conclusion="failure"),
               self.row("ci", "2026-10-12T18:00:00Z", conclusion="failure"),             # yesterday: not today's
               self.row("ARM A WATCH", "2026-10-13T18:05:00Z", status="in_progress", conclusion=None)]   # unfinished: ignored
        s = D.summarize_runs(raw, day)
        self.assertEqual(s["by_name"]["ci"]["total"], 2)
        self.assertEqual(s["bad"], [("ci", "failure", "15:00")])
        self.assertNotIn("ARM A WATCH", s["by_name"])

    def test_cancelled_runs_are_counted_not_listed_as_bad(self):
        day = datetime(2026, 10, 19).date()
        s = D.summarize_runs([self.row("RH laptop watchdog", "2026-10-19T15:30:33Z", conclusion="cancelled")], day)
        self.assertEqual((s["bad"], s["by_name"]["RH laptop watchdog"]["cancelled"]), ([], 1))

    def test_a_run_created_late_in_the_eastern_evening_belongs_to_that_day_not_the_utc_date(self):
        day = datetime(2026, 10, 13).date()
        raw = [self.row("x", "2026-10-14T01:30:00Z")]                                      # 21:30 ET on the 13th
        self.assertEqual(D.summarize_runs(raw, day)["by_name"]["x"]["total"], 1)

    def test_only_arm_a_runs_count_for_the_cadence(self):
        day = datetime(2026, 10, 13).date()
        raw = [self.row("Alpaca Trading Bot", "2026-10-13T19:00:00Z", rid=5), self.row("ci", "2026-10-13T19:00:00Z", rid=6)]
        self.assertEqual([r["id"] for r in D.arm_a_finished(raw, day)], [5])


class TheCadenceCount(unittest.TestCase):
    def test_only_arm_a_runs_that_finished_inside_the_session_count(self):
        """The pre-open runs exit in seconds and the post-close ones are market-closed exits: counting them would hide a real outage."""
        def r(i, created, updated):
            return {"name": "Alpaca Trading Bot", "created_at": created, "updated_at": updated, "run_started_at": created,
                    "status": "completed", "conclusion": "success", "id": i}
        raw = [r(1, "2026-10-13T13:00:09Z", "2026-10-13T13:00:25Z"),       # 09:00 ET: before the open
               r(2, "2026-10-13T13:30:09Z", "2026-10-13T13:30:25Z"),       # 09:30 ET: before the open
               r(3, "2026-10-13T14:00:09Z", "2026-10-13T14:10:30Z"),       # 10:10 ET: inside
               r(4, "2026-10-13T19:45:09Z", "2026-10-13T19:55:30Z"),       # 15:55 ET: inside (the last protective run)
               r(5, "2026-10-13T20:15:09Z", "2026-10-13T20:15:30Z")]       # 16:15 ET: after the close
        os.chdir(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            f = D.gather(et(2026, 10, 13, 16, 8), raw, ROOT)
        self.assertEqual(f["arm_a_finished"], 2)
        self.assertEqual(f["arm_a_expected"], 24)

    def test_an_early_close_day_expects_12_runs_not_24(self):
        os.chdir(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            f = D.gather(et(2026, 11, 27, 13, 8), [], ROOT)
        self.assertEqual(f["arm_a_expected"], 12)


class TheOtherSources(unittest.TestCase):
    def test_ci_is_the_latest_completed_run_whatever_its_date(self):
        got = D.fetch_ci(lambda path: {"workflow_runs": [{"conclusion": "success", "created_at": "2026-10-12T15:00:00Z"}]})
        self.assertEqual(got, {"conclusion": "success", "when": "10-12 11:00"})

    def test_ci_unreadable_or_never_run_is_none_not_green(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(D.fetch_ci(lambda path: {"workflow_runs": []}))
            self.assertIsNone(D.fetch_ci(lambda path: (_ for _ in ()).throw(OSError("down"))))

    def test_only_todays_major_or_critical_github_incidents_are_listed(self):
        data = {"incidents": [
            {"created_at": "2026-10-19T15:49:00.000Z", "resolved_at": "2026-10-19T16:56:00.000Z", "impact": "critical", "name": "Actions degraded"},
            {"created_at": "2026-10-19T12:00:00.000Z", "resolved_at": None, "impact": "minor", "name": "latency"},
            {"created_at": "2026-10-15T18:31:00.000Z", "resolved_at": "2026-10-15T18:56:00.000Z", "impact": "major", "name": "yesterday-ish"}]}
        got = D.fetch_incidents(datetime(2026, 10, 19).date(), lambda: data)
        self.assertEqual(got, [("11:49", "12:56", "critical", "Actions degraded")])
        self.assertEqual(D.fetch_incidents(datetime(2026, 10, 20).date(), lambda: data), [])

    def test_an_unreadable_status_page_is_none_so_the_digest_says_nothing_rather_than_no_incident(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(D.fetch_incidents(datetime(2026, 10, 19).date(), lambda: (_ for _ in ()).throw(OSError("down"))))


class RunOnce(unittest.TestCase):
    NOW = et(2026, 10, 13, 15, 56)

    def raw(self):
        return [{"name": "Alpaca Trading Bot", "created_at": "2026-10-13T19:45:09Z", "updated_at": "2026-10-13T19:55:45Z",
                 "run_started_at": "2026-10-13T19:45:10Z", "status": "completed", "conclusion": "success", "id": 77}]

    def go(self, deliver, **kw):
        os.chdir(ROOT)
        args = dict(now=self.NOW, sent_today=False, raw=self.raw(), ci_info={"conclusion": "success", "when": "10-13 11:25"}, incidents=[], deliver=deliver)
        args.update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            return D.run_once(**args)

    def test_nothing_is_delivered_when_it_is_not_due(self):
        calls = []
        rc, line = self.go(lambda s, b: calls.append(s), now=et(2026, 10, 13, 12, 0))
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("not sending", line)

    def test_nothing_is_delivered_when_it_was_already_sent(self):
        calls = []
        rc, line = self.go(lambda s, b: calls.append(s), sent_today=True)
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("already sent", line)

    def test_a_delivery_that_reaches_no_channel_turns_the_run_red(self):
        rc, line = self.go(lambda s, b: [])
        self.assertEqual(rc, 1)
        self.assertIn("NOT delivered", line)

    def test_a_delivered_digest_exits_clean_and_names_the_channels(self):
        got = []
        rc, line = self.go(lambda s, b: (got.append((s, b)), ["slack", "email"])[1])
        self.assertEqual(rc, 0, line)
        self.assertIn("slack,email", line)
        self.assertIn("Trading bots daily digest 2026-10-13", got[0][0])

    def test_an_unreadable_runs_list_still_sends_and_says_so(self):
        got = []
        rc, line = self.go(lambda s, b: (got.append(b), ["slack"])[1], raw=None)
        self.assertEqual(rc, 0)
        self.assertIn("COULD NOT READ today's workflow runs", got[0])


class MainEntryPoints(unittest.TestCase):
    def test_decide_writes_the_workflow_output_true_when_due_and_false_when_not(self):
        import tempfile
        orig_decide = D.decide
        try:
            for due, expect in ((True, "due=true"), (False, "due=false")):
                D.decide = lambda now=None, force=False, sent_today=None, _d=due: (_d, "x")
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "out")
                    os.environ["GITHUB_OUTPUT"] = path
                    try:
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(D.main(["--decide"]), 0)
                    finally:
                        os.environ.pop("GITHUB_OUTPUT", None)
                    with open(path, encoding="utf-8") as f:
                        self.assertEqual(f.read().strip(), expect)
        finally:
            D.decide = orig_decide


class Wiring(unittest.TestCase):
    def wf(self):
        with open(os.path.join(ROOT, ".github", "workflows", "daily-digest.yml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def steps(self):
        return self.wf()["jobs"]["digest"]["steps"]

    def test_the_triggers_are_both_arm_a_and_the_brief_with_two_schedules_and_a_manual_test(self):
        on = self.wf().get("on") or self.wf().get(True)
        self.assertEqual(on["workflow_run"]["workflows"], ["Alpaca Trading Bot", "Research Brief (morning + intraday)"])
        self.assertGreaterEqual(len(on["schedule"]), 2)
        self.assertEqual(on["workflow_dispatch"]["inputs"]["force"]["options"], ["no", "yes"])

    def test_the_send_step_has_exactly_the_name_the_already_sent_check_looks_for(self):
        names = [s.get("name") for s in self.steps()]
        self.assertIn(D.SEND_STEP, names)
        send = [s for s in self.steps() if s.get("name") == D.SEND_STEP][0]
        self.assertIn("steps.decide.outputs.due", send["if"])
        self.assertIn("--send", send["run"])

    def test_the_decide_step_comes_first_and_exposes_its_output(self):
        decide = [s for s in self.steps() if s.get("id") == "decide"][0]
        self.assertIn("--decide", decide["run"])
        self.assertLess(self.steps().index(decide), self.steps().index([s for s in self.steps() if s.get("name") == D.SEND_STEP][0]))

    def test_secrets_reach_only_the_send_step_and_nothing_swallows_a_failure(self):
        send = [s for s in self.steps() if s.get("name") == D.SEND_STEP][0]
        for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_EMAIL", "SLACK_WEBHOOK_URL", "GH_TOKEN"):
            self.assertIn(k, send["env"])
        decide = [s for s in self.steps() if s.get("id") == "decide"][0]
        self.assertNotIn("GMAIL_APP_PASSWORD", decide["env"])
        for s in self.steps():
            self.assertNotIn("|| true", str(s.get("run", "")))

    def test_runs_are_serialised_without_cancelling_the_one_in_progress(self):
        c = self.wf()["concurrency"]
        self.assertEqual(c["group"], "daily-digest")
        self.assertFalse(c["cancel-in-progress"])

    def test_the_checkout_is_deep_enough_to_read_the_laptops_status_history(self):
        co = [s for s in self.steps() if str(s.get("uses", "")).startswith("actions/checkout")][0]
        self.assertGreaterEqual(int(co["with"]["fetch-depth"]), 200)

    def test_permissions_are_read_only(self):
        self.assertEqual(self.wf()["permissions"], {"contents": "read", "actions": "read"})


if __name__ == "__main__":
    unittest.main()
