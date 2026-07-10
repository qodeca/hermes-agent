"""Tests for the gateway /allowlist show command.

The owner himself was denied after a restart with no way to answer "why was
I denied?" without reading code. ``/allowlist show`` surfaces the effective
authorization sources (env allowlists, pairing grants, allow-all flags,
including the per-platform ``PLATFORM_ALLOW_ALL_ENV``) as plain text.

The command is admin-gated (see ``GatewayCommandsMixin._handle_allowlist_
command``'s docstring): it requires an explicit ``allow_admin_from`` entry
for the caller, so most fixtures here configure the test source's user ID as
an admin. ``TestAllowlistShowAdminGate`` covers the denial path for a caller
who isn't.
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


def _make_runner(
    session_entry: SessionEntry,
    *,
    platform: Platform = Platform.TELEGRAM,
    admin_ids: tuple[str, ...] = ("u1",),
):
    """Build a bare GatewayRunner for /allowlist show tests.

    ``admin_ids`` populates ``allow_admin_from`` for the platform so the
    command's admin gate (see ``_handle_allowlist_command``) passes for a
    caller whose ``user_id`` is in the list — ``_make_source()`` below uses
    ``user_id="u1"``, matching the default. Pass ``admin_ids=()`` to build a
    runner with no admin configured (gating disabled -> denied for this
    command specifically) for the admin-gate denial tests.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            platform: PlatformConfig(
                enabled=True,
                token="***",
                extra={"allow_admin_from": list(admin_ids)} if admin_ids else {},
            )
        }
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
async def test_allowlist_show_reports_group_and_bot_policy_sources(monkeypatch):
    """The report must also surface the sources _is_user_authorized reads
    that the original /allowlist show implementation omitted: group-scoped
    allowlists (TELEGRAM_GROUP_ALLOWED_USERS / _CHATS, QQ_GROUP_ALLOWED_USERS)
    and the {PLATFORM}_ALLOW_BOTS policy."""
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_USERS", "333")
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100555")
    monkeypatch.setenv("QQ_GROUP_ALLOWED_USERS", "444")
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")

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

    assert "Group allowlists" in result
    assert "TELEGRAM_GROUP_ALLOWED_USERS: 333" in result
    assert "TELEGRAM_GROUP_ALLOWED_CHATS: -100555" in result
    assert "QQ_GROUP_ALLOWED_USERS: 444" in result
    assert "Bot policy" in result
    assert "DISCORD_ALLOW_BOTS: mentions" in result


@pytest.mark.asyncio
async def test_allowlist_show_reports_plugin_platform_env_vars(monkeypatch):
    """A plugin platform that registers its own allow_all_env /
    allowed_users_env (gateway.platform_registry.PlatformEntry) must show up
    in the report too — it already gates access via that same registry
    lookup in _is_user_authorized, so hiding it here would be a blind spot."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    monkeypatch.setenv("IRC_ALLOWED_USERS", "plugin-user")
    monkeypatch.setenv("IRC_ALLOW_ALL_USERS", "true")
    entry = PlatformEntry(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        allowed_users_env="IRC_ALLOWED_USERS",
        allow_all_env="IRC_ALLOW_ALL_USERS",
    )
    platform_registry.register(entry)
    try:
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

        assert "IRC_ALLOWED_USERS: plugin-user" in result
        assert "IRC_ALLOW_ALL_USERS: true" in result
    finally:
        platform_registry.unregister("irc")


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


class TestAllowlistShowAdminGate:
    """/allowlist show enumerates every allowlisted/paired user id, so it
    must stay admin-only even though slash-command gating is disabled by
    default (see ``_handle_allowlist_command``'s docstring: ordinary
    commands fall back to "everyone allowed to talk to the bot may run
    this" when ``allow_admin_from`` isn't configured — that default would
    let a stranger admitted by an active allow-all enumerate the allowlist).
    """

    @pytest.mark.asyncio
    async def test_no_admin_configured_denies_even_an_authorized_user(self):
        """Default gateway config (no allow_admin_from) — an authorized,
        allow-all-admitted user must still be refused the report."""
        session_entry = SessionEntry(
            session_key=build_session_key(_make_source()),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        # admin_ids=() -> no allow_admin_from configured for this platform.
        runner = _make_runner(session_entry, admin_ids=())

        result = await runner._handle_message(_make_event("/allowlist show"))

        assert "admin-only" in result
        assert "effective authorization sources" not in result

    @pytest.mark.asyncio
    async def test_non_admin_user_denied_when_admin_list_configured(self):
        """allow_admin_from IS configured, but the caller isn't on it."""
        session_entry = SessionEntry(
            session_key=build_session_key(_make_source()),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        # Admin list configured, but only for a different user than the
        # "u1" the default _make_source() carries.
        runner = _make_runner(session_entry, admin_ids=("owner-id",))

        result = await runner._handle_message(_make_event("/allowlist show"))

        assert "admin-only" in result
        assert "effective authorization sources" not in result

    @pytest.mark.asyncio
    async def test_admin_user_still_gets_the_report(self):
        """Sanity check: the admin allowlist DOES grant access."""
        session_entry = SessionEntry(
            session_key=build_session_key(_make_source()),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        runner = _make_runner(session_entry, admin_ids=("u1",))

        result = await runner._handle_message(_make_event("/allowlist show"))

        assert "effective authorization sources" in result
