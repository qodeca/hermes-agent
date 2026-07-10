"""Tests for per-run cron stats (finding 14 / T6).

Covers two layers:

- ``run_job`` itself: it now stashes a ``_cron_run_stats`` dict on the agent
  object it hands back via ``defer_agent_teardown`` (rather than widening its
  4-tuple return — see the comment in ``cron/scheduler.py`` next to
  ``_stats_start_monotonic``). These tests drive the real ``run_job`` with a
  fake ``AIAgent`` and inspect the stats it attaches.
- ``run_one_job``: it reads those stats off the deferred agent, threads them
  into ``mark_job_run(..., stats=...)`` so they land on the job record as
  ``last_run_stats``, and emits one ``cron.run_summary`` INFO log line per
  run regardless of delivery outcome. These tests fake ``run_job`` directly
  (mirroring ``tests/cron/test_run_one_job.py``'s pipeline-patching style) so
  they exercise only ``run_one_job``'s own extraction/threading/logging
  logic, against a real ``mark_job_run`` backed by a temp jobs store.

``mark_job_run`` itself gets a focused unit test confirming ``stats`` is
optional and legacy callers that omit it get no ``last_run_stats`` key at
all (unchanged behavior).
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cron.jobs import build_minimal_run_stats, create_job, get_job, mark_job_run
from cron.scheduler import run_job

STATS_KEYS = {
    "started_at", "ended_at", "duration_s", "exec_duration_s",
    "api_calls", "output_tokens", "exit_reason",
}


def _wall_span_seconds(stats: dict) -> float:
    """Wall-clock span ended_at - started_at from the stats' own timestamps."""
    started = datetime.fromisoformat(stats["started_at"])
    ended = datetime.fromisoformat(stats["ended_at"])
    return (ended - started).total_seconds()


_RUNTIME_PROVIDER = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate cron job storage into a temp dir — never touch real ~/.hermes/."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class _FakeAgentSuccess:
    """Minimal AIAgent stand-in with the attributes run_job's stats code reads."""

    def __init__(self, *args, **kwargs):
        self.session_output_tokens = 4321

    def run_conversation(self, *args, **kwargs):
        import time
        time.sleep(0.01)  # keep duration_s measurably > 0
        return {"final_response": "the answer"}

    def get_activity_summary(self):
        return {"api_call_count": 7}

    def close(self):
        pass


class _FakeAgentFails:
    """AIAgent stand-in whose run_conversation blows up."""

    def __init__(self, *args, **kwargs):
        self.session_output_tokens = 12

    def run_conversation(self, *args, **kwargs):
        self._touched = True
        raise RuntimeError("boom: upstream API error")

    def get_activity_summary(self):
        return {"api_call_count": 2}

    def close(self):
        pass


def _run_job(job, tmp_path, agent_cls):
    """Drive the real run_job() with a fake AIAgent, deferring teardown."""
    deferred: list = []
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_state.SessionDB", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME_PROVIDER), \
         patch("run_agent.AIAgent", agent_cls):
        result = run_job(job, defer_agent_teardown=deferred)
    return result, deferred


