"""REPLAY CORPUS for the QUEUED signal of arm_a_watch.py, and the git corroboration added after 2026-11-04.

WHY THIS FILE EXISTS (CLAUDE.md, EVIDENCE RULES 2, 3 and 6). On 2026-11-04 at 11:11 ET a first-level "Arm A is late" fired 32
seconds after the run had COMPLETED: the Actions API (run row and jobs endpoint) served stale state, and the queued signal had no
second source. Nothing had ever replayed that signal against the episodes on record. Every row in tests/arm_a_replay_data.py is an
instant from GitHub's own records (the run list, the jobs endpoint, the status.json git history) or from LAPTOP's logged evidence,
and each is run through the REAL run_once with only the I/O stubbed. Every row has a CONTROL that flips its single deciding input
and must give the opposite answer, so a test that can only say "no alert" cannot pass by accident.

WHAT THE CORROBORATION IS. status.json is committed by the pass that wrote it, and carries ts = when that pass STARTED. A queued run
R is not waiting for a machine if the newest commit holds a pass that started at or after R was created (the workflow serialises
passes, so that pass is R or later). It is NOT a commit age (a healthy pass and the 2026-10-05 stuck run look identical by age at six
minutes: LAPTOP's 13 minute guard moved that detection from 6.8 to 9.8 minutes) and NOT "any commit since" (a slow predecessor's
commit would hide a run that is really still waiting: 10-26 row "c-after").

WHAT IT DOES NOT COVER, pinned below so nobody believes otherwise: a stale read while the pass is still RUNNING (wait 6 to about 10.5
minutes, before the pass's own commit exists) has no git evidence. The five 2026-10-22 mails were that shape.

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import copy
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import arm_a_replay_data as R  # noqa: E402
import arm_a_watch as A  # noqa: E402

UTC = timezone.utc
QUEUED_TEXT = "give it a machine"                      # the sentence of a queued alert (rh_daemon's mute matches on it too)
NO_TRIGGER_TEXT = "No Arm A run has been triggered"


def T(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def et_stamp(utc_dt):
    """status.json's ts format for a pass that started at utc_dt."""
    return utc_dt.astimezone(A.bot.ET_TZ).strftime("%Y-%m-%dT%H:%M ET")


def jobs_payload(kind):
    """What the jobs endpoint answered, built from the REAL recorded payloads."""
    if kind == "none":
        return {"jobs": []}
    if kind == "started":
        return {"jobs": [copy.deepcopy(R.JOBS_RAN)]}
    if kind == "never_ran":
        # DERIVED: the recorded job as it stood BEFORE its 19:45:12 cancellation. runner_id 0 and no steps are what the real payload
        # carries throughout; the status word is reset to what it was while the run waited (the recorded one is the final, cancelled state).
        j = copy.deepcopy(R.JOBS_NEVER_RAN)
        j.update(status="queued", conclusion=None, completed_at=None)
        return {"jobs": [j]}
    if kind in ("stale_queued", "stale_queued_with_steps"):
        # 2026-11-04: "job status queued, no completed_at" (LAPTOP, 11:16:37). DERIVED from the real payload of that job: whether the
        # stale read carried steps is NOT recorded, so both are exercised.
        j = copy.deepcopy(R.JOBS_RAN)
        j.update(status="queued", conclusion=None, completed_at=None)
        if kind == "stale_queued":
            j.update(steps=[], runner_id=0, runner_name="")
        return {"jobs": [j]}
    raise ValueError(kind)


