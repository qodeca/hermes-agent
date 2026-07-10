"""Tests for T3: startup reconciliation of orphaned cron runs (findings 2, 22).

Incident: the gateway died mid-run (SIGKILL/OOM/crash). ``mark_job_run`` never
executed, so the run vanished — no error recorded, no operator alert.
Graceful-drain interruption (``mark_running_jobs_interrupted``,
cron/scheduler.py, sole caller gateway/run.py's shutdown path) only covers a
CLEAN shutdown with in-memory job ids; a hard crash bypasses it entirely and
nothing reconciled at startup, until now.

``reconcile_orphaned_runs()`` (cron/scheduler.py) is the durable-state
complement, called once by ``InProcessCronScheduler.start()`` before the
first tick. It considers two independent kinds of orphan:

  - recurring jobs' ``running_marker`` (T2) — always reconciled once reapable;
  - one-shot jobs' ``run_claim`` — reconciled ONLY when the one-shot's retry
    budget is exhausted (``repeat.completed >= repeat.times``); a claim with
    retry budget remaining is left for ``_get_due_jobs_locked``'s own
    stale-claim recovery (re-dispatch), so reconciliation never fights that
    at-most-times retry logic (#59229).

Multi-process hazard (#59229, cron/jobs.py ~L1834-1836): gateway and desktop
may both run in-process tickers against one HERMES_HOME. A marker/claim is
reapable only if it is TTL-expired OR its ``by`` HOST PREFIX (everything
before the last ":", i.e. minus the pid suffix ``_machine_id()`` appends when
HERMES_MACHINE_ID is unset) matches this process's own; a FRESH marker whose
host prefix does not match is a live run in another process and must be left
untouched.

Covers:
  (a) a running_marker 3h old (TTL-expired, foreign machine-id) -> reconciled
      to error, marker cleared;
  (b) a fresh running_marker stamped with THIS process's own machine-id ->
      reconciled (same-machine restart implies whatever fired it is gone);
  (c) a fresh running_marker from a foreign machine-id -> untouched (live run
      elsewhere);
  (c2) host-prefix specifics (TestHostPrefixReap): stamper/reconciler
      differing ONLY by pid on the same host -> reconciled even when fresh
      (the actual production bug this fix closes); a different host ->
      untouched; distinct pinned HERMES_MACHINE_ID values on one host ->
      treated as foreign;
  (d) a stale one-shot run_claim whose retry budget is exhausted -> reconciled
      to error (the T2-reviewer boundary: the due-scan would otherwise
      silently pop this job with no recorded error);
  (e) a stale one-shot run_claim with retry budget remaining -> left alone for
      the due-scan's own stale-claim recovery;
  (f) InProcessCronScheduler.start() calls reconcile() exactly once, before
      the first tick;
  (g) the guarded-import operator alert: degrades to logger.warning while
      hermes_cli.operator_alerts.send_operator_alert does not exist (current
      state — T16 ships it in a later slice), and routes through it once it
      does, with no code change required here.
"""
import logging
import os
import socket
import sys
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