class TestRunJobStats:
    """run_job attaches a complete stats dict to the (deferred) agent."""

    def test_successful_run_has_all_keys_with_plausible_values(self, tmp_path):
        job = {"id": "job-a", "name": "job a", "prompt": "hello"}
        (success, output, final_response, error), deferred = _run_job(job, tmp_path, _FakeAgentSuccess)

        assert success is True
        assert error is None
        assert len(deferred) == 1
        stats = deferred[0]._cron_run_stats

        assert set(stats) == STATS_KEYS
        assert stats["duration_s"] > 0
        assert stats["exec_duration_s"] > 0
        assert stats["api_calls"] == 7
        assert stats["output_tokens"] == 4321
        assert stats["exit_reason"] == "completed"
        assert stats["started_at"]
        assert stats["ended_at"]
        # Clock-consistency invariant: duration_s IS the wall-clock span
        # between the two recorded timestamps.
        assert stats["duration_s"] == pytest.approx(_wall_span_seconds(stats), abs=0.01)

    def test_started_at_prefers_running_marker_when_present(self, tmp_path):
        job = {
            "id": "job-marker",
            "name": "recurring job",
            "prompt": "hello",
            "running_marker": {"at": "2020-01-01T00:00:00+00:00", "by": "test"},
        }
        (success, *_rest), deferred = _run_job(job, tmp_path, _FakeAgentSuccess)

        assert success is True
        assert deferred[0]._cron_run_stats["started_at"] == "2020-01-01T00:00:00+00:00"

    def test_marker_anchored_duration_is_wall_clock_not_exec_time(self, tmp_path):
        """When started_at comes from the fire-time running_marker (which
        predates pool queueing), duration_s must be the wall-clock span
        ended_at - started_at — NOT the monotonic execution measure, which
        lives separately in exec_duration_s. A years-old marker makes the
        difference unmistakable."""
        job = {
            "id": "job-span",
            "name": "recurring job",
            "prompt": "hello",
            "running_marker": {"at": "2020-01-01T00:00:00+00:00", "by": "test"},
        }
        (success, *_rest), deferred = _run_job(job, tmp_path, _FakeAgentSuccess)
        assert success is True
        stats = deferred[0]._cron_run_stats

        # duration_s ≈ ended_at - started_at (the invariant), which here is
        # years; exec_duration_s stays the tiny actual execution time.
        assert stats["duration_s"] == pytest.approx(_wall_span_seconds(stats), abs=0.01)
        assert stats["duration_s"] > 365 * 24 * 3600  # marker is years in the past
        assert stats["exec_duration_s"] < 120  # actual execution was near-instant

    def test_failing_run_still_records_stats_with_error_exit_reason(self, tmp_path):
        job = {"id": "job-b", "name": "job b", "prompt": "hello"}
        (success, output, final_response, error), deferred = _run_job(job, tmp_path, _FakeAgentFails)

        assert success is False
        assert error is not None
        assert len(deferred) == 1
        stats = deferred[0]._cron_run_stats

        assert set(stats) == STATS_KEYS
        assert stats["duration_s"] >= 0
        assert stats["exec_duration_s"] >= 0
        assert stats["api_calls"] == 2
        assert stats["output_tokens"] == 12
        assert stats["exit_reason"] == "error"

    @pytest.mark.parametrize(
        "message,expected_reason",
        [
            ("Cron job 'x' idle for 5s (limit 1s) — last activity: none", "timeout_inactivity"),
            ("Cron job 'x' exceeded wall-clock runtime cap of 5s (ran 6s)", "timeout_wall_clock"),
        ],
    )
    def test_timeout_exit_reason_distinguishes_inactivity_vs_wall_clock(
        self, tmp_path, message, expected_reason
    ):
        class _FakeAgentTimeout(_FakeAgentFails):
            def run_conversation(self, *args, **kwargs):
                raise TimeoutError(message)

        (success, *_rest), deferred = _run_job(job := {"id": "job-t", "prompt": "hi"}, tmp_path, _FakeAgentTimeout)
        assert success is False
        assert deferred[0]._cron_run_stats["exit_reason"] == expected_reason

    def test_interrupted_exception_maps_to_interrupted_exit_reason(self, tmp_path):
        class _FakeAgentInterrupted(_FakeAgentFails):
            def run_conversation(self, *args, **kwargs):
                raise RuntimeError("interrupted_during_api_call")

        job = {"id": "job-i", "prompt": "hi"}
        (success, *_rest), deferred = _run_job(job, tmp_path, _FakeAgentInterrupted)
        assert success is False
        assert deferred[0]._cron_run_stats["exit_reason"] == "interrupted"

    def test_no_agent_job_gets_no_stats_object(self, tmp_path):
        """no_agent script jobs short-circuit before AIAgent is ever built —
        there is no agent to attach stats to, and no caller currently needs
        stats for that path (scope: agent-driven runs only)."""
        job = {"id": "job-script", "no_agent": True, "script": "/nonexistent/script.sh"}
        deferred: list = []
        with patch("cron.scheduler._hermes_home", tmp_path):
            run_job(job, defer_agent_teardown=deferred)
        assert deferred == []


