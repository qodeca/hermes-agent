"""Tests for the durable running marker on RECURRING cron jobs (T2, finding 2).

Incident: a recurring job fired, ran 2.4h, then died with the gateway — and
jobs.json still showed ``last_run_at: null``, because run state was persisted
only at completion (``mark_job_run``). One-shot jobs already have a durable
"claim stamped at fire time" pattern (``run_claim``); this generalizes that
idea to recurring jobs via a SEPARATE ``running_marker`` field so the
one-shot due-skip semantics (which exist to prevent double-dispatch) do not
leak onto recurring jobs, which must never be skipped because they carry a
marker — only made visible.

Covers:
  (a) a crash mid-run (run_job raises) leaves running_marker + last_status
      == "running" durably persisted in jobs.json;
  (b) a normal completion clears both and sets last_run_at;
  (c) no-skip regression: a fresh running_marker never causes get_due_jobs()
      to skip the job's next legitimate occurrence (unlike run_claim for
      one-shots);
  (d) the generalized TTL helper (_run_claim_ttl_seconds) folds in the T4
      wall-clock runtime cap alongside the inactivity timeout.
"""
from datetime import timedelta

import pytest

import cron.jobs as j
import cron.scheduler as s


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py)."""
    monkeypatch.setattr(j, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(j, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(j, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _recurring_job(job_id="rec1", **overrides):
    now = j._hermes_now()
    job = {
        "id": job_id,
        "name": "recurring",
        "prompt": "check things",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "next_run_at": (now - timedelta(seconds=5)).isoformat(),
        "enabled": True,
        "state": "scheduled",
    }
    job.update(overrides)
    return job


class TestRunningMarkerCrashVisibility:
    """(a) A recurring job that crashes mid-run leaves a durable trace."""

    def test_crash_mid_run_leaves_marker_and_running_status(self, tmp_cron_dir, monkeypatch):
        j.save_jobs([_recurring_job()])

        # get_due_jobs() is the shared "about to fire" checkpoint — it stamps
        # running_marker + last_status="running" under the same lock the
        # one-shot run_claim uses, then persists.
        due = j.get_due_jobs()
        assert [d["id"] for d in due] == ["rec1"]
        due_job = due[0]
        assert due_job.get("running_marker") is not None
        assert due_job.get("last_status") == "running"

        # Confirm it was actually persisted, not just returned in-memory.
        persisted = j.get_job("rec1")
        assert persisted["running_marker"] is not None
        assert persisted["last_status"] == "running"

        # Now fire it through run_one_job with run_job simulating a crash
        # (gateway killed mid-execution) — SystemExit is a BaseException,
        # not caught by run_one_job's `except Exception`, so it propagates.
        monkeypatch.setattr(s, "run_job", lambda job, **kw: (_ for _ in ()).throw(SystemExit(1)))

        with pytest.raises(SystemExit):
            s.run_one_job(due_job)

        # mark_job_run() never ran — the marker and "running" status must
        # still be on disk. This is the whole point: the crashed run is
        # durably visible instead of jobs.json showing last_run_at: null.
        after_crash = j.get_job("rec1")
        assert after_crash["running_marker"] is not None
        assert after_crash["running_marker"]["at"]
        assert after_crash["running_marker"]["by"]
        assert after_crash["last_status"] == "running"
        assert after_crash.get("last_run_at") is None


class TestRunningMarkerClearedOnCompletion:
    """(b) A normal completion clears the marker and sets last_run_at."""

    def test_normal_completion_clears_marker_and_sets_last_run_at(self, tmp_cron_dir, monkeypatch):
        j.save_jobs([_recurring_job()])
        due = j.get_due_jobs()
        due_job = due[0]
        assert due_job.get("running_marker") is not None  # sanity: it was stamped

        monkeypatch.setattr(
            s, "run_job",
            lambda job, **kw: (True, "output text", "final response", None),
        )
        monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
        monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)

        ok = s.run_one_job(due_job)
        assert ok is True

        after = j.get_job("rec1")
        assert after["running_marker"] is None
        assert after["last_status"] == "ok"
        assert after.get("last_run_at") is not None


class TestRunningMarkerNoSkipRegression:
    """(c) A fresh running_marker must NOT cause the job's next legitimate
    occurrence to be skipped — unlike run_claim's due-skip guard for
    one-shots. running_marker is visibility-only."""

    def test_fresh_marker_does_not_block_next_due_occurrence(self, tmp_cron_dir):
        now = j._hermes_now()
        # Simulate a job that still carries a fresh marker from a prior fire
        # (e.g. the marker survived because a crash skipped mark_job_run) but
        # whose next_run_at has legitimately come due again.
        stale_but_fresh_marker = {"at": now.isoformat(), "by": "otherhost:123"}
        j.save_jobs([_recurring_job(
            running_marker=stale_but_fresh_marker,
            last_status="running",
        )])

        due = j.get_due_jobs()

        assert [d["id"] for d in due] == ["rec1"], (
            "recurring job with a fresh running_marker must still be "
            "dispatched — running_marker carries no due-skip semantics"
        )

    def test_one_shot_run_claim_still_skips_for_contrast(self, tmp_cron_dir):
        """Contrast case: proves the test harness actually distinguishes the
        two behaviors — a one-shot WITH a fresh run_claim IS skipped."""
        now = j._hermes_now()
        run_at = (now - timedelta(seconds=5)).isoformat()
        fresh_claim = {"at": now.isoformat(), "by": "otherhost:123"}
        j.save_jobs([{
            "id": "oneshot1", "name": "R", "prompt": "x",
            "schedule": {"kind": "once", "run_at": run_at},
            "next_run_at": run_at, "enabled": True, "state": "scheduled",
            "run_claim": fresh_claim,
        }])

        due = j.get_due_jobs()

        assert due == [], "a one-shot with a fresh run_claim must be skipped"


class TestRunClaimTTLWithMaxRuntime:
    """(d) The generalized TTL helper folds T4's wall-clock runtime cap in
    alongside the inactivity timeout."""

    def test_ttl_uses_runtime_axis_when_it_dominates(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)  # -> 600*3=1800
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "10000")  # -> 10000*1.5=15000
        assert j._run_claim_ttl_seconds() == 15000.0

    def test_ttl_uses_inactivity_axis_when_it_dominates(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "6000")  # -> 6000*3=18000
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "100")  # -> 100*1.5=150
        assert j._run_claim_ttl_seconds() == 18000.0

    def test_ttl_floors_when_both_axes_unlimited(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "0")
        assert j._run_claim_ttl_seconds() == float(j.ONESHOT_RUN_CLAIM_TTL_SECONDS)

    def test_ttl_default_respects_default_wall_clock_cap(self, monkeypatch):
        """With nothing configured, the default max-runtime cap (3600s, T4)
        pulls the TTL above the historical 1800s inactivity-only default —
        the recovery window must not be shorter than a legitimately full-
        length run under the wall-clock cap."""
        monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        assert j._run_claim_ttl_seconds() == 5400.0  # max(1800, 3600*1.5, 1800)

    def test_ttl_reads_max_runtime_from_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)  # -> 1800
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"cron": {"max_runtime_seconds": 2000}},
        )
        assert j._run_claim_ttl_seconds() == 3000.0  # max(1800, 2000*1.5, 1800)
