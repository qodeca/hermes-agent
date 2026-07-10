"""Tests for the opt-in per-job misfire deadline (``misfire_deadline_seconds``).

Incident: a 06:00 greeting job fired at 09:45 after a gateway outage — 3h45m
late, worse than not firing at all. The DEFAULT fire-once-past-grace behavior
in ``cron/jobs.py``'s catch-up branch is deliberate (#33315: it exists to
avoid the perpetual-defer loop where a job whose runtime exceeds
``interval + grace`` would be skipped forever) and must not change.

This adds an OPT-IN per-job override: when ``misfire_deadline_seconds`` is
set and a recurring job's run is later than that many seconds past its
scheduled slot, the run is skipped entirely instead of fired — ``next_run_at``
still fast-forwards (accumulated missed slots are not replayed either way),
``last_status`` records ``"skipped_stale"`` with a descriptive
``last_error``, and the scheduler's tick delivers a one-line notice so the
user learns the run was skipped rather than wondering why nothing arrived.
The deadline is measured from the scheduled slot itself, INDEPENDENT of the
catch-up grace window: a deadline smaller than the grace still skips (the
smaller window dominates).

Covers:
  (a) set + very stale -> not in the due list, ``last_status ==
      "skipped_stale"``, and the scheduler's tick() delivers a skip notice
      (via the real ``get_due_jobs()`` + a monkeypatched ``_deliver_result``);
  (b) unset -> still fires once past grace (#33315 regression guard,
      mandatory);
  (c) set but within the deadline -> fires normally, same as today;
  (d) ``create_job`` / ``update_job`` validation: positive int required,
      ``None`` allowed (clears on update); the ``cronjob`` tool's update
      action sets the field and ``0`` clears it;
  (e) one-shot jobs are unaffected — the catch-up branch this modifies is
      gated to recurring (cron/interval) jobs only;
  (f) deadline below the grace window: ``deadline < lateness < grace`` must
      still skip — the deadline is not clamped to the grace;
  (g) ``get_due_jobs()`` contract: plain list by default, ``(due, skips)``
      with ``include_skips=True``.
"""
from datetime import datetime, timedelta

import pytest

