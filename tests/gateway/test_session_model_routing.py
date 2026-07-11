"""First-turn task-complexity routing for gateway sessions (T26).

The routed decision is seeded ONCE, on a session's first turn, into the
existing ``_session_model_overrides`` sticky map (with a ``routed`` marker),
so ``_resolve_session_agent_runtime`` returns the SAME model on every
subsequent turn — the model is fixed at conversation start and never
revisited, preserving per-conversation prompt-cache byte-stability.

Precedence (highest first): session ``/model`` override > channel override >
runtime-provider model > router > global config default. The router only
fills the global-default gap; every explicit choice skips routing entirely.

Covers:
  - first turn seeds the override map with the routed model + ``routed`` marker
  - turns 2..N reuse the seeded override (``route_model`` called exactly once)
  - the seeded override survives a simulated gateway restart without re-routing
  - a user /model override wins (skips routing; replaces a routed entry)
  - channel overrides and runtime-provider models skip routing entirely
  - routing disabled / gateway not in ``apply_to`` → no seeding, default model
  - a broken router import or a raising router fails open to the default model
  - a routed tier that names its own provider resolves its own credentials
    (never the default provider's key), and skips the route if that fails
"""

from unittest.mock import MagicMock, patch

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.session import SessionSource, SessionStore

LIGHT_MODEL = "vendor/light-model"
DEFAULT_MODEL = "vendor/default-model"

# < light_max_chars, no heavy keywords, no URLs, no numbered steps → light tier
SHORT_TASK = "Morning! Just saying hello."


def make_user_config(*, enabled=True, apply_to=("gateway",), light_tier=None):
    return {
        "model": {"default": DEFAULT_MODEL},
        "routing": {
            "enabled": enabled,
            "apply_to": list(apply_to),
            "classifier": "heuristic",
            "default_tier": "standard",
            "tiers": {
                "light": light_tier or {"model": LIGHT_MODEL},
                "standard": {"model": "vendor/standard-model"},
                "heavy": {"model": "vendor/heavy-model"},
            },
            # The default platform toolsets include heavy ones (e.g. browser),
            # which would floor every route at "heavy" — pin the heavy-toolset
            # trigger to a name that is never attached so SHORT_TASK routes
            # light deterministically.
            "heuristics": {"heavy_toolsets": ["never-attached-toolset"]},
        },
    }


def _default_runtime_kwargs():
    """Shape returned by gateway.run._resolve_runtime_agent_kwargs (no model)."""
    return {
        "api_key": "sk-default",
        "base_url": None,
        "provider": "openai",
        "api_mode": None,
        "command": None,
        "args": [],
        "credential_pool": None,
        "max_tokens": None,
    }


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


@pytest.fixture
def store_factory(tmp_path, monkeypatch):
    """Build SessionStores over a shared sessions dir, without SQLite."""

    def _raise():
        raise RuntimeError("SQLite disabled in test")

    import hermes_state

    monkeypatch.setattr(hermes_state, "SessionDB", _raise)

    def _make() -> SessionStore:
        store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
        assert store._db is None
        return store

    return _make


def _make_runner(store, config=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.session_store = store
    runner.config = config or GatewayConfig()
    return runner


def _seed(runner, session_key, source, cfg, text=SHORT_TASK):
    runner._maybe_route_session_model(
        session_key=session_key,
        source=source,
        task_text=text,
        user_config=cfg,
    )


def _resolve(runner, source, session_key, cfg):
    with patch(
        "gateway.run._resolve_runtime_agent_kwargs",
        return_value=_default_runtime_kwargs(),
    ):
        model, runtime_kwargs = runner._resolve_session_agent_runtime(
            source=source, session_key=session_key, user_config=cfg
        )
    return model, runtime_kwargs


class TestFirstTurnSeeding:
    def test_first_turn_seeds_routed_override(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)

        override = runner._session_model_overrides[session_key]
        assert override["model"] == LIGHT_MODEL
        assert override.get("routed") is True

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == LIGHT_MODEL

    def test_second_turn_reuses_override_route_model_called_once(
        self, store_factory
    ):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        import agent.model_router as model_router

        with patch(
            "agent.model_router.route_model", wraps=model_router.route_model
        ) as spy, patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)  # turn 1
            _seed(runner, session_key, source, cfg)  # turn 2 (defensive re-entry)

        assert spy.call_count == 1

        model1, _ = _resolve(runner, source, session_key, cfg)
        model2, _ = _resolve(runner, source, session_key, cfg)
        assert model1 == model2 == LIGHT_MODEL

    def test_routed_override_survives_restart_without_reroute(
        self, store_factory
    ):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)

        # The non-secret part is written through to the session store.
        persisted = store.get_model_override(session_key)
        assert persisted is not None
        assert persisted["model"] == LIGHT_MODEL

        # Simulated restart: fresh store + fresh runner. The persisted
        # override rehydrates; route_model must NOT run again — the model
        # was fixed at conversation start.
        runner2 = _make_runner(store_factory())
        with patch("agent.model_router.route_model") as spy, patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner2, session_key, source, cfg)
        assert spy.call_count == 0

        model, _ = _resolve(runner2, source, session_key, cfg)
        assert model == LIGHT_MODEL


