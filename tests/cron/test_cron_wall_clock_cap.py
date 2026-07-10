"""Tests for the cron wall-clock runtime cap.

The pre-existing inactivity timeout (HERMES_CRON_TIMEOUT) resets on every
API/tool/stream event, so a job that stays *active* on a failing backend
(e.g. retrying a slow/hanging call in a loop) can run unbounded — an
overnight incident saw a job run 2.4+ hours this way. This cap bounds the
TOTAL wall-clock runtime of a single job run regardless of activity.

Tests cover:
- Resolution precedence: env (HERMES_CRON_MAX_RUNTIME) > config
  (cron.max_runtime_seconds) > default (3600s)
- 0 (from either env or config) means unlimited
- Invalid env value falls back to the default with a warning, not a crash
- The poll loop trips the wall-clock cap for an agent that is always
  "active" (idle_seconds pinned at 0) and never finishes, calling
  agent.interrupt() and surfacing a TimeoutError-equivalent condition
- The wall-clock cap and the inactivity cap are independent: whichever
  trips first governs
"""

import concurrent.futures
import os
import sys
import time
from pathlib import Path


# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _resolve_cron_max_runtime  # noqa: E402


class FakeAgent:
    """Mock agent with controllable activity summary for timeout tests."""

    def __init__(self, idle_seconds=0.0, activity_desc="tool_call",
                 current_tool=None, api_call_count=5, max_iterations=90):
        self._idle_seconds = idle_seconds
        self._activity_desc = activity_desc
        self._current_tool = current_tool
        self._api_call_count = api_call_count
        self._max_iterations = max_iterations
        self._interrupted = False
        self._interrupt_msg = None

    def get_activity_summary(self):
        return {
            "last_activity_ts": time.time() - self._idle_seconds,
            "last_activity_desc": self._activity_desc,
            "seconds_since_activity": self._idle_seconds,
            "current_tool": self._current_tool,
            "api_call_count": self._api_call_count,
            "max_iterations": self._max_iterations,
        }

    def interrupt(self, msg):
        self._interrupted = True
        self._interrupt_msg = msg


class AlwaysActiveAgent(FakeAgent):
    """Agent that never goes idle (activity constantly refreshed) and never
    finishes on its own — only a wall-clock cap can stop it. Simulates a
    slow-but-active job hammering a failing backend."""

    def __init__(self, run_duration=5.0, **kwargs):
        super().__init__(idle_seconds=0.0, **kwargs)
        self._run_duration = run_duration

    def get_activity_summary(self):
        # Always report zero seconds since activity: this is what a
        # job that's continuously retrying a slow API call would report,
        # since _touch_activity() fires on every attempt.
        summary = super().get_activity_summary()
        summary["seconds_since_activity"] = 0.0
        return summary

    def run_conversation(self, prompt):
        time.sleep(self._run_duration)
        return {"final_response": "finally done", "messages": []}


