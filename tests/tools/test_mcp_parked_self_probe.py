"""Tests for the parked-server self-probe revival path (#57129).

Parking deregisters a server's tools, so no tool call can reach the
circuit-breaker half-open probe or ``_signal_reconnect`` — the only
things that set ``_reconnect_event``. The parked wait must therefore be
timed: the run task wakes on ``_PARKED_RETRY_INTERVAL`` and attempts one
revival probe on its own.
"""

import asyncio
import time

import pytest


@pytest.mark.no_isolate
def test_parked_server_self_probes_and_revives(monkeypatch, tmp_path):
    """A parked server must revive on its own once the backend recovers,
    without any explicit _reconnect_event.set()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_MAX_RECONNECT_RETRIES", 1)
    # Keep the self-probe cadence tiny so the test is fast.
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.05)

    _real_sleep = asyncio.sleep

    async def _fast_sleep(_delay, *a, **kw):
        await _real_sleep(0)

    monkeypatch.setattr(mcp_tool.asyncio, "sleep", _fast_sleep)

    state = {
        "transport_calls": 0,
        "deregistered": 0,
        "backend_up": False,
        "revived_registration": 0,
    }

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            def _deregister_tools(self):
                state["deregistered"] += 1
                self._registered_tool_names = []

            def _register_discovered_tools_if_needed(self):
                if self._ready.is_set() and not self._registered_tool_names:
                    state["revived_registration"] += 1
                    self._registered_tool_names = ["srv__tool"]

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                if state["transport_calls"] == 1:
                    # First connect succeeds (sets _ready), then dies.
                    self.session = object()
                    self._ready.set()
                    self.session = None
                    raise RuntimeError("backend outage begins")
                if not state["backend_up"]:
                    raise RuntimeError("backend still down")
                # Backend recovered: establish a session and park in the
                # lifecycle wait like the real transport does.
                self.session = object()
                self._register_discovered_tools_if_needed()
                await self._wait_for_lifecycle_event()

        task = _Task("srv")
        task._registered_tool_names = ["srv__tool"]

        run_task = asyncio.ensure_future(task.run({"command": "x"}))

        # Let it exhaust the budget (1 retry) and park.
        for _ in range(2000):
            await _real_sleep(0)
            if state["deregistered"] >= 1:
                break
        assert state["deregistered"] >= 1, "server never parked"
        assert not run_task.done(), "run task exited instead of parking"

        # The backend comes back. NOTHING sets _reconnect_event — revival
        # must come from the timed self-probe alone.
        state["backend_up"] = True
        for _ in range(200):
            await _real_sleep(0.01)
            if task.session is not None:
                break

        assert task.session is not None, (
            "parked server never self-probed back to life "
            f"(transport_calls={state['transport_calls']})"
        )
        assert state["revived_registration"] >= 1, (
            "revived server did not re-register its tools"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Keepalive reconnect dwell (T12)
#
# The incident: a server down all night produced identical 3-attempt
# reconnect cycles back-to-back with no pause. The keepalive-failure path
# (a session that comes back up just long enough to fail its very next
# liveness probe) never raises out of _run_http/_run_stdio, so it never
# reaches the exception-branch backoff/park machinery exercised by the
# tests above -- these tests cover the separate dwell added to the *clean*
# "reconnect" return path for exactly that case, using real wall-clock
# timing (not the fast-sleep patch used elsewhere in this file, since the
# thing under test IS the elapsed time).
# ---------------------------------------------------------------------------


class _DeadSession:
    """Fake MCP session whose keepalive probe always fails."""

    async def send_ping(self):
        raise RuntimeError("keepalive down")


def test_keepalive_failure_dwells_before_next_reconnect_cycle(monkeypatch, tmp_path):
    """Two keepalive-triggered reconnect cycles in a row must be spaced
    apart by (about) _PARKED_RETRY_INTERVAL, not fire back-to-back."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.3)
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)

    state = {"transport_calls": 0}
    call_times: list = []

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                call_times.append(time.monotonic())
                if state["transport_calls"] <= 2:
                    self.session = _DeadSession()
                    self._ready.set()
                    return await self._wait_for_lifecycle_event()
                self._shutdown_event.set()
                return "shutdown"

        task = _Task("srv")
        task._registered_tool_names = []

        run_task = asyncio.ensure_future(
            task.run({"command": "x", "keepalive_interval": 0.01})
        )

        for _ in range(800):
            await asyncio.sleep(0.01)
            if state["transport_calls"] >= 3 or run_task.done():
                break

        assert state["transport_calls"] >= 3, (
            f"third reconnect cycle never started "
            f"(transport_calls={state['transport_calls']})"
        )
        gap_1_2 = call_times[1] - call_times[0]
        gap_2_3 = call_times[2] - call_times[1]
        assert gap_1_2 < 0.25, (
            f"the very first keepalive failure must not dwell, got {gap_1_2:.3f}s"
        )
        assert gap_2_3 >= 0.25, (
            f"the second keepalive-triggered cycle must dwell ~0.3s, "
            f"got {gap_2_3:.3f}s"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())