class TestPrecedence:
    def test_existing_user_override_skips_routing(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        # Simulate a prior /model command (slash_commands.py stores this shape).
        user_override = {
            "model": "vendor/user-model",
            "provider": "openai",
            "api_key": "sk-user",
            "base_url": None,
            "api_mode": None,
        }
        runner._session_model_overrides[session_key] = user_override

        with patch("agent.model_router.route_model") as spy:
            _seed(runner, session_key, source, cfg)
        assert spy.call_count == 0
        assert runner._session_model_overrides[session_key] is user_override

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == "vendor/user-model"

    def test_model_command_replaces_routed_entry(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)
        assert runner._session_model_overrides[session_key]["routed"] is True

        # /model performs an unconditional assignment — the user's choice
        # replaces the routed entry and wins from then on.
        runner._session_model_overrides[session_key] = {
            "model": "vendor/user-model",
            "provider": "openai",
            "api_key": "sk-user",
            "base_url": None,
            "api_mode": None,
        }
        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == "vendor/user-model"

    def test_channel_override_skips_routing(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        gw_config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "c1": ChannelOverride(model="vendor/channel-model"),
                    },
                ),
            },
        )
        runner = _make_runner(store, config=gw_config)
        cfg = make_user_config()

        with patch("agent.model_router.route_model") as spy:
            _seed(runner, session_key, source, cfg)
        assert spy.call_count == 0
        assert session_key not in runner._session_model_overrides

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == "vendor/channel-model"

    def test_runtime_provider_model_skips_routing(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        runtime = _default_runtime_kwargs()
        runtime["model"] = "vendor/runtime-model"
        with patch(
            "gateway.run._resolve_runtime_agent_kwargs", return_value=runtime
        ), patch("agent.model_router.route_model") as spy:
            _seed(runner, session_key, source, cfg)
        assert spy.call_count == 0
        assert session_key not in runner._session_model_overrides


class TestGating:
    def test_routing_disabled_no_seeding(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config(enabled=False)

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)
        assert session_key not in runner._session_model_overrides

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == DEFAULT_MODEL

    def test_gateway_not_in_apply_to_no_seeding(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config(apply_to=("cron", "delegate"))

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)
        assert session_key not in runner._session_model_overrides

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == DEFAULT_MODEL

    def test_no_routing_config_no_seeding(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = {"model": {"default": DEFAULT_MODEL}}

        with patch("agent.model_router.route_model") as spy:
            _seed(runner, session_key, source, cfg)
        assert spy.call_count == 0
        assert session_key not in runner._session_model_overrides


class TestFailOpen:
    def test_broken_router_import_fails_open(self, store_factory, monkeypatch):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        import sys

        # A poisoned module entry makes ``from agent.model_router import …``
        # raise ImportError at the call site.
        monkeypatch.setitem(sys.modules, "agent.model_router", None)

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)
        assert session_key not in runner._session_model_overrides

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == DEFAULT_MODEL

    def test_raising_router_fails_open(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config()

        with patch(
            "agent.model_router.route_model",
            side_effect=RuntimeError("router exploded"),
        ), patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ):
            _seed(runner, session_key, source, cfg)
        assert session_key not in runner._session_model_overrides

        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == DEFAULT_MODEL


class TestRoutedProviderCredentials:
    def test_routed_provider_resolves_own_credentials(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config(
            light_tier={"model": LIGHT_MODEL, "provider": "openrouter"}
        )

        routed_runtime = {
            "api_key": "sk-routed",
            "base_url": "https://openrouter.example/v1",
            "provider": "openrouter",
            "api_mode": None,
            "command": None,
            "args": [],
            "credential_pool": None,
        }
        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ), patch(
            "gateway.run._resolve_runtime_agent_kwargs_for_provider",
            return_value=routed_runtime,
        ):
            _seed(runner, session_key, source, cfg)

        override = runner._session_model_overrides[session_key]
        assert override["model"] == LIGHT_MODEL
        assert override["provider"] == "openrouter"
        # Credentials come from the routed provider's own resolution — never
        # the default provider's key.
        assert override["api_key"] == "sk-routed"

        model, runtime_kwargs = _resolve(runner, source, session_key, cfg)
        assert model == LIGHT_MODEL
        assert runtime_kwargs["provider"] == "openrouter"
        assert runtime_kwargs["api_key"] == "sk-routed"

    def test_routed_provider_credential_failure_skips_route(self, store_factory):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config(
            light_tier={"model": LIGHT_MODEL, "provider": "openrouter"}
        )

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ), patch(
            "gateway.run._resolve_runtime_agent_kwargs_for_provider",
            side_effect=RuntimeError("no credentials"),
        ):
            _seed(runner, session_key, source, cfg)

        assert session_key not in runner._session_model_overrides
        model, _ = _resolve(runner, source, session_key, cfg)
        assert model == DEFAULT_MODEL

    def test_api_key_never_persisted_for_routed_override(self, store_factory, tmp_path):
        store = store_factory()
        source = _make_source()
        session_key = store.get_or_create_session(source).session_key
        runner = _make_runner(store)
        cfg = make_user_config(
            light_tier={"model": LIGHT_MODEL, "provider": "openrouter"}
        )

        routed_runtime = {
            "api_key": "sk-routed-SECRET",
            "base_url": None,
            "provider": "openrouter",
            "api_mode": None,
            "command": None,
            "args": [],
            "credential_pool": None,
        }
        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value=_default_runtime_kwargs(),
        ), patch(
            "gateway.run._resolve_runtime_agent_kwargs_for_provider",
            return_value=routed_runtime,
        ):
            _seed(runner, session_key, source, cfg)

        raw = (tmp_path / "sessions.json").read_text(encoding="utf-8")
        assert "sk-routed-SECRET" not in raw
        assert "api_key" not in raw