class Harness(unittest.TestCase):
    """The real run_once, real job_started on a stubbed _get_json, real reconcile / corroborate / measure / crossings / build_alert."""

    def check(self, row, commit=None, status_ts="row", jobs="row", exact=True, now=None, fired=None, runs=None, status=None):
        commit = T(row["commit"]) if commit is None else commit
        ts = row["status_ts"] if status_ts == "row" else status_ts
        status = status if status is not None else ({"ts": ts} if ts else None)
        runs = copy.deepcopy(row["runs"] if runs is None else runs)
        answers = row["jobs"] if jobs == "row" else jobs
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}

        def get_json(url, token=None):
            rid = int(url.split("/runs/")[1].split("/")[0])
            kind = answers.get(str(rid), answers.get(rid)) if isinstance(answers, dict) else answers
            if kind == "unreadable":
                raise OSError("jobs endpoint unreadable")
            return jobs_payload(kind or "none"), {"Date": "recorded"}

        sent = []
        fired = ({} if exact else None) if fired is None else fired
        with mock.patch.object(A, "_get_json", get_json), contextlib.redirect_stdout(io.StringIO()):
            rc, line, out = A.run_once(
                now=now or T(row["now"]), fired=fired, deliver=lambda s, b, u: sent.append((s, b, u)) or True,
                get_commit_time=lambda ref: commit, get_status=lambda ref: status,
                get_runs=lambda: [dict(r, created_at=T(r["created_at"]), conclusion=None) for r in runs])
        return {"rc": rc, "line": line, "sent": sent, "bodies": [b for _, b, _ in sent], "fired": fired}

    def queued_alerts(self, res):
        return [b for b in res["bodies"] if QUEUED_TEXT in b]


ROWS = {r["id"]: r for r in R.ROWS}
TRUE_ALERTS = [r for r in R.ROWS if r["expect"].get("queued_first_level") is True]
NO_TRIGGER = [r for r in R.ROWS if r["expect"].get("no_trigger")]


def waiting_run(row):
    return [r for r in row["runs"] if r["status"] in A.QUEUED_STATES][-1]


