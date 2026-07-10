"""Tests for the mandatory request-timeout floor (T7, findings 4 & 23).

Production incident: a model API call ran 46 minutes with no client
timeout. Root causes:

1. Three Anthropic client-build sites in ``run_agent.py``
   (``_try_refresh_anthropic_client_credentials``, ``_swap_credential``,
   ``_rebuild_anthropic_client``) passed the raw
   ``get_provider_request_timeout()`` getter straight to
   ``build_anthropic_client(..., timeout=...)``. That getter returns
   ``None`` whenever the provider config sets no
   ``request_timeout_seconds`` / ``timeout_seconds`` — its contract is
   intentionally "None when unset" because other consumers rely on that
   meaning. Routing it *unresolved* into a client builder means an
   unconfigured provider gets whatever bare-``None`` fallback the SDK
   plumbing happens to apply, instead of hermes's own config -> env ->
   1800s default chain (``AIAgent._resolved_api_call_timeout()``).

2. ``AIAgent._compute_non_stream_stale_timeout`` returned ``float("inf")``
   (no stale-call detection at all) whenever the stale-timeout base was
   the implicit default *and* the endpoint looked local. A finite,
   generous ceiling should apply instead so no call is ever fully
   unguarded.

These tests pin: (a) all three Anthropic client-build sites route their
``timeout=`` kwarg through ``self._resolved_api_call_timeout()``, so an
unconfigured provider gets the 1800s floor (or the configured value, when
set) rather than a bare ``None``; (b) the stale-timeout detector's
implicit-default/local-endpoint case returns a finite ceiling.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _write_config(tmp_path: Path, body: str) -> None:
    (tmp_path / "config.yaml").write_text(body or "{}\n", encoding="utf-8")


def _make_anthropic_agent(**overrides):
    """Minimal AIAgent for exercising the Anthropic client-build helpers.

    Bypasses ``__init__`` (network/auth resolution) the same way
    ``test_restore_primary_pool_reselect.py`` does — these helpers only
    touch the handful of attributes they read/write.
    """
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.model = "claude-opus-4-7"
    agent.provider = "anthropic"
    agent.api_mode = "anthropic_messages"
    agent.base_url = "https://api.anthropic.com"
    agent._anthropic_api_key = "sk-ant-old"
    agent._anthropic_base_url = "https://api.anthropic.com"
    agent._anthropic_client = MagicMock()
    agent._is_anthropic_oauth = False
    agent.api_key = "sk-ant-old"
    agent._client_kwargs = {}
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


def _isolate_config(monkeypatch, tmp_path: Path, body: str = "") -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.delenv("HERMES_API_TIMEOUT", raising=False)
    _write_config(tmp_path, body)


# ── Finding 4: the three Anthropic client-build sites ──────────────────────


class TestRebuildAnthropicClientTimeoutFloor:
    """``_rebuild_anthropic_client`` (run_agent.py ~L4586-4591)."""

    def test_no_config_gets_1800s_floor_not_none(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        agent = _make_anthropic_agent()
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with patch("agent.anthropic_adapter.build_anthropic_client", _fake_build):
            agent._rebuild_anthropic_client()

        assert captured["timeout"] is not None
        assert captured["timeout"] == 1800.0

    def test_configured_provider_timeout_wins(self, monkeypatch, tmp_path):
        _isolate_config(
            monkeypatch,
            tmp_path,
            """\
providers:
  anthropic:
    request_timeout_seconds: 45
""",
        )
        agent = _make_anthropic_agent()
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with patch("agent.anthropic_adapter.build_anthropic_client", _fake_build):
            agent._rebuild_anthropic_client()

        assert captured["timeout"] == 45.0


class TestSwapCredentialTimeoutFloor:
    """``_swap_credential`` (run_agent.py ~L4518-4521)."""

    def test_no_config_gets_1800s_floor_not_none(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        agent = _make_anthropic_agent()
        entry = SimpleNamespace(
            runtime_api_key="sk-ant-new",
            runtime_base_url="https://api.anthropic.com",
        )
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with patch("agent.anthropic_adapter.build_anthropic_client", _fake_build):
            agent._swap_credential(entry)

        assert captured["timeout"] is not None
        assert captured["timeout"] == 1800.0

    def test_configured_provider_timeout_wins(self, monkeypatch, tmp_path):
        _isolate_config(
            monkeypatch,
            tmp_path,
            """\