def test_keepalive_dwell_interrupted_by_shutdown(monkeypatch, tmp_path):
    """Shutdown must interrupt an in-progress dwell immediately, not wait
    out the remaining interval."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 5.0)
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)

    state = {"transport_calls": 0}

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                self.session = _DeadSession()
                self._ready.set()
                return await self._wait_for_lifecycle_event()

        task = _Task("srv")
        task._registered_tool_names = []

        run_task = asyncio.ensure_future(
            task.run({"command": "x", "keepalive_interval": 0.01})
        )

        # Wait for the second cycle to begin dwelling.
        for _ in range(800):
            await asyncio.sleep(0.005)
            if state["transport_calls"] >= 2:
                break
        assert state["transport_calls"] >= 2, "second cycle never started"

        # Give the dwell a moment to actually be in progress.
        await asyncio.sleep(0.05)
        start = time.monotonic()
        task._shutdown_event.set()

        await asyncio.wait_for(run_task, timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, (
            f"shutdown took {elapsed:.2f}s to interrupt a 5.0s dwell"
        )
        assert state["transport_calls"] == 2, (
            "no third reconnect cycle should have started after shutdown"
        )

    asyncio.run(_scenario())


def test_manual_reconnect_signal_bypasses_dwell(monkeypatch, tmp_path):
    """An explicit _signal_reconnect() (manual /mcp refresh, OAuth recovery,
    the tool handler's dead-session nudge) reconnects immediately even
    while a keepalive-triggered dwell would otherwise apply."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask, _signal_reconnect

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 5.0)
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)
    monkeypatch.setattr(mcp_tool, "_mcp_loop", None)  # drive .set() directly

    state = {"transport_calls": 0}

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                if state["transport_calls"] >= 3:
                    self._shutdown_event.set()
                    return "shutdown"
                self.session = _DeadSession()
                self._ready.set()
                return await self._wait_for_lifecycle_event()

        task = _Task("srv")
        task._registered_tool_names = []

        run_task = asyncio.ensure_future(
            task.run({"command": "x", "keepalive_interval": 0.01})
        )

        for _ in range(800):
            await asyncio.sleep(0.005)
            if state["transport_calls"] >= 2:
                break
        assert state["transport_calls"] >= 2, "second cycle never started"

        # The server is now dwelling on a 5s budget. A manual refresh must
        # break through it immediately.
        await asyncio.sleep(0.02)
        start = time.monotonic()
        assert _signal_reconnect(task)

        for _ in range(800):
            await asyncio.sleep(0.005)
            if state["transport_calls"] >= 3:
                break
        elapsed = time.monotonic() - start
        assert state["transport_calls"] >= 3, (
            "manual reconnect signal did not bypass the dwell"
        )
        assert elapsed < 1.0, (
            f"manual refresh took {elapsed:.2f}s to take effect "
            f"(dwell budget was 5.0s)"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())


def test_keepalive_recovery_clears_dwell_for_later_isolated_blip(monkeypatch, tmp_path):
    """A successful reconnect that stays healthy for a while must clear the
    dwell timestamp, so a later isolated blip reconnects immediately rather
    than inheriting the earlier outage's dwell."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 5.0)
    monkeypatch.setattr(mcp_tool, "_MIN_KEEPALIVE_INTERVAL", 0.01)

    class _FlakySession:
        def __init__(self, healthy_flag):
            self._healthy = healthy_flag

        async def send_ping(self):
            if not self._healthy["ok"]:
                raise RuntimeError("keepalive down again")

    healthy_flag = {"ok": True}
    state = {"transport_calls": 0}
    call_times: list = []

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return False

            async def _run_stdio(self, config):
                state["transport_calls"] += 1
                call_times.append(time.monotonic())
                if state["transport_calls"] == 1:
                    self.session = _DeadSession()
                    self._ready.set()
                    return await self._wait_for_lifecycle_event()
                if state["transport_calls"] == 2:
                    self.session = _FlakySession(healthy_flag)
                    self._ready.set()
                    return await self._wait_for_lifecycle_event()
                self._shutdown_event.set()
                return "shutdown"

        task = _Task("srv")
        task._registered_tool_names = []

        run_task = asyncio.ensure_future(
            task.run({"command": "x", "keepalive_interval": 0.01})
        )

        for _ in range(800):
            await asyncio.sleep(0.005)
            if state["transport_calls"] >= 2:
                break
        assert state["transport_calls"] >= 2, "second cycle never started"

        # Let several successful probes clear the outage/dwell state.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if task._last_reconnect_cycle_at is None:
                break
        assert task._last_reconnect_cycle_at is None, (
            "a healthy keepalive run should have cleared the dwell timestamp"
        )

        # Simulate an isolated later blip.
        healthy_flag["ok"] = False
        for _ in range(800):
            await asyncio.sleep(0.005)
            if state["transport_calls"] >= 3:
                break

        assert state["transport_calls"] >= 3, "isolated blip never reconnected"
        gap = call_times[2] - call_times[1]
        assert gap < 1.0, (
            f"isolated blip after a healthy period should reconnect "
            f"immediately, took {gap:.2f}s (dwell budget was 5.0s)"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())