class TrueAlertsStillAlert(Harness):
    """10-26: three alerts the mute swallowed, all TRUE (no job listed for 6 to 8 minutes), and the instant after the slow predecessor
    committed. The corroboration must leave every one of them standing, in both the exact (laptop) and stateless (GitHub) modes."""

    def test_every_true_row_alerts_at_the_first_level(self):
        self.assertEqual([r["id"] for r in TRUE_ALERTS], ["10-26 a", "10-26 b", "10-26 c", "10-26 c-after"])
        for row in TRUE_ALERTS:
            for exact in (True, False):
                with self.subTest(row=row["id"], exact=exact):
                    res = self.check(row, exact=exact)
                    self.assertEqual(len(self.queued_alerts(res)), 1, res["line"])
                    self.assertNotIn("git shows", res["line"])

    def test_the_wait_is_the_recorded_wait_not_a_shortened_one(self):
        waits = {"10-26 a": 7, "10-26 b": 6, "10-26 c": 8}          # LAPTOP's logged "waited N min"
        for rid, minutes in waits.items():
            row = ROWS[rid]
            created = T(waiting_run(row)["created_at"])
            self.assertEqual(int((T(row["now"]) - created).total_seconds() // 60), minutes, rid)

    def test_control_the_same_instant_is_suppressed_when_the_waiting_runs_own_pass_has_committed(self):
        """Each true row, with ONE input changed: the newest commit is the waiting run's own pass. It must then be suppressed, so the
        rows above are alerts because of the data and not because nothing can suppress."""
        for row in TRUE_ALERTS:
            with self.subTest(row=row["id"]):
                created = T(waiting_run(row)["created_at"])
                res = self.check(row, commit=T(row["now"]) - timedelta(seconds=30), status_ts=et_stamp(created + timedelta(seconds=3)))
                self.assertEqual(self.queued_alerts(res), [], res["line"])
                self.assertIn("git shows their pass had already committed", res["line"])

    def test_the_slow_predecessors_commit_does_not_hide_the_waiting_run(self):
        """The reason the proof is a pass START time and not 'a commit since': at 16:24:52 on 10-26 a commit (16:23:52) HAD landed after
        run 40357285384 was created (16:15:05), but it was the earlier pass (ts 12:13 ET = 16:13Z). The run was still waiting."""
        row = ROWS["10-26 c-after"]
        created = T(waiting_run(row)["created_at"])
        self.assertGreater(T(row["commit"]), created)                                  # a commit after creation exists
        self.assertLess(A.parse_status_ts({"ts": row["status_ts"]}), created.replace(second=0))    # but it is not this run's pass
        self.assertFalse(A.pass_started_after({"ts": row["status_ts"]}, T(row["commit"]), created, T(row["now"])))
        self.assertEqual(len(self.queued_alerts(self.check(row))), 1)


class FalseAlertsAreNotRepeated(Harness):
    def test_11_04_1111_is_suppressed_even_though_both_actions_endpoints_say_queued(self):
        """EVIDENCE RULE 2 as a test: the run row says queued and the jobs endpoint says queued (a stale read of a finished job), and
        the alert does NOT fire, because git shows the run's own pass committed 40 seconds earlier."""
        row = ROWS["11-04 11:11:14"]
        self.assertEqual(waiting_run(row)["status"], "queued")                          # the row, as the API stale-served it
        for exact in (True, False):
            with self.subTest(exact=exact):
                res = self.check(row, jobs={"41863263764": "stale_queued"}, exact=exact)        # jobs: queued, no steps, no runner
                self.assertEqual(res["bodies"], [], res["line"])
                self.assertEqual(res["rc"], 0)
                self.assertIn("git shows their pass had already committed", res["line"])        # git did it, not the jobs check

    def test_a_stale_job_read_that_carries_steps_is_caught_by_the_jobs_check_before_git_is_needed(self):
        """The other possible shape of the 11-04 read (status queued, steps and runner present). Not recorded either way, so both exist."""
        res = self.check(ROWS["11-04 11:11:14"], jobs={"41863263764": "stale_queued_with_steps"})
        self.assertEqual(res["bodies"], [], res["line"])
        self.assertIn("their job was already running", res["line"])

    def test_the_summary_line_names_the_run_and_the_git_facts_that_decided_it(self):
        """RULE 4: the correction is not invisible. The laptop logs this line on a non-zero exit and GitHub prints it."""
        res = self.check(ROWS["11-04 11:11:14"], jobs={"41863263764": "stale_queued"})
        self.assertIn("run 41863263764 created 16:00:08Z", res["line"])
        self.assertIn("newest status.json committed 16:10:34Z", res["line"])
        self.assertIn("its pass started 16:00Z", res["line"])
        res["line"].encode("ascii")

    def test_control_one_minute_earlier_the_same_run_IS_alerted(self):
        """The same stale answers with the commit state as it stood before 16:10:34 (the PREVIOUS pass, ts 10:45 ET): alert. So the
        suppression above is the commit's doing."""
        row = ROWS["11-04 11:11:14"]
        res = self.check(row, jobs={"41863263764": "stale_queued"}, commit=T("2026-11-04T15:55:27Z"), status_ts="2026-11-04T10:45 ET")
        self.assertEqual(len(self.queued_alerts(res)), 1, res["line"])

    def test_an_honest_started_job_is_still_enough_on_its_own(self):
        """The jobs check is unchanged: no git proof needed when the jobs endpoint is right (the 10-22 fix)."""
        row = ROWS["11-04 11:11:14"]
        res = self.check(row, jobs={"41863263764": "started"}, commit=T("2026-11-04T15:55:27Z"), status_ts="2026-11-04T10:45 ET")
        self.assertEqual(res["bodies"], [])
        self.assertIn("their job was already running", res["line"])

    def test_the_five_10_22_rows_are_suppressed_by_the_jobs_check_not_by_git(self):
        rows = [r for r in R.ROWS if r["id"].startswith("10-22")]
        self.assertEqual(len(rows), 5)
        for row in rows:
            with self.subTest(row=row["id"]):
                res = self.check(row)
                self.assertEqual(res["bodies"], [], res["line"])
                self.assertIn("their job was already running", res["line"])
                self.assertNotIn("git shows", res["line"])

    def test_KNOWN_RESIDUAL_a_stale_read_while_the_pass_is_still_running_has_no_git_evidence(self):
        """The class git cannot fix, pinned so nobody thinks it can. The 10-22 rows with the jobs endpoint UNREADABLE (or lying) alert,
        because each fell at wait 6.3 to 10.0 minutes, before that pass's own commit exists. If this ever starts passing as a
        suppression, something started inventing evidence: read it before celebrating."""
        for row in (r for r in R.ROWS if r["id"].startswith("10-22")):
            with self.subTest(row=row["id"]):
                res = self.check(row, jobs="unreadable")
                self.assertEqual(len(self.queued_alerts(res)), 1, res["line"])
                self.assertNotIn("git shows", res["line"])
                self.assertLess((T(row["now"]) - T(waiting_run(row)["created_at"])).total_seconds() / 60, 10.5)


class NoTriggerRowsAreUntouched(Harness):
    def test_the_missing_trigger_rows_alert_on_no_trigger(self):
        self.assertEqual([r["id"] for r in NO_TRIGGER], ["11-04 10:24:34", "10-23 12:37:35"])
        for row in NO_TRIGGER:
            for exact in (True, False):
                with self.subTest(row=row["id"], exact=exact):
                    res = self.check(row, exact=exact)
                    self.assertEqual(len(res["bodies"]), 1, res["line"])
                    self.assertIn(NO_TRIGGER_TEXT, res["bodies"][0])

    def test_a_stale_queued_row_beside_a_missing_trigger_is_still_reported_because_it_is_at_the_urgent_level(self):
        """11-04 10:24:34: which signal fired is not recorded. If the API ALSO said the 15:00 run was queued, that row is 24 minutes old
        (a run old enough to sit beside a no_trigger alert is always past the 20 minute URGENT threshold), and the urgent level is judged
        on the uncorrected runs BY DESIGN (Devon's brief: urgent, no_trigger and stale are untouched). So git proof (its pass ts 10:00 ET,
        committed 15:10:56) does not hide it, and the alert says so. This is the stated residual: a stale 'queued' row that outlives
        20 minutes pages as urgent."""
        row = ROWS["11-04 10:24:34"]
        runs = copy.deepcopy(row["runs"])
        runs[-1]["status"] = "queued"
        res = self.check(row, runs=runs, jobs={str(runs[-1]["id"]): "stale_queued"})
        self.assertEqual(len(res["sent"]), 1, res["line"])
        subject, body, urgent = res["sent"][0]
        self.assertTrue(urgent)
        self.assertIn(NO_TRIGGER_TEXT, body)
        self.assertIn(QUEUED_TEXT, body)
        self.assertIn("Reported anyway because the wait reached the urgent level", body)

    def test_the_same_stale_row_inside_the_first_level_range_does_not_add_a_queued_line(self):
        """Control for the test above: a stale queued row only 9 minutes old, next to a (separately caused) missing trigger, is corrected
        by git, so the alert carries the no_trigger line alone."""
        row = ROWS["11-04 10:24:34"]
        now = T(row["now"])
        runs = copy.deepcopy(row["runs"])
        runs[-1] = dict(runs[-1], status="queued", created_at=(now - timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        # the run was created 9 minutes ago, so its own pass (ts) is the one at 10:15 ET = 15:15Z; commit it 30 s ago
        res = self.check(row, runs=runs, jobs={str(runs[-1]["id"]): "stale_queued"}, commit=now - timedelta(seconds=30),
                         status_ts=et_stamp(now - timedelta(minutes=9)))
        self.assertEqual(res["bodies"], [], res["line"])     # no_trigger is measured from the newest created run (9 min), so nothing fires
        # and the control that lets "nothing fires" mean something: the SAME runs and jobs with the row's own (older) status.json alert
        again = self.check(row, runs=runs, jobs={str(runs[-1]["id"]): "stale_queued"})
        self.assertEqual(len(self.queued_alerts(again)), 1, again["line"])

    def test_control_a_run_created_on_time_removes_the_alert(self):
        row = ROWS["10-23 12:37:35"]
        runs = copy.deepcopy(row["runs"]) + [{"id": 1, "status": "in_progress", "created_at": "2026-10-23T16:30:08Z"}]
        self.assertEqual(self.check(row, runs=runs)["bodies"], [])


class October5(Harness):
    """The recorded 2026-10-05 stuck runs with the REAL status.json of the last good commit (ts 15:16 ET). The older replay in
    test_arm_a_watch.py passes get_status=None, so it cannot exercise the corroboration; this one can."""

    def runs_at(self, now):
        rows = []
        for rid, created, updated, conclusion in R.OCT5["runs"]:
            c, u = T(created), T(updated)
            if c > now:
                continue
            bad = c.minute in (30, 45)
            rows.append({"id": rid, "status": ("queued" if bad else "in_progress") if now < u else "completed", "created_at": created})
        return rows

    def first_alert(self, status_ts=None, commit=None, step=60):
        now = datetime(2026, 10, 5, 19, 28, tzinfo=UTC)
        fired = {}
        row = {"commit": R.OCT5["commit"], "status_ts": status_ts or R.OCT5["status_ts"], "runs": [], "jobs": {}, "now": ""}
        while now <= datetime(2026, 10, 5, 19, 55, tzinfo=UTC):
            res = self.check(row, commit=commit, status_ts=status_ts or R.OCT5["status_ts"], jobs="never_ran", fired=fired,
                             now=now, runs=self.runs_at(now))
            if res["sent"]:
                return now, res
            now += timedelta(seconds=step)
        return None, None

    def test_the_recorded_status_does_not_prove_the_stuck_run_ran(self):
        self.assertEqual(R.OCT5["status_ts"], "2026-10-05T15:16 ET")
        self.assertFalse(A.pass_started_after({"ts": R.OCT5["status_ts"]}, T(R.OCT5["commit"]), T("2026-10-05T19:30:09Z"),
                                              T("2026-10-05T19:36:56Z")))

    def test_it_alerts_between_six_and_seven_minutes_with_the_real_status_and_the_real_never_ran_job(self):
        when, res = self.first_alert()
        self.assertIsNotNone(when, "never alerted before the close")
        waited = (when - T("2026-10-05T19:30:09Z")).total_seconds() / 60
        self.assertGreaterEqual(waited, 6.0)
        self.assertLess(waited, 7.0)
        self.assertEqual(len(self.queued_alerts(res)), 1)
        self.assertNotIn("git shows", res["line"])

    def test_control_the_same_replay_is_suppressed_if_a_pass_started_after_the_stuck_run_had_committed(self):
        """Give the 19:30:09 run a commit it could never have had (ts 15:30 ET, committed 19:31). Its first-level alert (19:36:56 above)
        must disappear. The first alert left is the NEXT stuck run's (created 19:45:07, whose pass stamp 19:30 is older than it), at six
        minutes. In the real timeline the 19:30 run was cancelled at 19:45:12, before it could reach the 20 minute urgent level."""
        when, res = self.first_alert(status_ts="2026-10-05T15:30 ET", commit=T("2026-10-05T19:31:00Z"))
        self.assertIsNotNone(when)
        self.assertGreaterEqual((when - T("2026-10-05T19:45:07Z")).total_seconds() / 60, 6.0)
        self.assertIn("created at 15:45 ET", res["bodies"][0])

    def test_urgent_is_judged_on_the_uncorrected_runs_so_git_proof_cannot_hide_twenty_minutes(self):
        now = T("2026-10-05T19:50:30Z")                                               # 20.35 minutes after 19:30:09
        row = {"commit": "2026-10-05T19:31:00Z", "status_ts": "2026-10-05T15:30 ET", "runs": [], "jobs": {}, "now": ""}
        stuck = [{"id": 37363799207, "status": "queued", "created_at": "2026-10-05T19:30:09Z"}]      # the recorded stuck run
        res = self.check(row, jobs="never_ran", now=now, runs=stuck)
        urgent = [s for s in res["sent"] if s[2]]
        self.assertEqual(len(urgent), 1, res["line"])
        self.assertIn("Git side (not the Actions API)", urgent[0][1])
        self.assertIn("Reported anyway because the wait reached the urgent level", urgent[0][1])


class PassStartedAfter(unittest.TestCase):
    CREATED = T("2026-11-04T16:00:08Z")
    NOW = T("2026-11-04T16:11:14Z")
    COMMIT = T("2026-11-04T16:10:34Z")

    def f(self, ts="2026-11-04T11:00 ET", commit=COMMIT, created=CREATED, now=NOW, status="row"):
        st = {"ts": ts} if status == "row" else status
        return A.pass_started_after(st, commit, created, now)

    def test_the_runs_own_pass_proves_it(self):
        self.assertTrue(self.f())

    def test_a_pass_that_started_a_minute_before_the_run_was_created_does_not(self):
        self.assertFalse(self.f(ts="2026-11-04T10:59 ET"))
        self.assertFalse(self.f(ts="2026-11-04T10:45 ET"))

    def test_a_later_pass_also_proves_it(self):
        self.assertTrue(self.f(ts="2026-11-04T11:05 ET", commit=T("2026-11-04T16:11:00Z")))

    def test_every_missing_or_inconsistent_input_answers_false(self):
        self.assertFalse(self.f(status=None))
        self.assertFalse(self.f(status={}))
        self.assertFalse(self.f(status={"ts": "yesterday"}))
        self.assertFalse(self.f(commit=None))                                         # no git history (a shallow clone)
        self.assertFalse(A.pass_started_after({"ts": "2026-11-04T11:00 ET"}, self.COMMIT, None, self.NOW))
        self.assertFalse(self.f(ts="2026-11-04T11:20 ET"))                            # a start stamp later than its own commit
        self.assertFalse(self.f(ts="2026-11-04T11:00 ET", now=T("2026-11-04T15:50:00Z")))      # and one in the future

    def test_the_clock_slack_is_one_minute(self):
        self.assertTrue(self.f(ts="2026-11-04T11:11 ET", commit=T("2026-11-04T16:10:34Z")))    # 16:11 vs commit 16:10:34: within 1 min
        self.assertFalse(self.f(ts="2026-11-04T11:12 ET", commit=T("2026-11-04T16:10:34Z"), now=T("2026-11-04T16:13:00Z")))


class CorroborateAndMeasure(unittest.TestCase):
    NOW = T("2026-11-04T16:11:14Z")
    OPEN = T("2026-11-04T14:30:00Z")
    COMMIT = T("2026-11-04T16:10:34Z")
    STATUS = {"ts": "2026-11-04T11:00 ET"}

    def rows(self, status="queued", minutes=11.1):
        return [{"id": 9, "status": status, "created_at": self.NOW - timedelta(minutes=minutes)}]

    def test_only_queued_rows_are_touched_and_the_input_is_not_mutated(self):
        rows = self.rows() + [{"id": 10, "status": "completed", "created_at": self.NOW - timedelta(minutes=30)},
                              {"id": 11, "status": "in_progress", "created_at": self.NOW - timedelta(minutes=9)}]
        before = copy.deepcopy(rows)
        out, corrected = A.corroborate(rows, self.STATUS, self.COMMIT, self.NOW)
        self.assertEqual(rows, before)
        self.assertEqual([r["id"] for r in corrected], [9])
        self.assertEqual([r["status"] for r in out], ["in_progress", "completed", "in_progress"])
        self.assertEqual(out[0]["row_status"], "queued")
        self.assertEqual(out[0]["proof"], "status.json")

    def test_nothing_to_do_for_no_runs(self):
        self.assertEqual(A.corroborate(None, self.STATUS, self.COMMIT, self.NOW), (None, []))
        self.assertEqual(A.corroborate([], self.STATUS, self.COMMIT, self.NOW), ([], []))

    def test_measure_with_git_lowers_a_first_level_wait(self):
        sig, shown, corrected = A.measure_with_git(self.NOW, self.OPEN, self.COMMIT, self.STATUS, self.rows(minutes=11.1))
        self.assertEqual(sig["queued"], 0.0)
        self.assertEqual(len(corrected), 1)

    def test_measure_with_git_never_hides_the_urgent_wait(self):
        sig, shown, corrected = A.measure_with_git(self.NOW, self.OPEN, self.COMMIT, self.STATUS, self.rows(minutes=21.0))
        self.assertAlmostEqual(sig["queued"], 21.0, places=1)
        self.assertEqual(shown[0]["status"], "queued")                               # the alert is built from the uncorrected run
        self.assertEqual(len(corrected), 1)

    def test_no_git_leaves_measure_unchanged(self):
        for kw in ({"commit_utc": None, "status": self.STATUS}, {"commit_utc": self.COMMIT, "status": None}):
            with self.subTest(**{k: bool(v) for k, v in kw.items()}):
                sig, _, corrected = A.measure_with_git(self.NOW, self.OPEN, kw["commit_utc"], kw["status"], self.rows(minutes=8.0))
                self.assertAlmostEqual(sig["queued"], 8.0, places=1)
                self.assertEqual(corrected, [])


class JobsPayloads(unittest.TestCase):
    """job_started on the REAL recorded payloads (tests/arm_a_replay_data.py)."""

    def started(self, payload):
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}
        with mock.patch.object(A, "_get_json", lambda url, token=None: (payload, {})):
            return A.job_started(1)

    def test_the_job_that_never_got_a_machine_is_not_started_even_though_it_carries_started_at_completed_at_and_a_conclusion(self):
        j = R.JOBS_NEVER_RAN
        self.assertEqual((j["runner_id"], j["steps"], j["status"], j["conclusion"]), (0, [], "completed", "cancelled"))
        self.assertEqual(j["started_at"], j["created_at"])                           # the trap: started_at is set without a machine
        self.assertTrue(j["completed_at"])
        self.assertFalse(A._job_ran(dict(j, status="queued")))                       # status word aside, nothing proves a machine ran
        self.assertFalse(A._job_ran(dict(j, status="queued", conclusion=None, completed_at=None)))

    def test_jobs_that_ran_are_started(self):
        for j in (R.JOBS_RAN, R.JOBS_RAN_12):
            self.assertTrue(self.started({"jobs": [j]}))
            self.assertGreater(j["runner_id"], 0)
            self.assertGreaterEqual(len(j["steps"]), 11)

    def test_a_status_that_lags_its_own_payload_does_not_hide_a_job_that_ran(self):
        """The 11-04 self-contradiction (status behind, steps/runner ahead). Not seen for a QUEUED status in any recorded read; the
        in_progress case LAPTOP exhibited was already counted as started."""
        lagged = dict(R.JOBS_RAN, status="queued", conclusion=None, completed_at=None)
        self.assertTrue(self.started({"jobs": [lagged]}))
        self.assertTrue(self.started({"jobs": [dict(lagged, steps=[])]}))               # runner assigned alone
        self.assertTrue(self.started({"jobs": [dict(lagged, runner_id=0, runner_name="")]}))   # steps alone
        self.assertFalse(self.started({"jobs": [dict(lagged, steps=[], runner_id=0, runner_name="")]}))   # neither: still waiting

    def test_no_job_listed_stays_unanswered_because_that_is_what_the_true_10_26_runs_returned(self):
        self.assertIsNone(self.started({"jobs": []}))

    def test_the_evidence_records_whether_steps_and_a_runner_were_present(self):
        self.started({"jobs": [dict(R.JOBS_RAN, status="queued")]})
        ev = A.EVIDENCE["jobs"][1]
        self.assertEqual(ev["statuses"], ["queued"])
        self.assertEqual(ev["runner"], [True])
        self.assertEqual(ev["steps_ran"], [len(R.JOBS_RAN["steps"])])
        text = A.evidence_text([{"id": 1, "status": "queued", "created_at": T("2026-11-04T16:00:08Z")}])
        self.assertIn("jobs ['queued'] (steps that ran [%d], runner assigned [True])" % len(R.JOBS_RAN["steps"]), text)


class EvidenceText(unittest.TestCase):
    RUN = [{"id": 77, "status": "queued", "created_at": T("2026-11-04T16:00:08Z")}]

    def setUp(self):
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}

    def test_three_git_sentences_each_true_in_its_own_case(self):
        commit, started = T("2026-11-04T15:55:27Z"), T("2026-11-04T15:45:00Z")
        plain = A.evidence_text(self.RUN, {"commit": commit, "pass_started": started})
        self.assertIn("newest status.json commit 15:55:27 UTC, its pass started 15:45 UTC; no pass that started after the waiting run was created has committed", plain)
        blind = A.evidence_text(self.RUN, {"commit": None, "pass_started": None})
        self.assertIn("status.json history unreadable here, so nothing could corroborate", blind)
        held = A.evidence_text(self.RUN, {"commit": commit, "pass_started": started, "ran_ids": {77}})
        self.assertIn("which is after run(s) 77 were created", held)
        self.assertNotIn("no pass that started after the waiting run was created has committed", held)       # it would be false here
        for text in (plain, blind, held):
            text.encode("ascii")

    def test_without_git_the_text_is_exactly_what_it_was_before(self):
        self.assertNotIn("Git side", A.evidence_text(self.RUN))

    def test_the_mute_marker_sentence_is_intact_and_the_git_text_comes_after_it(self):
        c = {"signal": "queued", "threshold": 6, "minutes": 7.0, "urgent": False}
        body = A.build_alert([c], T("2026-11-04T16:08:00Z"), self.RUN, "LAPTOP BOT", {"commit": T("2026-11-04T15:55:27Z"),
                                                                                       "pass_started": T("2026-11-04T15:45:00Z")})[1]
        self.assertEqual(body.count(QUEUED_TEXT), 1)
        self.assertGreater(body.index("Git side"), body.index("Evidence"))
        self.assertGreater(body.index("Evidence"), body.index(QUEUED_TEXT))

    def test_only_the_queued_signal_carries_evidence(self):
        for sig in ("no_trigger", "stale"):
            text = A.describe({"signal": sig, "threshold": 22, "minutes": 30.0, "urgent": False}, T("2026-11-04T16:08:00Z"), self.RUN,
                              {"commit": T("2026-11-04T15:55:27Z"), "pass_started": T("2026-11-04T15:45:00Z")})
            self.assertNotIn("Evidence", text)
            self.assertNotIn("Git side", text)


class ExactModeRecovers(Harness):
    def test_an_alert_that_went_out_is_followed_by_a_recovery_notice_once_git_shows_the_pass_ran(self):
        row = ROWS["10-26 a"]
        fired = {}
        first = self.check(row, fired=fired)
        self.assertEqual(len(self.queued_alerts(first)), 1)
        created = T(waiting_run(row)["created_at"])
        later = T(row["now"]) + timedelta(minutes=5)
        second = self.check(row, fired=fired, now=later, commit=later - timedelta(seconds=20), status_ts=et_stamp(created + timedelta(seconds=3)),
                            jobs={"40337891093": "none"})
        self.assertEqual(self.queued_alerts(second), [])
        self.assertTrue(any("running again" in s for s, _, _ in second["sent"]), second["line"])


if __name__ == "__main__":
    unittest.main()
