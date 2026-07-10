"""Tests for the gateway /allowlist show command (finding #29 — T22).

The owner himself was denied after a restart with no way to answer "why was
I denied?" without reading code. ``/allowlist show`` surfaces the effective
authorization sources (env allowlists, pairing grants, allow-all flags,
including T21's per-platform ``PLATFORM_ALLOW_ALL_ENV``) as plain text.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_state import AsyncSessionDB


@pytest.fixture(autouse=True)
def _isolate_allowlist_env(monkeypatch):
    for var in (
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "TELEGRAM_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "DISCORD_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(platform),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry, *, platform: Platform = Platform.TELEGRAM):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = AsyncSessionDB(MagicMock())
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner.pairing_store = SimpleNamespace(list_approved=lambda: [])
    runner.pairing_stores = {}
    return runner


@pytest.mark.asyncio
async def test_allowlist_show_dispatches_via_handle_message():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)

    result = await runner._handle_message(_make_event("/allowlist show"))

    assert "effective authorization sources" in result
    assert "GATEWAY_ALLOW_ALL_USERS: (not set)" in result


@pytest.mark.asyncio
async def test_allowlist_show_reports_configured_sources(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111,222")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)

    result = await runner._handle_message(_make_event("/allowlist show"))

    assert "TELEGRAM_ALLOWED_USERS: 111,222" in result
    assert "DISCORD_ALLOW_ALL_USERS: true" in result


@pytest.mark.asyncio
async def test_allowlist_show_lists_paired_users_from_profile_store():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    # Per-profile store must win over the global default when the source
    # carries a registered profile (mirrors _pairing_store_for's contract).
    runner.pairing_store = SimpleNamespace(list_approved=lambda: [{"platform": "telegram", "user_id": "wrong-store"}])
    profile_store = SimpleNamespace(
        list_approved=lambda: [
            {"platform": "telegram", "user_id": "owner-id", "user_name": "Marcin"}
        ]
    )
    runner.pairing_stores = {"work": profile_store}
    source = _make_source()
    source.profile = "work"
    event = MessageEvent(text="/allowlist show", source=source, message_id="m1")

    result = await runner._handle_message(event)

    assert "telegram: owner-id (Marcin)" in result
    assert "wrong-store" not in result


@pytest.mark.asyncio
async def test_allowlist_bare_returns_usage():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)

    result = await runner._handle_message(_make_event("/allowlist"))

    assert result == "Usage: /allowlist show"