class TestMarkJobRunStats:
    """mark_job_run: stats is optional and additive, never required."""

    def test_stats_persisted_as_last_run_stats(self, tmp_cron_dir):
        job = create_job(prompt="Test", schedule="every 1h")
        stats = {
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:05+00:00",
            "duration_s": 5.0,
            "exec_duration_s": 4.2,
            "api_calls": 3,
            "output_tokens": 100,
            "exit_reason": "completed",
        }
        mark_job_run(job["id"], True, stats=stats)
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_run_stats"] == stats

    def test_legacy_call_without_stats_writes_no_last_run_stats_key(self, tmp_cron_dir):
        job = create_job(prompt="Test", schedule="every 1h")
        mark_job_run(job["id"], True)
        updated = get_job(job["id"])
        assert updated is not None
        assert "last_run_stats" not in updated

    def test_stats_omitted_on_subsequent_call_does_not_clear_prior_value(self, tmp_cron_dir):
        """Documents current behavior: mark_job_run only ever ADDS the key
        when stats is given; it never explicitly clears a stale one. The
        outcome-recording callers (shutdown interrupt, reconcile, processing
        errors) therefore pass build_minimal_run_stats(...) explicitly — see
        TestStaleStatsReplaced — so the only omitting caller left is
        run_one_job's success path for stats-less runs (no_agent jobs), where
        a surviving previous record is the correct behavior to pin."""
        job = create_job(prompt="Test", schedule="every 1h")
        mark_job_run(job["id"], True, stats={"exit_reason": "completed"})
        mark_job_run(job["id"], True)  # legacy-style call, no stats kwarg
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_run_stats"] == {"exit_reason": "completed"}