import cron.jobs as j
import cron.scheduler as s


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors tests/cron/test_jobs.py).

    Also redirects cron/scheduler.py's Hermes-home resolution so tick()'s
    advisory tick lock lands under the same temp directory instead of the
    real ~/.hermes/.
    """
    monkeypatch.setattr(j, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(j, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(j, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(s, "_hermes_home", tmp_path)
    return tmp_path


def _make_stale_recurring_job(schedule="every 1h", hours_late=2, **create_kwargs):
    """Create a recurring job and force its next_run_at into the past."""
    job = j.create_job(prompt="Good morning!", schedule=schedule, **create_kwargs)
    jobs = j.load_jobs()
    jobs[0]["next_run_at"] = (j._hermes_now() - timedelta(hours=hours_late)).isoformat()
    j.save_jobs(jobs)
    return job


class TestMisfireDeadlineSkipsStaleRun:
    """(a) A recurring job past its opt-in deadline is skipped, not fired."""

    def test_stale_past_deadline_not_in_due_list(self, tmp_cron_dir):
        job = _make_stale_recurring_job(misfire_deadline_seconds=60, hours_late=2)

        due = j.get_due_jobs()

        assert due == [], "a run past its misfire_deadline_seconds must not be dispatched"
        persisted = j.get_job(job["id"])
        assert persisted["last_status"] == "skipped_stale"
        assert persisted["last_error"]
        assert "60" in persisted["last_error"]  # the configured deadline is named
        # next_run_at is still fast-forwarded — no burst of missed slots later.
        next_dt = j._ensure_aware(datetime.fromisoformat(persisted["next_run_at"]))
        assert next_dt > j._hermes_now()

    def test_scheduler_delivers_skip_notice(self, tmp_cron_dir, monkeypatch):
        job = _make_stale_recurring_job(misfire_deadline_seconds=60, hours_late=2)

        delivered = []

        def _fake_deliver_result(job_arg, content, adapters=None, loop=None):
            delivered.append((job_arg, content))
            return None

        monkeypatch.setattr(s, "_deliver_result", _fake_deliver_result)

        fired = s.tick(verbose=False, sync=True)

        assert fired == 0, "the skipped run must not be counted as dispatched"
        assert len(delivered) == 1, "the scheduler must deliver exactly one skip notice"
        delivered_job, delivered_content = delivered[0]
        assert delivered_job["id"] == job["id"]
        assert "stale" in delivered_content.lower()
        assert "60" in delivered_content

        # A second tick with nothing newly skipped must not re-deliver.
        delivered.clear()
        s.tick(verbose=False, sync=True)
        assert delivered == []


class TestDefaultBehaviorUnchanged:
    """(b) #33315 regression guard: without misfire_deadline_seconds, a stale
    recurring job still fires once past grace — the default must not change."""

    def test_stale_without_deadline_still_fires_once(self, tmp_cron_dir):
        job = _make_stale_recurring_job(hours_late=2)  # no misfire_deadline_seconds

        due = j.get_due_jobs()

        assert [d["id"] for d in due] == [job["id"]], (
            "#33315: a stale recurring job without an opt-in deadline must "
            "still fire once instead of being skipped"
        )
        persisted = j.get_job(job["id"])
        assert persisted["last_status"] != "skipped_stale"
        next_dt = j._ensure_aware(datetime.fromisoformat(persisted["next_run_at"]))
        assert next_dt > j._hermes_now()


class TestWithinDeadlineStillFires:
    """(c) Stale past grace but within the configured deadline still fires."""

    def test_stale_within_deadline_fires_normally(self, tmp_cron_dir):
        job = j.create_job(
            prompt="Good morning!",
            schedule="every 1h",
            misfire_deadline_seconds=3600,  # 1 hour
        )
        jobs = j.load_jobs()
        # 40 minutes late: past the 30-min grace for an hourly job, but well
        # inside the 1-hour misfire_deadline_seconds.
        jobs[0]["next_run_at"] = (j._hermes_now() - timedelta(minutes=40)).isoformat()
        j.save_jobs(jobs)

        due = j.get_due_jobs()

        assert [d["id"] for d in due] == [job["id"]]
        persisted = j.get_job(job["id"])
        assert persisted["last_status"] != "skipped_stale"


class TestDeadlineBelowGraceStillSkips:
    """(f) The deadline is measured from the scheduled slot, independent of
    the grace window — a deadline smaller than the grace must NOT be
    silently clamped to it. Regression: with the check nested inside the
    ``> grace`` gate, ``misfire_deadline_seconds=1800`` on a daily job
    (grace 7200s) was ignored for anything under 2h late."""

    def test_deadline_below_grace_skips(self, tmp_cron_dir):
        job = j.create_job(
            prompt="Good morning!",
            schedule="every 1h",  # grace = 1800s (half the period)
            misfire_deadline_seconds=300,  # 5 min — well below the 30-min grace
        )
        jobs = j.load_jobs()
        # 10 minutes late: deadline (300s) < lateness (600s) < grace (1800s).
        jobs[0]["next_run_at"] = (j._hermes_now() - timedelta(minutes=10)).isoformat()
        j.save_jobs(jobs)

        due = j.get_due_jobs()

        assert due == [], (
            "a deadline smaller than the grace window must still skip — the "
            "deadline is measured from the scheduled slot, not gated on grace"
        )
        persisted = j.get_job(job["id"])
        assert persisted["last_status"] == "skipped_stale"
        # next_run_at is still fast-forwarded to a future occurrence.
        next_dt = j._ensure_aware(datetime.fromisoformat(persisted["next_run_at"]))
        assert next_dt > j._hermes_now()

    def test_below_grace_and_below_deadline_fires(self, tmp_cron_dir):
        """Sanity companion: within BOTH windows the job fires normally."""
        job = j.create_job(
            prompt="Good morning!",
            schedule="every 1h",
            misfire_deadline_seconds=900,  # 15 min
        )
        jobs = j.load_jobs()
        # 5 minutes late: inside the deadline and inside the grace.
        jobs[0]["next_run_at"] = (j._hermes_now() - timedelta(minutes=5)).isoformat()
        j.save_jobs(jobs)

        due = j.get_due_jobs()

        assert [d["id"] for d in due] == [job["id"]]
        persisted = j.get_job(job["id"])
        assert persisted["last_status"] != "skipped_stale"


class TestGetDueJobsContract:
    """(g) get_due_jobs() returns a plain list by default and (due, skips)
    with include_skips=True — existing callers keep the historical shape."""

    def test_default_shape_is_plain_list(self, tmp_cron_dir):
        _make_stale_recurring_job(misfire_deadline_seconds=60, hours_late=2)
        result = j.get_due_jobs()
        assert isinstance(result, list)

    def test_include_skips_returns_due_and_skips(self, tmp_cron_dir):
        job = _make_stale_recurring_job(misfire_deadline_seconds=60, hours_late=2)
        due, skips = j.get_due_jobs(include_skips=True)
        assert due == []
        assert len(skips) == 1
        assert skips[0]["job"]["id"] == job["id"]
        assert "notice" in skips[0] and "stale" in skips[0]["notice"].lower()


class TestCreateJobValidation:
    """(d) misfire_deadline_seconds must be a positive int, or None."""

    @pytest.mark.parametrize("bad_value", [-1, 0, 1.5, "60", True, False])
    def test_invalid_values_rejected(self, tmp_cron_dir, bad_value):
        with pytest.raises(ValueError):
            j.create_job(
                prompt="x", schedule="every 1h", misfire_deadline_seconds=bad_value
            )

    def test_none_is_accepted_and_preserves_default_behavior(self, tmp_cron_dir):
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=None)
        assert job["misfire_deadline_seconds"] is None

    def test_positive_int_is_persisted(self, tmp_cron_dir):
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=120)
        assert job["misfire_deadline_seconds"] == 120
        persisted = j.get_job(job["id"])
        assert persisted["misfire_deadline_seconds"] == 120


class TestUpdateJobValidation:
    """(d) update_job applies the same validation as create_job; explicit
    None clears the field, an absent key leaves it unchanged."""

    @pytest.mark.parametrize("bad_value", [-1, 0, 1.5, "60", True, False])
    def test_update_job_rejects_invalid_values(self, tmp_cron_dir, bad_value):
        job = j.create_job(prompt="x", schedule="every 1h")
        with pytest.raises(ValueError):
            j.update_job(job["id"], {"misfire_deadline_seconds": bad_value})
        persisted = j.get_job(job["id"])
        assert persisted.get("misfire_deadline_seconds") is None, (
            "a rejected update must not partially apply"
        )

    def test_update_job_sets_positive_int(self, tmp_cron_dir):
        job = j.create_job(prompt="x", schedule="every 1h")
        j.update_job(job["id"], {"misfire_deadline_seconds": 600})
        assert j.get_job(job["id"])["misfire_deadline_seconds"] == 600

    def test_update_job_explicit_none_clears(self, tmp_cron_dir):
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=600)
        j.update_job(job["id"], {"misfire_deadline_seconds": None})
        assert j.get_job(job["id"])["misfire_deadline_seconds"] is None

    def test_update_job_absent_key_leaves_field_unchanged(self, tmp_cron_dir):
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=600)
        j.update_job(job["id"], {"name": "renamed"})
        assert j.get_job(job["id"])["misfire_deadline_seconds"] == 600


class TestCronjobToolUpdate:
    """(d) The cronjob tool's update action exposes the field: a positive
    int sets it, 0 clears it, omitting it leaves it unchanged."""

    def test_tool_update_sets_deadline(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob
        job = j.create_job(prompt="x", schedule="every 1h")
        out = cronjob(action="update", job_id=job["id"], misfire_deadline_seconds=600)
        assert '"success": true' in out.lower()
        assert j.get_job(job["id"])["misfire_deadline_seconds"] == 600

    def test_tool_update_zero_clears_deadline(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=600)
        out = cronjob(action="update", job_id=job["id"], misfire_deadline_seconds=0)
        assert '"success": true' in out.lower()
        assert j.get_job(job["id"])["misfire_deadline_seconds"] is None

    def test_tool_update_omitted_leaves_unchanged(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob
        job = j.create_job(prompt="x", schedule="every 1h", misfire_deadline_seconds=600)
        out = cronjob(action="update", job_id=job["id"], name="renamed")
        assert '"success": true' in out.lower()
        assert j.get_job(job["id"])["misfire_deadline_seconds"] == 600

    def test_tool_create_passes_deadline_through(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob
        import json as _json
        out = cronjob(
            action="create",
            prompt="Good morning!",
            schedule="every 1h",
            misfire_deadline_seconds=600,
        )
        payload = _json.loads(out)
        assert payload["success"] is True
        assert j.get_job(payload["job_id"])["misfire_deadline_seconds"] == 600


class TestOneShotUnaffected:
    """(e) misfire_deadline_seconds only affects recurring (cron/interval)
    jobs — the catch-up branch it modifies is gated to those kinds, so a
    one-shot job must fire regardless of how stale it is or whether the
    field is set (unchanged one-shot semantics)."""

    def test_stale_one_shot_with_deadline_still_fires(self, tmp_cron_dir):
        job = j.create_job(
            prompt="Reminder",
            schedule="30m",  # duration strings become one-shot ("once")
            misfire_deadline_seconds=60,
        )
        assert job["schedule"]["kind"] == "once"
        jobs = j.load_jobs()
        jobs[0]["next_run_at"] = (j._hermes_now() - timedelta(hours=2)).isoformat()
        j.save_jobs(jobs)

        due = j.get_due_jobs()

        assert [d["id"] for d in due] == [job["id"]], (
            "misfire_deadline_seconds must not affect one-shot jobs — the "
            "catch-up branch it modifies is recurring-only"
        )
        persisted = j.get_job(job["id"])
        assert persisted.get("last_status") != "skipped_stale"