def _run_poll_loop(agent, *, inactivity_limit, runtime_limit, poll_interval=0.05):
    """Mirror the scheduler's run_job() poll loop (cron/scheduler.py ~L3106-3134)."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(agent.run_conversation, "test prompt")
    run_started = time.monotonic()
    inactivity_timeout = False
    runtime_timeout = False
    result = None

    try:
        if inactivity_limit is None and runtime_limit is None:
            result = future.result()
        else:
            while True:
                done, _ = concurrent.futures.wait({future}, timeout=poll_interval)
                if done:
                    result = future.result()
                    break
                elapsed = time.monotonic() - run_started
                if runtime_limit is not None and elapsed >= runtime_limit:
                    runtime_timeout = True
                    break
                idle_secs = 0.0
                if hasattr(agent, "get_activity_summary"):
                    try:
                        act = agent.get_activity_summary()
                        idle_secs = act.get("seconds_since_activity", 0.0)
                    except Exception:
                        pass
                if inactivity_limit is not None and idle_secs >= inactivity_limit:
                    inactivity_timeout = True
                    break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if inactivity_timeout or runtime_timeout:
        if hasattr(agent, "interrupt"):
            agent.interrupt("Cron job timed out")

    return result, inactivity_timeout, runtime_timeout


class TestWallClockCapTripsLoop:
    """Test the wall-clock cap branch of the poll loop directly."""

    def test_always_active_agent_is_capped_by_wall_clock(self):
        """An agent reporting constant activity (idle always ~0) that never
        finishes on its own must still be stopped by the wall-clock cap."""
        agent = AlwaysActiveAgent(run_duration=5.0)

        result, inactivity_timeout, runtime_timeout = _run_poll_loop(
            agent,
            inactivity_limit=600.0,  # would never trip: agent is never idle
            runtime_limit=0.3,
            poll_interval=0.05,
        )

        assert runtime_timeout is True
        assert inactivity_timeout is False
        assert result is None  # never got a result — interrupted first
        assert agent._interrupted is True

    def test_inactivity_cap_can_still_trip_first(self):
        """When the agent goes idle before the wall-clock cap is reached,
        the (cheaper, more specific) inactivity path still governs."""
        class GoesIdleAgent(FakeAgent):
            def __init__(self, **kwargs):
                super().__init__(idle_seconds=0.0, **kwargs)
                self._start = None

            def get_activity_summary(self):
                summary = super().get_activity_summary()
                if self._start is not None:
                    summary["seconds_since_activity"] = time.time() - self._start
                return summary

            def run_conversation(self, prompt):
                self._start = time.time()
                time.sleep(5.0)
                return {"final_response": "done", "messages": []}

        agent = GoesIdleAgent()
        result, inactivity_timeout, runtime_timeout = _run_poll_loop(
            agent,
            inactivity_limit=0.2,
            runtime_limit=600.0,
            poll_interval=0.05,
        )

        assert inactivity_timeout is True
        assert runtime_timeout is False
        assert result is None
        assert agent._interrupted is True

    def test_no_cap_configured_waits_for_completion(self):
        """Both limits None (unlimited) — the loop just awaits the result."""
        agent = FakeAgent(idle_seconds=0.0)
        agent.run_conversation = lambda prompt: {"final_response": "Done", "messages": []}

        result, inactivity_timeout, runtime_timeout = _run_poll_loop(
            agent, inactivity_limit=None, runtime_limit=None,
        )

        assert result["final_response"] == "Done"
        assert not inactivity_timeout
        assert not runtime_timeout
        assert not agent._interrupted


class TestResolveCronMaxRuntime:
    """Test the env > config > default resolution helper directly (no
    duplicated parsing logic — this imports the real implementation)."""

    def test_default_is_3600_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        assert _resolve_cron_max_runtime({}) == 3600.0
        assert _resolve_cron_max_runtime({"cron": {}}) == 3600.0

    def test_config_value_used_when_no_env(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        cfg = {"cron": {"max_runtime_seconds": 1800}}
        assert _resolve_cron_max_runtime(cfg) == 1800.0

    def test_env_beats_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "120")
        cfg = {"cron": {"max_runtime_seconds": 1800}}
        assert _resolve_cron_max_runtime(cfg) == 120.0

    def test_env_zero_means_unlimited(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "0")
        cfg = {"cron": {"max_runtime_seconds": 1800}}
        assert _resolve_cron_max_runtime(cfg) is None

    def test_config_zero_means_unlimited(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        cfg = {"cron": {"max_runtime_seconds": 0}}
        assert _resolve_cron_max_runtime(cfg) is None

    def test_invalid_env_falls_back_to_default_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "not-a-number")
        cfg = {"cron": {"max_runtime_seconds": 1800}}
        with caplog.at_level("WARNING"):
            result = _resolve_cron_max_runtime(cfg)
        assert result == 3600.0
        assert any("HERMES_CRON_MAX_RUNTIME" in rec.message for rec in caplog.records)

    def test_invalid_config_value_falls_back_to_default_with_warning(self, monkeypatch, caplog):
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        cfg = {"cron": {"max_runtime_seconds": "not-a-number"}}
        with caplog.at_level("WARNING"):
            result = _resolve_cron_max_runtime(cfg)
        assert result == 3600.0
        assert any("max_runtime_seconds" in rec.message for rec in caplog.records)

    def test_empty_env_uses_config(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_MAX_RUNTIME", "")
        cfg = {"cron": {"max_runtime_seconds": 900}}
        assert _resolve_cron_max_runtime(cfg) == 900.0

    def test_non_dict_cfg_uses_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_CRON_MAX_RUNTIME", raising=False)
        assert _resolve_cron_max_runtime(None) == 3600.0
        assert _resolve_cron_max_runtime({"cron": "not-a-dict"}) == 3600.0
