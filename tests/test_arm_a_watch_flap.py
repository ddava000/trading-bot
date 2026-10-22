"""ARM A WATCH, 2026-10-22: three false "Arm A is late" alerts, and nobody could tell who sent them.

At 10:23, 10:40 ET the watch (run by the laptop daemon) emailed and posted "Arm A is late"; at 10:29 "running again".
Arm A was never late: every one of the 8 runs from 13:00Z to 14:45Z has a job that started 2 to 3 seconds after it was
created. GitHub had an "Actions Results" incident open from 14:18Z, and the run ROW the watch reads said queued for 8 to
10 minutes. The question the QUEUED signal asks is "is a machine working on this run", which the JOB answers.

What these tests pin:
  * a run whose row says queued but whose job has started is not counted, and the summary line says so;
  * a run whose job is still queued, or whose job cannot be read, still alerts (the check must be able to say yes);
  * the 2026-10-05 stuck-run timeline still alerts about six minutes in (the reason the 6 and 20 stay);
  * a first-level queued alert is read again just before delivery and withdrawn if nothing is waiting any more;
  * the subject says who checked: GitHub on a runner, LAPTOP BOT otherwise (Devon could not tell, and thought a third
    session existed).

Run:  python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arm_a_watch as A  # noqa: E402

UTC = timezone.utc


def at(h, m, s=0, day=22):
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def row(created, status, rid, conclusion=None):
    return {"id": rid, "status": status, "conclusion": conclusion, "created_at": created}


COMMIT = at(14, 10)                      # status.json last committed 14:10Z, as on the morning of 10-22


class Check:
    """One run_once with every input injected and delivery recorded."""
    def __init__(self, now, runs_seq, jobs, fired=None, deliver_ok=True, sender="LAPTOP BOT", commit=COMMIT):   # explicit: "auto" follows the environment, and CI is GitHub
        self.now, self.fired, self.sent, self.bodies, self.reads = now, fired, [], [], 0
        seq = list(runs_seq) if isinstance(runs_seq, list) and runs_seq and isinstance(runs_seq[0], list) else [runs_seq]

        def get_runs():
            self.reads += 1
            return seq[min(self.reads - 1, len(seq) - 1)]

        def deliver(subject, body, urgent):
            self.sent.append((subject, urgent))
            self.bodies.append(body)
            return deliver_ok
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.rc, self.line, _ = A.run_once(
                now=now, deliver=deliver, fired=fired, get_commit_time=lambda ref: commit,
                get_status=lambda ref: None, get_runs=get_runs, sender=sender,
                get_job_state=jobs if callable(jobs) else (lambda rid: jobs))


class TheTenTwentyTwoFlap(unittest.TestCase):
    NOW = at(14, 23, 48)
    RUNS = [row(at(14, 15, 5), "queued", 1), row(at(14, 0, 12), "completed", 0, "success")]   # 8.7 minutes, as at 10:23:48 ET

    def test_a_row_that_says_queued_while_the_job_runs_sends_nothing_and_says_why(self):
        c = Check(self.NOW, self.RUNS, jobs=True)
        self.assertEqual((c.rc, c.sent), (0, []))
        self.assertIn("1 run row(s) said queued while their job was already running", c.line)
        self.assertIn("longest wait for a machine 0m", c.line)

    def test_control_the_same_row_with_the_job_still_queued_alerts(self):
        c = Check(self.NOW, self.RUNS, jobs=False)
        self.assertEqual([s for s, _ in c.sent], ["ARM A WATCH: Arm A is late (checked by LAPTOP BOT)"])

    def test_control_a_job_that_cannot_be_read_keeps_the_alert(self):
        c = Check(self.NOW, self.RUNS, jobs=None)
        self.assertEqual(len(c.sent), 1, "an unreadable job must never be read as calm")

    def test_control_a_job_check_that_raises_keeps_the_alert(self):
        def boom(rid):
            raise RuntimeError("api down")
        c = Check(self.NOW, self.RUNS, jobs=boom)
        self.assertEqual(len(c.sent), 1)

    def test_the_later_alerts_of_the_morning_are_also_silent_when_the_job_ran(self):
        for now, created in ((at(14, 40, 9), at(14, 30, 9)), (at(14, 45, 33), at(14, 30, 9))):
            c = Check(now, [row(created, "queued", 2)], jobs=True, commit=at(14, 25))
            self.assertEqual(c.sent, [], now)


class TheTenFiveReplayStillAlerts(unittest.TestCase):
    """The stuck runs of 2026-10-05: the job never started. Real timeline, as in test_arm_a_watch.October5Replay."""
    LAST_COMMIT = datetime(2026, 10, 5, 19, 26, 53, tzinfo=UTC)
    STUCK = datetime(2026, 10, 5, 19, 30, 9, tzinfo=UTC)

    def runs_at(self, now):
        r = [row(datetime(2026, 10, 5, 19, 15, 7, tzinfo=UTC), "completed", 10, "success")]
        if now >= self.STUCK:
            r.append(row(self.STUCK, "queued", 11))
        return r

    def first_alert(self, jobs, until=datetime(2026, 10, 5, 19, 55, tzinfo=UTC)):
        now, fired = datetime(2026, 10, 5, 19, 28, tzinfo=UTC), {}
        while now <= until:
            sent = []
            with contextlib.redirect_stdout(io.StringIO()):
                A.run_once(now=now, deliver=lambda s, b, u: sent.append((s, u)) or True, fired=fired,
                           get_commit_time=lambda ref: self.LAST_COMMIT, get_status=lambda ref: None,
                           get_runs=lambda n=now: self.runs_at(n), get_job_state=lambda rid: jobs)
            if sent:
                return now, sent
            now += timedelta(seconds=60)
        return None, []

    def test_a_run_whose_job_never_started_still_alerts_about_six_minutes_in(self):
        when, sent = self.first_alert(jobs=False)
        waited = (when - self.STUCK).total_seconds() / 60
        self.assertGreaterEqual(waited, 6.0)
        self.assertLess(waited, 7.0)
        self.assertEqual(len(sent), 1)

    def test_the_same_timeline_with_the_job_running_never_alerts_on_the_queue_signal(self):
        # until 19:52:00: after that the no-trigger signal (22 minutes since the last run was created) speaks for itself
        when, sent = self.first_alert(jobs=True, until=datetime(2026, 10, 5, 19, 51, 59, tzinfo=UTC))
        self.assertIsNone(when, "the queue signal must not fire for a run whose job is running: %r" % (sent,))


class TheSecondLook(unittest.TestCase):
    NOW = at(14, 37)

    def waiting(self, rid=1, minutes=7):
        return [row(self.NOW - timedelta(minutes=minutes), "queued", rid)]

    def started(self, rid=1, minutes=7):
        return [row(self.NOW - timedelta(minutes=minutes), "in_progress", rid)]

    def test_an_alert_is_withdrawn_when_the_run_has_started_by_the_second_read(self):
        c = Check(self.NOW, [self.waiting(), self.started()], jobs=False)
        self.assertEqual((c.rc, c.sent), (0, []))
        self.assertIn("queued alert withdrawn", c.line)
        self.assertEqual(c.reads, 2)

    def test_control_a_run_still_waiting_on_the_second_read_alerts(self):
        c = Check(self.NOW, [self.waiting(), self.waiting()], jobs=False)
        self.assertEqual(len(c.sent), 1)
        self.assertNotIn("withdrawn", c.line)

    def test_control_an_unreadable_second_read_keeps_the_alert(self):
        c = Check(self.NOW, [self.waiting(), None], jobs=False)
        self.assertEqual(len(c.sent), 1)

    def test_a_withdrawn_alert_does_not_mark_the_threshold_as_fired(self):
        fired = {}
        c = Check(self.NOW, [self.waiting(), self.started()], jobs=False, fired=fired)
        self.assertEqual(c.sent, [])
        self.assertNotIn(6, fired.get("queued", set()))
        again = Check(self.NOW + timedelta(minutes=1), self.waiting(minutes=8), jobs=False, fired=fired)
        self.assertEqual(len(again.sent), 1, "a run that is still waiting a minute later must alert")

    def test_exact_mode_still_alerts_once_per_episode(self):
        fired = {}
        first = Check(self.NOW, self.waiting(), jobs=False, fired=fired)
        second = Check(self.NOW + timedelta(minutes=5), self.waiting(minutes=12), jobs=False, fired=fired)
        self.assertEqual((len(first.sent), len(second.sent)), (1, 0))

    def test_the_twenty_minute_alert_is_urgent_and_is_not_held_for_a_second_look(self):
        c = Check(self.NOW, self.waiting(minutes=21), jobs=False)
        self.assertEqual([u for _, u in c.sent], [True])
        self.assertEqual(c.reads, 1)

    def test_a_stale_status_alert_is_not_withdrawn_by_the_queue_second_look(self):
        c = Check(self.NOW, [self.waiting(), self.started()], jobs=False, commit=self.NOW - timedelta(minutes=40))
        self.assertEqual(len(c.sent), 1)
        self.assertIn("has not been committed for 40 minutes", c.bodies[0])
        self.assertNotIn("waited", c.bodies[0])
        self.assertIn("queued alert withdrawn", c.line)


class Reconcile(unittest.TestCase):
    def test_only_queued_rows_with_an_id_are_asked_about(self):
        asked = []
        rows = [row(at(14, 0), "completed", 1, "success"), row(at(14, 15), "in_progress", 2), row(at(14, 30), "queued", 3),
                {"status": "queued", "created_at": at(14, 31)}, {"id": None, "status": "queued", "created_at": at(14, 32)}]
        out, lagged = A.reconcile(rows, lambda rid: asked.append(rid) or True)
        self.assertEqual(asked, [3])
        self.assertEqual((lagged, out[2]["status"], out[2]["row_status"]), (1, "in_progress", "queued"))
        self.assertEqual((out[3]["status"], out[4]["status"]), ("queued", "queued"))   # no id: nothing to ask, still waiting

    def test_the_input_rows_are_not_modified(self):
        rows = [row(at(14, 30), "queued", 3)]
        A.reconcile(rows, lambda rid: True)
        self.assertEqual(rows[0]["status"], "queued")

    def test_none_and_empty_pass_through(self):
        self.assertEqual(A.reconcile(None, lambda r: True), (None, 0))
        self.assertEqual(A.reconcile([], lambda r: True), ([], 0))

    def test_a_false_or_unknown_answer_leaves_the_row_waiting(self):
        for answer in (False, None):
            out, lagged = A.reconcile([row(at(14, 30), "queued", 3)], lambda rid, a=answer: a)
            self.assertEqual((lagged, out[0]["status"]), (0, "queued"))


class JobStarted(unittest.TestCase):
    def ask(self, jobs=None, error=None):
        class R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return json.dumps({"jobs": jobs}).encode()
        with mock.patch.object(A.urllib.request, "urlopen", side_effect=error) if error else \
                mock.patch.object(A.urllib.request, "urlopen", return_value=R()):
            with contextlib.redirect_stdout(io.StringIO()):
                return A.job_started(123)

    def test_a_started_job_is_true(self):
        self.assertIs(self.ask([{"status": "in_progress"}]), True)
        self.assertIs(self.ask([{"status": "completed"}]), True)

    def test_every_job_still_queued_is_false(self):
        self.assertIs(self.ask([{"status": "queued"}, {"status": "waiting"}]), False)

    def test_one_started_job_among_queued_ones_is_true(self):
        self.assertIs(self.ask([{"status": "queued"}, {"status": "in_progress"}]), True)

    def test_no_jobs_listed_yet_is_unknown_not_false(self):
        self.assertIsNone(self.ask([]))

    def test_an_api_failure_is_unknown(self):
        self.assertIsNone(self.ask(error=OSError("boom")))

    def test_fetch_runs_carries_the_run_id(self):
        class R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return json.dumps({"workflow_runs": [{"id": 77, "status": "queued", "conclusion": None,
                                                      "created_at": "2026-10-22T14:15:05Z"}]}).encode()
        with mock.patch.object(A.urllib.request, "urlopen", return_value=R()):
            self.assertEqual(A.fetch_runs()[0]["id"], 77)


class TheSenderIsInTheSubject(unittest.TestCase):
    def test_label_by_environment(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(A.sender_label(), "GitHub")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            self.assertEqual(A.sender_label(), "LAPTOP BOT")
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(A.sender_label(), "LAPTOP BOT")

    def test_an_explicit_sender_and_none(self):
        self.assertEqual(A.sender_label("CLOUD"), "CLOUD")
        self.assertEqual(A.sender_label(None), "")
        self.assertEqual(A.sender_label(""), "")

    def test_every_subject_run_once_sends_names_its_sender(self):
        now = at(14, 37)
        late = Check(now, [row(now - timedelta(minutes=7), "queued", 1)], jobs=False, sender="GitHub")
        self.assertEqual(late.sent, [("ARM A WATCH: Arm A is late (checked by GitHub)", False)])
        down = Check(now, [row(now - timedelta(minutes=21), "queued", 1)], jobs=False, sender="LAPTOP BOT")
        self.assertEqual(down.sent, [("ARM A WATCH ALERT: Arm A is not running (checked by LAPTOP BOT)", True)])

    def test_the_recovery_notice_names_its_sender_too(self):
        fired = {}
        now = at(14, 37)
        Check(now, [row(now - timedelta(minutes=7), "queued", 1)], jobs=False, fired=fired, sender="LAPTOP BOT")
        calm = Check(now + timedelta(minutes=5), [row(now + timedelta(minutes=1), "in_progress", 2)], jobs=False,
                     fired=fired, sender="LAPTOP BOT", commit=now + timedelta(minutes=4))
        self.assertEqual([s for s, _ in calm.sent], ["ARM A WATCH: Arm A is running again (checked by LAPTOP BOT)"])

    def test_no_sender_keeps_the_plain_subject(self):
        now = at(14, 37)
        c = Check(now, [row(now - timedelta(minutes=7), "queued", 1)], jobs=False, sender=None)
        self.assertEqual(c.sent, [("ARM A WATCH: Arm A is late", False)])

    def test_the_laptop_daemon_gets_the_sender_without_any_change_on_its_side(self):
        src = open(os.path.join(ROOT, "rh_daemon.py"), encoding="utf-8").read()
        start = src.index("arm_a_watch.run_once(")
        call = src[start:src.index("deliver=_deliver)", start)]
        self.assertNotIn("sender", call, "the daemon relies on the 'auto' default; passing one would bypass it")

    def test_ascii_subjects_only(self):
        now = at(14, 37)
        c = Check(now, [row(now - timedelta(minutes=7), "queued", 1)], jobs=False)
        c.sent[0][0].encode("ascii")
        self.assertNotIn("—", c.sent[0][0])


if __name__ == "__main__":
    unittest.main()