providers:
  anthropic:
    request_timeout_seconds: 45
""",
        )
        agent = _make_anthropic_agent()
        entry = SimpleNamespace(
            runtime_api_key="sk-ant-new",
            runtime_base_url="https://api.anthropic.com",
        )
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with patch("agent.anthropic_adapter.build_anthropic_client", _fake_build):
            agent._swap_credential(entry)

        assert captured["timeout"] == 45.0


class TestRefreshAnthropicCredentialsTimeoutFloor:
    """``_try_refresh_anthropic_client_credentials`` (run_agent.py ~L4394-4399)."""

    def test_no_config_gets_1800s_floor_not_none(self, monkeypatch, tmp_path):
        _isolate_config(monkeypatch, tmp_path)
        agent = _make_anthropic_agent()
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with (
            patch("agent.anthropic_adapter.build_anthropic_client", _fake_build),
            patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="sk-ant-new"),
        ):
            result = agent._try_refresh_anthropic_client_credentials()

        assert result is True
        assert captured["timeout"] is not None
        assert captured["timeout"] == 1800.0

    def test_configured_provider_timeout_wins(self, monkeypatch, tmp_path):
        _isolate_config(
            monkeypatch,
            tmp_path,
            """\
providers:
  anthropic:
    request_timeout_seconds: 45
""",
        )
        agent = _make_anthropic_agent()
        captured = {}

        def _fake_build(api_key, base_url, timeout=None, **kw):
            captured["timeout"] = timeout
            return MagicMock()

        with (
            patch("agent.anthropic_adapter.build_anthropic_client", _fake_build),
            patch("agent.anthropic_adapter.resolve_anthropic_token", return_value="sk-ant-new"),
        ):
            result = agent._try_refresh_anthropic_client_credentials()

        assert result is True
        assert captured["timeout"] == 45.0


# ── Finding 23: no fully-unguarded stale-call detector ──────────────────────


def _make_local_agent(tmp_path: Path, **overrides):
    from run_agent import AIAgent

    kwargs = dict(
        model="llama3",
        provider="ollama",
        api_key="sk-dummy",
        base_url="http://localhost:11434",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


class TestStaleTimeoutNeverFullyUnguarded:
    def test_implicit_default_local_endpoint_is_finite(self, monkeypatch, tmp_path):
        """Reported gap: implicit-default + local endpoint disabled the
        stale-call detector entirely (float('inf')). It must now be a
        finite, generous ceiling so no call is ever fully unguarded."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("", encoding="utf-8")
        monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
        _write_config(tmp_path, "")

        agent = _make_local_agent(tmp_path)
        base, implicit = agent._resolved_api_call_stale_timeout_base()
        assert base == 90.0
        assert implicit is True

        timeout = agent._compute_non_stream_stale_timeout({"input": "hi"})
        assert timeout != float("inf")
        assert timeout == 900.0

    def test_explicit_local_config_still_respected(self, monkeypatch, tmp_path):
        """An explicit user-configured stale timeout for a local endpoint is
        NOT implicit and must still be honored verbatim (no floor override)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text("", encoding="utf-8")
        monkeypatch.delenv("HERMES_API_CALL_STALE_TIMEOUT", raising=False)
        _write_config(
            tmp_path,
            """\
providers:
  ollama:
    stale_timeout_seconds: 30
""",
        )

        import importlib
        from hermes_cli import timeouts as to_mod
        importlib.reload(to_mod)

        agent = _make_local_agent(tmp_path)
        timeout = agent._compute_non_stream_stale_timeout({"input": "hi"})
        assert timeout == 30.0