class TestRunOneJobStatsThreading:
    """run_one_job extracts stats from the deferred agent, persists them via
    mark_job_run, and logs a cron.run_summary line — independent of delivery
    outcome."""

    def _fake_run_job(self, *, success, stats, calls, error=None, final_response="final response"):
        def _inner(job, *, defer_agent_teardown=None):
            calls.append(("run_job", job["id"]))
            if stats is not None and defer_agent_teardown is not None:
                agent = SimpleNamespace(_cron_run_stats=stats)
                defer_agent_teardown.append(agent)
            return (success, "output doc", final_response, error)
        return _inner

    def _patch_common(self, monkeypatch, tmp_path, calls, *, deliver_raises=False):
        import cron.scheduler as s

        monkeypatch.setattr(s, "_hermes_home", tmp_path)
        monkeypatch.setattr(s, "claim_dispatch", lambda *_a, **_kw: True)
        monkeypatch.setattr(s, "save_job_output", lambda jid, out: calls.append(("save", jid)) or "/tmp/out.md")

        def _fake_deliver(job, content, adapters=None, loop=None):
            calls.append(("deliver", job["id"]))
            if deliver_raises:
                raise RuntimeError("delivery platform is down")
            return None

        monkeypatch.setattr(s, "_deliver_result", _fake_deliver)

    def test_success_persists_last_run_stats_on_job_record(self, tmp_cron_dir, monkeypatch, caplog):
        import cron.scheduler as s

        job = create_job(prompt="Test", schedule="every 1h")
        calls: list = []
        stats = {
            "started_at": "2026-07-10T00:00:00+00:00",
            "ended_at": "2026-07-10T00:00:05+00:00",
            "duration_s": 5.0,
            "exec_duration_s": 4.8,
            "api_calls": 3,
            "output_tokens": 456,
            "exit_reason": "completed",
        }
        self._patch_common(monkeypatch, tmp_cron_dir, calls)
        monkeypatch.setattr(s, "run_job", self._fake_run_job(success=True, stats=stats, calls=calls))

        with caplog.at_level(logging.INFO, logger="cron.run_summary"):
            result = s.run_one_job(job)

        assert result is True
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_run_stats"] == stats
        assert updated["last_status"] == "ok"

        summary_records = [r for r in caplog.records if r.name == "cron.run_summary"]
        assert len(summary_records) == 1
        msg = summary_records[0].getMessage()
        assert job["id"] in msg
        assert "exit_reason=completed" in msg
        assert "api_calls=3" in msg
        assert "output_tokens=456" in msg

    def test_failing_run_still_records_stats_with_error_status(self, tmp_cron_dir, monkeypatch):
        import cron.scheduler as s

        job = create_job(prompt="Test", schedule="every 1h")
        calls: list = []
        stats = {
            "started_at": "2026-07-10T00:00:00+00:00",
            "ended_at": "2026-07-10T00:00:02+00:00",
            "duration_s": 2.0,
            "exec_duration_s": 1.9,
            "api_calls": 1,
            "output_tokens": 20,
            "exit_reason": "error",
        }
        self._patch_common(monkeypatch, tmp_cron_dir, calls)
        monkeypatch.setattr(
            s, "run_job",
            self._fake_run_job(success=False, stats=stats, calls=calls, error="RuntimeError: boom", final_response=""),
        )

        result = s.run_one_job(job)

        assert result is True  # run_one_job "succeeds" at processing even though the job itself failed
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_status"] == "error"
        assert updated["last_run_stats"] == stats

    def test_run_summary_logged_even_when_delivery_fails(self, tmp_cron_dir, monkeypatch, caplog):
        import cron.scheduler as s

        job = create_job(prompt="Test", schedule="every 1h")
        calls: list = []
        stats = {
            "started_at": "2026-07-10T00:00:00+00:00",
            "ended_at": "2026-07-10T00:00:03+00:00",
            "duration_s": 3.0,
            "exec_duration_s": 2.7,
            "api_calls": 5,
            "output_tokens": 78,
            "exit_reason": "completed",
        }
        self._patch_common(monkeypatch, tmp_cron_dir, calls, deliver_raises=True)
        monkeypatch.setattr(s, "run_job", self._fake_run_job(success=True, stats=stats, calls=calls))

        with caplog.at_level(logging.INFO, logger="cron.run_summary"):
            result = s.run_one_job(job)

        assert result is True
        assert ("deliver", job["id"]) in calls  # delivery was attempted (and raised internally)

        summary_records = [r for r in caplog.records if r.name == "cron.run_summary"]
        assert len(summary_records) == 1

        # Delivery failure is tracked but does not block persistence of stats.
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_run_stats"] == stats
        assert updated["last_delivery_error"] == "delivery platform is down"

    def test_no_stats_from_run_job_means_no_last_run_stats_written(self, tmp_cron_dir, monkeypatch):
        """A run_job double that never populates defer_agent_teardown (e.g.
        the no_agent script path, or any existing test double with the old
        fixed-arity signature) must not break mark_job_run's legacy no-stats
        contract."""
        import cron.scheduler as s

        job = create_job(prompt="Test", schedule="every 1h")
        calls: list = []
        self._patch_common(monkeypatch, tmp_cron_dir, calls)
        monkeypatch.setattr(s, "run_job", self._fake_run_job(success=True, stats=None, calls=calls))

        result = s.run_one_job(job)

        assert result is True
        updated = get_job(job["id"])
        assert updated is not None
        assert "last_run_stats" not in updated


