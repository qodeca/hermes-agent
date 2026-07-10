"""Tests for the MCPServerTask reconnect signal.

When the OAuth layer cannot recover in-place (e.g., external refresh of a
single-use refresh_token made the SDK's in-memory refresh fail), the tool
handler signals MCPServerTask to tear down the current MCP session and
reconnect with fresh credentials. This file exercises the signal plumbing
in isolation from the full stdio/http transport machinery.
"""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_reconnect_event_attribute_exists():
    """MCPServerTask has a _reconnect_event alongside _shutdown_event."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")
    assert hasattr(task, "_reconnect_event")
    assert isinstance(task._reconnect_event, asyncio.Event)
    assert not task._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_wait_for_lifecycle_event_returns_reconnect():
    """When _reconnect_event fires, helper returns 'reconnect' and clears it."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")

    task._reconnect_event.set()
    reason = await task._wait_for_lifecycle_event()
    assert reason == "reconnect"
    # Should have cleared so the next cycle starts fresh
    assert not task._reconnect_event.is_set()


@pytest.mark.asyncio
async def test_wait_for_lifecycle_event_returns_shutdown():
    """When _shutdown_event fires, helper returns 'shutdown'."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")

    task._shutdown_event.set()
    reason = await task._wait_for_lifecycle_event()
    assert reason == "shutdown"


@pytest.mark.asyncio
async def test_wait_for_lifecycle_event_shutdown_wins_when_both_set():
    """If both events are set simultaneously, shutdown takes precedence."""
    from tools.mcp_tool import MCPServerTask
    task = MCPServerTask("test")

    task._shutdown_event.set()
    task._reconnect_event.set()
    reason = await task._wait_for_lifecycle_event()
    assert reason == "shutdown"


# ---------------------------------------------------------------------------
# Keepalive failure-log rollup (T12)
#
# A server down for hours previously logged an identical WARNING for every
# keepalive-triggered reconnect cycle. These tests lock in the rollup: only
# the first failed cycle in an outage logs WARNING; subsequent ones log
# DEBUG and increment a counter; recovery emits one rollup WARNING with the
# final count plus one recovery log, and only once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keepalive_failure_rollup_first_warning_then_debug(caplog):
    """First failed cycle logs WARNING; later ones in the same outage log
    DEBUG and increment the failure counter instead."""
    import logging
    from tools.mcp_tool import MCPServerTask

    task = MCPServerTask("test")
    caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")

    task._record_keepalive_failure(RuntimeError("down 1"))
    task._record_keepalive_failure(RuntimeError("down 2"))
    task._record_keepalive_failure(RuntimeError("down 3"))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]

    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "keepalive failed" in warnings[0].getMessage()
    assert len(debugs) == 2, [r.getMessage() for r in debugs]
    assert task._reconnect_failure_count == 3


@pytest.mark.asyncio
async def test_keepalive_recovery_emits_one_rollup_and_one_recovery_log(caplog):
    """Recovery emits exactly one rollup WARNING (with the failure count)
    plus one recovery log -- not on every subsequent successful probe."""
    import logging
    from tools.mcp_tool import MCPServerTask

    task = MCPServerTask("test")
    caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")

    for i in range(4):
        task._record_keepalive_failure(RuntimeError(f"down {i}"))

    caplog.clear()
    task._record_keepalive_recovery()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    assert "4 failed cycles" in warnings[0].getMessage()
    assert "unreachable" in warnings[0].getMessage()
    assert "recovered" in warnings[1].getMessage()
    assert task._reconnect_failure_count == 0
    assert task._outage_started_at is None
    assert task._last_reconnect_cycle_at is None

    caplog.clear()
    task._record_keepalive_recovery()  # already recovered -- no-op
    assert not caplog.records, "recovery must be logged once, not repeatedly"


@pytest.mark.asyncio
async def test_keepalive_failure_rollup_heartbeat_during_long_outage(monkeypatch, caplog):
    """A sustained outage still surfaces a WARNING periodically, not just at
    the start and the end."""
    import logging
    from tools import mcp_tool
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_ROLLUP_HEARTBEAT_INTERVAL", 100.0)
    task = MCPServerTask("test")
    caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")

    fake_now = [0.0]
    monkeypatch.setattr(mcp_tool.time, "monotonic", lambda: fake_now[0])

    task._record_keepalive_failure(RuntimeError("down 1"))  # t=0, WARNING
    fake_now[0] = 50.0
    task._record_keepalive_failure(RuntimeError("down 2"))  # t=50, DEBUG
    fake_now[0] = 150.0
    task._record_keepalive_failure(RuntimeError("down 3"))  # t=150, heartbeat WARNING

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    assert "unreachable" in warnings[1].getMessage()
