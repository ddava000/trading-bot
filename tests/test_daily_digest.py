"""daily_digest.py: the GitHub-run summary that replaced the two scheduled daily-check sessions (Devon 2026-10-17).

The sessions it replaces failed silently (one froze on 2026-10-14 and skipped two days, one was killed by the shared session limit), so the first
thing to pin is that THIS thing cannot fail the same way: it sends once per trading day on a reliable trigger, it never calls an unreadable source
calm, and a delivery that reaches no channel makes the run red. Every guard is shown able to fail as well as pass.

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


def et(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=ET).astimezone(UTC)


def run(rid, updated):
    return {"id": rid, "updated_at": updated}


class WhenItSends(unittest.TestCase):
    TUE = (2026, 10, 13)            # ordinary trading day: bot closes 15:55, digest due 16:05 ET

    def test_not_on_a_weekend_or_holiday(self):
        self.assertFalse(D.should_send("workflow_run", et(2026, 10, 17, 17, 0), [], "1")[0])        # Saturday
        self.assertFalse(D.should_send("workflow_run", et(2026, 11, 26, 17, 0), [], "1")[0])        # Thanksgiving

    def test_not_before_the_bots_own_close_plus_ten_minutes(self):
        self.assertFalse(D.should_send("workflow_run", et(*self.TUE, 16, 4), [run(1, et(*self.TUE, 16, 4))], "1")[0])
        self.assertTrue(D.should_send("workflow_run", et(*self.TUE, 16, 6), [run(1, et(*self.TUE, 16, 6))], "1")[0])

    def test_the_first_run_to_finish_after_the_close_sends(self):
        runs = [run(1, et(*self.TUE, 15, 50)), run(2, et(*self.TUE, 16, 8))]
        ok, why = D.should_send("workflow_run", et(*self.TUE, 16, 8), runs, "2")
        self.assertTrue(ok, why)

    def test_a_later_run_stays_quiet_because_an_earlier_one_already_sent(self):
        runs = [run(2, et(*self.TUE, 16, 8)), run(3, et(*self.TUE, 16, 23))]
        ok, why = D.should_send("workflow_run", et(*self.TUE, 16, 23), runs, "3")
        self.assertFalse(ok, why)

    def test_a_run_that_finished_before_the_threshold_does_not_send_even_if_announced_late(self):
        runs = [run(1, et(*self.TUE, 16, 2))]
        ok, why = D.should_send("workflow_run", et(*self.TUE, 16, 7), runs, "1")
        self.assertFalse(ok, why)

    def test_an_api_that_lags_the_event_still_sends_for_the_first_run(self):
        ok, why = D.should_send("workflow_run", et(*self.TUE, 16, 9), [], "9")
        self.assertTrue(ok, why)

    def test_workflow_run_without_a_trigger_id_never_guesses(self):
        self.assertFalse(D.should_send("workflow_run", et(*self.TUE, 16, 9), [], None)[0])

    def test_early_close_day_is_due_at_1305_not_1605(self):
        fri = (2026, 11, 27)
        self.assertTrue(D.should_send("workflow_run", et(*fri, 13, 6), [run(1, et(*fri, 13, 6))], "1")[0])
        self.assertFalse(D.should_send("workflow_run", et(*fri, 13, 4), [run(1, et(*fri, 13, 4))], "1")[0])
        self.assertFalse(D.should_send("workflow_run", et(*self.TUE, 13, 6), [run(1, et(*self.TUE, 13, 6))], "1")[0])   # control: same clock, ordinary day

    def test_the_backup_schedule_sends_only_when_the_primary_path_had_nothing(self):
        late = et(*self.TUE, 17, 30)
        self.assertTrue(D.should_send("schedule", late, [run(1, et(*self.TUE, 15, 50))])[0])               # nothing finished after the close
        self.assertFalse(D.should_send("schedule", late, [run(2, et(*self.TUE, 16, 8))])[0])                # the primary path fired
        self.assertFalse(D.should_send("schedule", et(*self.TUE, 16, 20), [])[0])                          # too early for a backup

    def test_force_always_sends_even_on_a_saturday(self):
        self.assertTrue(D.should_send("workflow_dispatch", et(2026, 10, 17, 10, 0), [], force=True)[0])


def facts(**kw):
    f = {"date": "2026-10-13", "weekday": "Tue",
         "runs": {"by_name": {"Alpaca Trading Bot": {"total": 28, "bad": []}}, "bad": []}, "ci": "success",
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

    def test_subject_and_body_are_plain_ascii_with_no_em_dash(self):
        _, _, subject, body = D.build_report(facts())
        self.assertTrue((subject + body).isascii())
        self.assertNotIn("—", subject + body)

    def test_a_failed_run_is_a_flag(self):
        f = self.flags(runs={"by_name": {"ci": {"total": 1, "bad": ["failure"]}}, "bad": [("ci", "failure", "14:15")]})
        self.assertTrue(any("FAILURE" in x and "ci" in x for x in f), f)

    def test_red_ci_is_a_flag(self):
        self.assertTrue(any("CI latest run is failure" in x for x in self.flags(ci="failure")))

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


class RunOnce(unittest.TestCase):
    NOW = et(2026, 10, 13, 16, 8)

    def raw(self):
        return [{"name": "Alpaca Trading Bot", "created_at": "2026-10-13T20:00:09Z", "updated_at": "2026-10-13T20:08:00Z",
                 "run_started_at": "2026-10-13T20:00:10Z", "status": "completed", "conclusion": "success", "id": 77}]

    def go(self, deliver, **kw):
        os.chdir(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            return D.run_once(now=self.NOW, event="workflow_run", trigger_run_id="77", raw=self.raw(), deliver=deliver, **kw)

    def test_nothing_is_delivered_when_it_is_not_due(self):
        calls = []
        rc, line = D.run_once(now=et(2026, 10, 13, 12, 0), event="workflow_run", trigger_run_id="77", raw=self.raw(),
                              deliver=lambda s, b: calls.append(s))
        self.assertEqual((rc, calls), (0, []))
        self.assertIn("not sending", line)

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
        os.chdir(ROOT)
        with contextlib.redirect_stdout(io.StringIO()):
            rc, line = D.run_once(now=self.NOW, event="workflow_run", trigger_run_id="77", raw=None, deliver=lambda s, b: (got.append(b), ["slack"])[1])
        self.assertEqual(rc, 0)
        self.assertIn("COULD NOT READ today's workflow runs", got[0])


class Wiring(unittest.TestCase):
    def wf(self):
        with open(os.path.join(ROOT, ".github", "workflows", "daily-digest.yml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_the_primary_trigger_is_the_arm_a_workflow_with_a_schedule_backup_and_a_manual_test(self):
        on = self.wf().get("on") or self.wf().get(True)
        self.assertEqual(on["workflow_run"]["workflows"], ["Alpaca Trading Bot"])
        self.assertIn("schedule", on)
        self.assertEqual(on["workflow_dispatch"]["inputs"]["force"]["options"], ["no", "yes"])

    def test_it_passes_the_trigger_id_the_force_flag_and_the_secrets_and_never_swallows_a_failure(self):
        step = [s for s in self.wf()["jobs"]["digest"]["steps"] if "daily_digest.py" in str(s.get("run", ""))][0]
        for k in ("TRIGGER_RUN_ID", "FORCE_DIGEST", "GMAIL_USER", "GMAIL_APP_PASSWORD", "ALERT_EMAIL", "SLACK_WEBHOOK_URL", "GH_TOKEN"):
            self.assertIn(k, step["env"])
        self.assertNotIn("|| true", step["run"])
        self.assertNotIn("concurrency", self.wf())               # a concurrency group once silently dropped a pending run

    def test_the_checkout_is_deep_enough_to_read_the_laptops_status_history(self):
        co = [s for s in self.wf()["jobs"]["digest"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout")][0]
        self.assertGreaterEqual(int(co["with"]["fetch-depth"]), 200)

    def test_permissions_are_read_only(self):
        self.assertEqual(self.wf()["permissions"], {"contents": "read", "actions": "read"})


if __name__ == "__main__":
    unittest.main()