class TestBuildMinimalRunStats:
    """build_minimal_run_stats: explicit outcome record for paths without an
    executed agent run (shutdown interrupt, reconcile, processing errors)."""

    def test_shape_and_null_unknowables_without_job(self):
        stats = build_minimal_run_stats("interrupted")
        assert set(stats) == STATS_KEYS
        assert stats["exit_reason"] == "interrupted"
        assert stats["ended_at"]
        assert stats["started_at"] is None
        assert stats["duration_s"] is None
        assert stats["exec_duration_s"] is None
        assert stats["api_calls"] is None
        assert stats["output_tokens"] is None

    def test_started_at_recovered_from_running_marker(self):
        job = {"running_marker": {"at": "2020-01-01T00:00:00+00:00", "by": "test"}}
        stats = build_minimal_run_stats("reconciled", job=job)
        assert stats["started_at"] == "2020-01-01T00:00:00+00:00"
        # Clock-consistency invariant holds here too: duration_s is the
        # wall-clock span between the record's own timestamps.
        assert stats["duration_s"] == pytest.approx(_wall_span_seconds(stats), abs=0.01)
        assert stats["exec_duration_s"] is None  # the run never executed here

    def test_started_at_recovered_from_one_shot_run_claim(self):
        job = {"run_claim": {"at": "2020-06-01T00:00:00+00:00", "by": "test"}}
        stats = build_minimal_run_stats("reconciled", job=job)
        assert stats["started_at"] == "2020-06-01T00:00:00+00:00"
        assert stats["duration_s"] is not None

    def test_unparseable_marker_yields_null_duration(self):
        job = {"running_marker": {"at": "not-a-timestamp", "by": "test"}}
        stats = build_minimal_run_stats("reconciled", job=job)
        assert stats["started_at"] == "not-a-timestamp"
        assert stats["duration_s"] is None


class TestStaleStatsReplaced:
    """Cross-call scenarios: a later interrupt/reconcile outcome must replace
    a previous successful run's last_run_stats, never leave exit_reason
    "completed" standing next to a fresh last_status "error"."""

    _COMPLETED_STATS = {
        "started_at": "2026-07-09T22:00:00+00:00",
        "ended_at": "2026-07-09T22:00:30+00:00",
        "duration_s": 30.0,
        "exec_duration_s": 29.5,
        "api_calls": 4,
        "output_tokens": 250,
        "exit_reason": "completed",
    }

    def test_shutdown_interrupt_replaces_completed_stats(self, tmp_cron_dir):
        import cron.scheduler as s

        job = create_job(prompt="Test", schedule="every 1h")
        mark_job_run(job["id"], True, stats=dict(self._COMPLETED_STATS))
        before = get_job(job["id"])
        assert before is not None
        assert before["last_run_stats"]["exit_reason"] == "completed"

        # Simulate the gateway shutdown path: the job is in flight when the
        # process force-kills tool subprocesses.
        with s._running_lock:
            s._running_job_ids.add(job["id"])
        try:
            marked = s.mark_running_jobs_interrupted("Interrupted by gateway shutdown")
        finally:
            with s._running_lock:
                s._running_job_ids.discard(job["id"])
                s._interrupted_job_ids.discard(job["id"])

        assert job["id"] in marked
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_status"] == "error"
        assert updated["last_run_stats"]["exit_reason"] == "interrupted"
        assert updated["last_run_stats"]["ended_at"]

    def test_reconcile_replaces_completed_stats(self, tmp_cron_dir, monkeypatch):
        import cron.scheduler as s
        from cron.jobs import _machine_id, load_jobs, save_jobs

        job = create_job(prompt="Test", schedule="every 1h")
        mark_job_run(job["id"], True, stats=dict(self._COMPLETED_STATS))

        # Stamp a running_marker as if a previous incarnation of this host
        # died mid-run (own host prefix => reapable at startup regardless of
        # marker age).
        marker_at = "2026-07-09T23:00:00+00:00"
        jobs = load_jobs()
        for j in jobs:
            if j["id"] == job["id"]:
                j["running_marker"] = {"at": marker_at, "by": _machine_id()}
        save_jobs(jobs)

        monkeypatch.setattr(s, "_send_reconcile_alert", lambda *a, **kw: None)
        reconciled = s.reconcile_orphaned_runs()

        assert job["id"] in reconciled
        updated = get_job(job["id"])
        assert updated is not None
        assert updated["last_status"] == "error"
        assert updated["last_run_stats"]["exit_reason"] == "reconciled"
        # started_at recovered from the orphaned run's own marker stamp.
        assert updated["last_run_stats"]["started_at"] == marker_at
        assert updated["running_marker"] is None  # marker cleared by mark_job_run