import cron.jobs as j
import cron.scheduler as s


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory (mirrors test_run_claim_recurring.py)."""
    monkeypatch.setattr(j, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(j, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(j, "OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


@pytest.fixture()
def own_machine_id(monkeypatch):
    """Pin _machine_id() to a deterministic, IDENTICAL value on both the
    "stamper" (marker's ``by``) and the "reconciler" (``_machine_id()``
    itself, via the env var) sides. Only exercises the same-literal-string
    case (env override, or two processes independently pinned to the same
    id) — it deliberately does NOT cover the differs-only-by-pid production
    scenario (unset HERMES_MACHINE_ID, ``f"{host}:{pid}"``, restart gets a
    new pid), since reusing one pinned string for both sides would mask
    that. See TestHostPrefixReap below, which derives real host:pid-shaped
    ids per side instead of reusing this fixture.
    """
    monkeypatch.setenv("HERMES_MACHINE_ID", "this-host:111")
    return "this-host:111"


def _wait_until(predicate, timeout=10.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


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


def _oneshot_job(job_id="once1", *, times, completed, **overrides):
    now = j._hermes_now()
    run_at = (now - timedelta(minutes=10)).isoformat()
    job = {
        "id": job_id,
        "name": "one shot",
        "prompt": "do the thing",
        "schedule": {"kind": "once", "run_at": run_at},
        "next_run_at": run_at,
        "enabled": True,
        "state": "scheduled",
        "repeat": {"times": times, "completed": completed},
    }
    job.update(overrides)
    return job


class TestRunningMarkerReconciliation:
    """(a)/(b)/(c): recurring-job running_marker reap rules."""

    def test_ttl_expired_foreign_marker_is_reconciled_to_error(self, tmp_cron_dir):
        now = j._hermes_now()
        stale_marker = {
            "at": (now - timedelta(hours=3)).isoformat(),
            "by": "otherhost:999",
        }
        j.save_jobs([_recurring_job(
            running_marker=stale_marker, last_status="running",
        )])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["rec1"]
        after = j.get_job("rec1")
        assert after["running_marker"] is None
        assert after["last_status"] == "error"
        assert after["last_error"] == "interrupted: scheduler restarted mid-run"
        assert after["last_run_at"] is not None

    def test_fresh_marker_with_own_machine_id_is_reconciled(self, tmp_cron_dir, own_machine_id):
        now = j._hermes_now()
        fresh_own_marker = {"at": now.isoformat(), "by": own_machine_id}
        j.save_jobs([_recurring_job(
            running_marker=fresh_own_marker, last_status="running",
        )])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["rec1"]
        after = j.get_job("rec1")
        assert after["running_marker"] is None
        assert after["last_status"] == "error"

    def test_fresh_marker_with_foreign_machine_id_is_untouched(self, tmp_cron_dir, own_machine_id):
        now = j._hermes_now()
        fresh_foreign_marker = {"at": now.isoformat(), "by": "otherhost:999"}
        j.save_jobs([_recurring_job(
            running_marker=fresh_foreign_marker, last_status="running",
        )])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == []
        after = j.get_job("rec1")
        assert after["running_marker"] == fresh_foreign_marker
        assert after["last_status"] == "running"
        assert after.get("last_run_at") is None


class TestHostPrefixReap:
    """``_stamp_is_reapable`` compares the HOST PREFIX of ``by`` (everything
    before the last ":"), not the full string. This is what makes own-machine
    reap work at all in production: ``_machine_id()`` embeds ``os.getpid()``
    when HERMES_MACHINE_ID is unset, so a restarted gateway process — a new
    pid every time — could never match its crashed predecessor's stamp under
    exact-string equality. These tests derive real ``host:pid``-shaped ids
    (not the ``own_machine_id`` fixture's single shared literal) so the
    pid-differs-but-host-matches case is actually exercised.
    """

    def test_stamp_and_reconciler_differing_only_by_pid_same_host_is_reconciled(
        self, tmp_cron_dir, monkeypatch,
    ):
        """The actual bug this fix closes: same host, HERMES_MACHINE_ID unset
        on both the dead incarnation and the restarted one, pids differ. Must
        be reaped even though the marker is fresh."""
        monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
        host = socket.gethostname()
        dead_incarnation_id = f"{host}:{os.getpid() + 1}"  # a pid that is not ours
        fresh_marker = {"at": j._hermes_now().isoformat(), "by": dead_incarnation_id}
        j.save_jobs([_recurring_job(running_marker=fresh_marker, last_status="running")])

        reconciled = s.reconcile_orphaned_runs()  # reconciles as the current process/pid

        assert reconciled == ["rec1"]
        after = j.get_job("rec1")
        assert after["running_marker"] is None
        assert after["last_status"] == "error"

    def test_different_host_fresh_is_untouched(self, tmp_cron_dir, monkeypatch):
        """Host segment differs (pid happens to coincide) — a different host
        can never be this process's crashed predecessor, so it stays."""
        monkeypatch.delenv("HERMES_MACHINE_ID", raising=False)
        fresh_marker = {
            "at": j._hermes_now().isoformat(),
            "by": f"some-other-host:{os.getpid()}",
        }
        j.save_jobs([_recurring_job(running_marker=fresh_marker, last_status="running")])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == []
        assert j.get_job("rec1")["running_marker"] == fresh_marker

    def test_distinct_pinned_machine_ids_on_one_host_are_treated_as_foreign(
        self, tmp_cron_dir, monkeypatch,
    ):
        """Operators running more than one Hermes process on one host against
        a shared HERMES_HOME (e.g. gateway + desktop) opt out of cross-reap
        by pinning distinct HERMES_MACHINE_ID values. Neither pinned id here
        has a ':' suffix to strip, so each compares as its whole string and
        they never collide, even though they're "the same host"."""
        monkeypatch.setenv("HERMES_MACHINE_ID", "hermes-desktop")
        fresh_marker = {"at": j._hermes_now().isoformat(), "by": "hermes-gateway"}
        j.save_jobs([_recurring_job(running_marker=fresh_marker, last_status="running")])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == []
        assert j.get_job("rec1")["running_marker"] == fresh_marker


class TestOneShotRunClaimReconciliation:
    """(d)/(e): the run_claim vs due-scan boundary for one-shot jobs."""

    def test_exhausted_stale_claim_is_reconciled_to_error(self, tmp_cron_dir):
        now = j._hermes_now()
        stale_claim = {
            "at": (now - timedelta(hours=3)).isoformat(),
            "by": "otherhost:999",
        }
        # completed >= times: no retry budget left. Without reconciliation,
        # _get_due_jobs_locked's "dispatch limit reached" branch would
        # silently pop this from jobs.json on the next tick with no recorded
        # error and no alert — exactly the T2-reviewer gap this closes.
        j.save_jobs([_oneshot_job(times=1, completed=1, run_claim=stale_claim)])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["once1"]
        # mark_job_run's own repeat-exhaustion path removes the job once
        # completed >= times (the same terminal behavior a successful final
        # run gets) — the reconciliation value-add is the recorded error +
        # alert that happened before that removal, not survival of the row.
        assert j.get_job("once1") is None

    def test_stale_claim_with_retry_budget_remaining_is_untouched(self, tmp_cron_dir):
        """A one-shot job with retry budget left must be skipped: the due-scan's
        own stale-claim recovery will legitimately re-dispatch it. Reconciling
        it here would call mark_job_run, which sets last_run_at and — for a
        'once' schedule — makes compute_next_run return None, permanently
        disabling the job and burning the remaining retry budget without ever
        attempting it."""
        now = j._hermes_now()
        stale_claim = {
            "at": (now - timedelta(hours=3)).isoformat(),
            "by": "otherhost:999",
        }
        j.save_jobs([_oneshot_job(times=3, completed=1, run_claim=stale_claim)])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == []
        after = j.get_job("once1")
        assert after is not None
        assert after["run_claim"] == stale_claim
        assert after.get("last_run_at") is None
        assert after["repeat"]["completed"] == 1  # untouched

        # Sanity: the due-scan's own recovery still re-dispatches it (proves
        # this is a legitimate live retry path, not a silent black hole).
        due = j.get_due_jobs()
        assert [d["id"] for d in due] == ["once1"]

    def test_fresh_own_machine_claim_with_budget_remaining_is_untouched(
        self, tmp_cron_dir, own_machine_id,
    ):
        """Even a same-machine-id claim is left alone when retry budget
        remains — the exhaustion check gates reconciliation, not just
        reapability."""
        now = j._hermes_now()
        claim = {"at": now.isoformat(), "by": own_machine_id}
        j.save_jobs([_oneshot_job(times=2, completed=1, run_claim=claim)])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == []
        assert j.get_job("once1")["run_claim"] == claim


class TestReconcileCalledOnceBeforeFirstTick:
    """(f): InProcessCronScheduler.start() reconciles before ticking."""

    def test_start_calls_reconcile_once_before_first_tick(self):
        from cron.scheduler_provider import InProcessCronScheduler

        calls = []

        def fake_reconcile(self):
            calls.append("reconcile")
            return None

        def fake_tick(*a, **k):
            calls.append("tick")
            return 0

        stop = threading.Event()
        prov = InProcessCronScheduler()
        with patch.object(InProcessCronScheduler, "reconcile", fake_reconcile), \
             patch("cron.scheduler.tick", side_effect=fake_tick), \
             patch("cron.jobs.record_ticker_heartbeat"):
            t = threading.Thread(
                target=prov.start, args=(stop,), kwargs={"interval": 0}, daemon=True,
            )
            t.start()
            assert _wait_until(lambda: len(calls) >= 1), "start() never called reconcile/tick"
            stop.set()
            t.join(timeout=5)

        assert not t.is_alive()
        assert calls[0] == "reconcile", "reconcile() must run before the first tick"
        assert calls.count("reconcile") == 1, "reconcile() must run exactly once per start()"

    def test_inprocess_reconcile_delegates_to_reconcile_orphaned_runs(self, tmp_cron_dir):
        """InProcessCronScheduler.reconcile() (the ABC hook override) drives
        the module-level reconcile_orphaned_runs()."""
        from cron.scheduler_provider import InProcessCronScheduler

        calls = []
        with patch("cron.scheduler.reconcile_orphaned_runs", side_effect=lambda: calls.append(1) or []):
            assert InProcessCronScheduler().reconcile() is None
        assert calls == [1]

    def test_inprocess_reconcile_swallows_errors(self):
        """A reconciliation bug must never prevent the ticker from starting —
        reconcile() catches and logs instead of propagating."""
        from cron.scheduler_provider import InProcessCronScheduler

        with patch(
            "cron.scheduler.reconcile_orphaned_runs",
            side_effect=RuntimeError("boom"),
        ):
            assert InProcessCronScheduler().reconcile() is None

    @pytest.mark.parametrize("exc", [SystemExit(1), KeyboardInterrupt()])
    def test_inprocess_reconcile_swallows_baseexception(self, exc):
        """BaseException too, mirroring the tick loop's guard (#32612): once
        T16's alert delivery lands, a SystemExit from a misbehaving provider
        SDK reached via _send_reconcile_alert must not escape reconcile() and
        prevent the ticker from ever starting. KeyboardInterrupt is swallowed
        for the same reason as in the tick loop — shutdown is driven by
        stop_event (set by the main thread's signal handler), not by
        exceptions in this daemon thread."""
        from cron.scheduler_provider import InProcessCronScheduler

        with patch("cron.scheduler.reconcile_orphaned_runs", side_effect=exc):
            assert InProcessCronScheduler().reconcile() is None


class TestGuardedOperatorAlert:
    """(g): the T16 guarded-import alert pattern."""

    def test_degrades_to_logger_warning_when_operator_alerts_module_missing(
        self, tmp_cron_dir, caplog,
    ):
        """Current state: hermes_cli.operator_alerts does not exist yet (T16
        ships it later). reconcile_orphaned_runs() must still reconcile the
        job and must not raise — it degrades to logger.warning."""
        assert "hermes_cli.operator_alerts" not in sys.modules
        with pytest.raises(ImportError):
            import hermes_cli.operator_alerts  # noqa: F401

        now = j._hermes_now()
        stale_marker = {"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:999"}
        j.save_jobs([_recurring_job(running_marker=stale_marker, last_status="running")])

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["rec1"]
        assert any(
            "reconciled at scheduler startup" in rec.message for rec in caplog.records
        ), "expected a logger.warning fallback when operator_alerts is unavailable"

    def test_routes_through_send_operator_alert_once_available(self, tmp_cron_dir, monkeypatch):
        """Once hermes_cli.operator_alerts.send_operator_alert exists (T16),
        the guarded import picks it up with no code change here."""
        import types

        fake_module = types.ModuleType("hermes_cli.operator_alerts")
        alerts = []
        fake_module.send_operator_alert = lambda message: alerts.append(message)
        monkeypatch.setitem(sys.modules, "hermes_cli.operator_alerts", fake_module)

        now = j._hermes_now()
        stale_marker = {"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:999"}
        j.save_jobs([_recurring_job(running_marker=stale_marker, last_status="running")])

        reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["rec1"]
        assert len(alerts) == 1
        assert "rec1" in alerts[0] or "recurring" in alerts[0]

    def test_alert_delivery_failure_does_not_block_reconciliation(
        self, tmp_cron_dir, monkeypatch, caplog,
    ):
        """If send_operator_alert itself raises, the job is still reconciled
        (last_status/last_run_at already committed via mark_job_run before
        the alert is attempted) and the failure is logged, not raised."""
        import types

        fake_module = types.ModuleType("hermes_cli.operator_alerts")

        def _boom(message):
            raise RuntimeError("alert channel down")

        fake_module.send_operator_alert = _boom
        monkeypatch.setitem(sys.modules, "hermes_cli.operator_alerts", fake_module)

        now = j._hermes_now()
        stale_marker = {"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:999"}
        j.save_jobs([_recurring_job(running_marker=stale_marker, last_status="running")])

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["rec1"]
        after = j.get_job("rec1")
        assert after["last_status"] == "error"
        assert any("alert delivery failed" in rec.message for rec in caplog.records)


class TestMultipleJobsReconciledInOnePass:
    def test_mixed_batch_reconciles_only_the_eligible_ones(self, tmp_cron_dir, own_machine_id):
        now = j._hermes_now()
        jobs = [
            _recurring_job(
                job_id="stale_recurring",
                running_marker={"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:1"},
                last_status="running",
            ),
            _recurring_job(
                job_id="fresh_foreign_recurring",
                running_marker={"at": now.isoformat(), "by": "otherhost:2"},
                last_status="running",
                next_run_at=(now - timedelta(seconds=5)).isoformat(),
            ),
            _oneshot_job(
                job_id="exhausted_oneshot",
                times=1, completed=1,
                run_claim={"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:3"},
            ),
            _oneshot_job(
                job_id="retryable_oneshot",
                times=3, completed=1,
                run_claim={"at": (now - timedelta(hours=3)).isoformat(), "by": "otherhost:4"},
            ),
        ]
        j.save_jobs(jobs)

        reconciled = s.reconcile_orphaned_runs()

        assert set(reconciled) == {"stale_recurring", "exhausted_oneshot"}
        assert j.get_job("fresh_foreign_recurring")["running_marker"] is not None
        assert j.get_job("retryable_oneshot")["run_claim"] is not None
        assert j.get_job("exhausted_oneshot") is None

    def test_per_job_failure_does_not_abort_remaining_jobs(self, tmp_cron_dir, monkeypatch, caplog):
        """One malformed job dict must not abort reconciliation of the rest —
        mark_job_run is wrapped per-job (mirrors mark_running_jobs_interrupted):
        the first job's mark_job_run raises, the second is still reconciled."""
        now = j._hermes_now()
        stale_at = (now - timedelta(hours=3)).isoformat()
        j.save_jobs([
            _recurring_job(
                job_id="poisoned",
                running_marker={"at": stale_at, "by": "otherhost:1"},
                last_status="running",
            ),
            _recurring_job(
                job_id="healthy",
                running_marker={"at": stale_at, "by": "otherhost:2"},
                last_status="running",
                next_run_at=(now - timedelta(seconds=5)).isoformat(),
            ),
        ])

        real_mark_job_run = s.mark_job_run

        def _mark(job_id, success, error=None, **kw):
            if job_id == "poisoned":
                raise RuntimeError("malformed job dict")
            return real_mark_job_run(job_id, success, error, **kw)

        monkeypatch.setattr(s, "mark_job_run", _mark)

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            reconciled = s.reconcile_orphaned_runs()

        assert reconciled == ["healthy"], (
            "the second job must still be reconciled after the first one's "
            "mark_job_run raised"
        )
        after = j.get_job("healthy")
        assert after["running_marker"] is None
        assert after["last_status"] == "error"
        # The poisoned job's state is untouched (its mark_job_run raised)...
        assert j.get_job("poisoned")["running_marker"] is not None
        # ...and the failure was logged, not swallowed silently.
        assert any(
            "Failed to reconcile orphaned job poisoned" in rec.message
            for rec in caplog.records
        )
