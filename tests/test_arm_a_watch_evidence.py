"""ARM A WATCH keeps what GitHub returned, and cannot be answered from a shared cache (2026-10-22).

Ten false "Arm A is late" mails in one day, every one from the laptop's copy of this check and none from GitHub's own
copy, which reads with a token. An unauthenticated REST read is marked public, max-age=60, so a cache between the
laptop and GitHub may serve it, and may serve it stale while GitHub is degraded. Nobody could say what either the run
list or the jobs endpoint had returned, because nothing kept it. These tests pin: every read is a cache miss by
construction (unique query parameter, no-cache request headers), and an alert carries what GitHub returned.

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
NOW = datetime(2026, 10, 22, 17, 37, 42, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload, self.headers = payload, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


class Headers(dict):
    pass


def urlopen_recording(payload, headers=None):
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return FakeResponse(payload, headers)
    return fake, seen


RUNS_PAYLOAD = {"workflow_runs": [{"id": 77, "status": "queued", "conclusion": None, "created_at": "2026-10-22T17:30:08Z"}]}
JOBS_PAYLOAD = {"jobs": [{"status": "queued"}]}
HDRS = Headers({"Date": "Thu, 22 Oct 2026 17:37:40 GMT", "Age": "412", "X-Cache": "HIT", "Cache-Control": "public, max-age=60"})


class EveryReadIsACacheMiss(unittest.TestCase):
    def setUp(self):
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}

    def test_the_runs_read_carries_a_unique_query_parameter_and_no_cache_headers(self):
        fake, seen = urlopen_recording(RUNS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            A.fetch_runs()
        req = seen[0]
        self.assertIn("&_cb=", req.full_url)                           # the url already has "?per_page=", so "&"
        self.assertEqual(req.get_header("Cache-control"), "no-cache")
        self.assertEqual(req.get_header("Pragma"), "no-cache")

    def test_the_jobs_read_carries_them_too(self):
        fake, seen = urlopen_recording(JOBS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            A.job_started(77)
        self.assertIn("&_cb=", seen[0].full_url)
        self.assertEqual(seen[0].get_header("Cache-control"), "no-cache")

    def test_two_reads_are_two_different_urls(self):
        fake, seen = urlopen_recording(RUNS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake), mock.patch.object(A.time, "time", side_effect=[1000.0, 1000.5]):
            A.fetch_runs()
            A.fetch_runs()
        self.assertNotEqual(seen[0].full_url, seen[1].full_url)

    def test_a_url_without_a_query_string_gets_a_question_mark(self):
        fake, seen = urlopen_recording({})
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            A._get_json("https://api.github.com/x")
        self.assertIn("/x?_cb=", seen[0].full_url)

    def test_the_token_is_still_sent_when_there_is_one(self):
        fake, seen = urlopen_recording(RUNS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            A.fetch_runs(token="tok")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer tok")

    def test_the_run_ids_still_come_back(self):
        fake, _ = urlopen_recording(RUNS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            self.assertEqual(A.fetch_runs()[0]["id"], 77)


class AnAlertShowsWhatGitHubReturned(unittest.TestCase):
    def setUp(self):
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}

    def alert_body(self, jobs_ok=True, headers=HDRS):
        runs_fake, _ = urlopen_recording(RUNS_PAYLOAD, headers)
        jobs_fake, _ = urlopen_recording(JOBS_PAYLOAD, headers)
        with mock.patch.object(A.urllib.request, "urlopen", runs_fake):
            runs = A.fetch_runs()
        if jobs_ok:
            with mock.patch.object(A.urllib.request, "urlopen", jobs_fake):
                runs, _ = A.reconcile(runs, A.job_started)
        else:
            with mock.patch.object(A.urllib.request, "urlopen", side_effect=OSError("boom")), contextlib.redirect_stdout(io.StringIO()):
                runs, _ = A.reconcile(runs, A.job_started)
        c = {"signal": "queued", "threshold": 6, "minutes": 7.6, "urgent": False}
        return A.build_alert([c], NOW, runs)[1]

    def test_the_body_carries_the_row_the_jobs_and_the_cache_headers(self):
        body = self.alert_body()
        self.assertIn("Evidence (what GitHub returned to this check)", body)
        self.assertIn("run 77: list row 'queued', jobs ['queued']", body)
        self.assertIn("Age=412", body)
        self.assertIn("X-Cache=HIT", body)

    def test_unreadable_jobs_are_said_to_be_unreadable(self):
        self.assertIn("jobs unreadable", self.alert_body(jobs_ok=False))

    def test_a_response_without_headers_does_not_break_the_alert(self):
        body = self.alert_body(headers=None)
        self.assertIn("list response headers: none", body)

    def test_the_sentence_the_laptop_mute_matches_on_is_intact_and_the_evidence_comes_after_it(self):
        body = self.alert_body()
        marker = "give it a machine"
        self.assertEqual(body.count(marker), 1)
        self.assertGreater(body.index("Evidence"), body.index(marker))
        self.assertIn("https://www.githubstatus.com Evidence (what GitHub returned", body)     # the old sentence ends, then the new one

    def test_only_the_queued_signal_carries_evidence(self):
        for sig in ("no_trigger", "stale"):
            text = A.describe({"signal": sig, "threshold": 22, "minutes": 30.0, "urgent": False}, NOW, [])
            self.assertNotIn("Evidence", text)

    def test_the_ascii_only_rule_holds_for_the_evidence(self):
        self.alert_body().encode("ascii")

    def test_the_evidence_store_is_bounded(self):
        fake, _ = urlopen_recording(JOBS_PAYLOAD)
        with mock.patch.object(A.urllib.request, "urlopen", fake):
            for rid in range(60):
                A.job_started(rid)
        self.assertEqual(len(A.EVIDENCE["jobs"]), 20)
        self.assertIn(59, A.EVIDENCE["jobs"])
        self.assertNotIn(0, A.EVIDENCE["jobs"])


class RunOnceEndToEnd(unittest.TestCase):
    def test_the_delivered_alert_for_a_stuck_run_contains_the_evidence(self):
        A.EVIDENCE["runs_meta"], A.EVIDENCE["jobs"] = {}, {}
        created = NOW - timedelta(minutes=8)
        runs_payload = {"workflow_runs": [{"id": 5, "status": "queued", "conclusion": None,
                                           "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}]}
        fake_runs, _ = urlopen_recording(runs_payload, HDRS)
        fake_jobs, _ = urlopen_recording(JOBS_PAYLOAD, HDRS)

        def router(req, timeout=None):
            return (fake_jobs if "/jobs" in req.full_url else fake_runs)(req, timeout)
        sent = []
        with mock.patch.object(A.urllib.request, "urlopen", router), contextlib.redirect_stdout(io.StringIO()):
            A.run_once(now=NOW, deliver=lambda s, b, u: sent.append(b) or True, get_commit_time=lambda ref: NOW - timedelta(minutes=10),
                       get_status=lambda ref: None, sender="LAPTOP BOT")
        self.assertEqual(len(sent), 1)
        self.assertIn("jobs ['queued']", sent[0])
        self.assertIn("Age=412", sent[0])


if __name__ == "__main__":
    unittest.main()
