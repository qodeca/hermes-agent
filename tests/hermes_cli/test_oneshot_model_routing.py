"""Task-complexity routing for oneshot runs (hermes_cli/oneshot.py, T26).

Precedence (highest first): explicit ``--model`` / HERMES_INFERENCE_MODEL >
router > config default. The router only fills the global-default gap; it is
config-gated (``routing.enabled``, off by default) and fail-open — a broken
router must never break a oneshot run.
"""

from unittest.mock import MagicMock, patch

import pytest

LIGHT_MODEL = "vendor/light-model"
DEFAULT_MODEL = "vendor/default-model"

# < light_max_chars, no heavy keywords, no URLs, no numbered steps → light tier
SHORT_TASK = "Morning! Just saying hello."


def make_config(*, enabled=True, apply_to=("oneshot",)):
    return {
        "model": {"default": DEFAULT_MODEL},
        "routing": {
            "enabled": enabled,
            "apply_to": list(apply_to),
            "classifier": "heuristic",
            "default_tier": "standard",
            "tiers": {
                "light": {"model": LIGHT_MODEL},
                "standard": {"model": "vendor/standard-model"},
                "heavy": {"model": "vendor/heavy-model"},
            },
        },
    }


def _fake_runtime():
    return {
        "api_key": "sk-test",
        "base_url": None,
        "provider": "openai",
        "api_mode": None,
        "credential_pool": None,
    }


def _run(prompt=SHORT_TASK, *, cfg, model=None, provider=None, monkeypatch=None):
    """Drive hermes_cli.oneshot._run_agent with the agent + providers faked.

    Returns (final AIAgent model kwarg, resolve_runtime_provider call kwargs).
    """
    from hermes_cli import oneshot

    fake_agent = MagicMock()
    fake_agent.run_conversation.return_value = {"final_response": "ok"}
    fake_agent_cls = MagicMock(return_value=fake_agent)
    resolve = MagicMock(return_value=_fake_runtime())

    with patch("hermes_cli.config.load_config", return_value=cfg), patch(
        "hermes_cli.runtime_provider.resolve_runtime_provider", resolve
    ), patch("run_agent.AIAgent", fake_agent_cls), patch.object(
        oneshot, "_create_session_db_for_oneshot", return_value=None
    ):
        response, result = oneshot._run_agent(
            prompt, model=model, provider=provider, use_config_toolsets=False
        )

    assert response == "ok"
    agent_kwargs = fake_agent_cls.call_args.kwargs
    return agent_kwargs, resolve


class TestOneshotRouting:
    def test_routed_when_enabled_and_no_override(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        agent_kwargs, resolve = _run(cfg=make_config())
        assert agent_kwargs["model"] == LIGHT_MODEL
        # The routed model is what provider resolution targets.
        assert resolve.call_args.kwargs["target_model"] == LIGHT_MODEL

    def test_explicit_model_arg_untouched(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        with patch("agent.model_router.route_model") as spy:
            agent_kwargs, _ = _run(
                cfg=make_config(), model="vendor/explicit-model"
            )
        assert spy.call_count == 0
        assert agent_kwargs["model"] == "vendor/explicit-model"

    def test_env_model_untouched(self, monkeypatch):
        monkeypatch.setenv("HERMES_INFERENCE_MODEL", "vendor/env-model")
        with patch("agent.model_router.route_model") as spy:
            agent_kwargs, _ = _run(cfg=make_config())
        assert spy.call_count == 0
        assert agent_kwargs["model"] == "vendor/env-model"

    def test_routing_disabled_uses_config_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        agent_kwargs, _ = _run(cfg=make_config(enabled=False))
        assert agent_kwargs["model"] == DEFAULT_MODEL

    def test_oneshot_not_in_apply_to_uses_config_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        agent_kwargs, _ = _run(cfg=make_config(apply_to=("gateway",)))
        assert agent_kwargs["model"] == DEFAULT_MODEL

    def test_broken_router_fails_open(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        with patch(
            "agent.model_router.route_model",
            side_effect=RuntimeError("router exploded"),
        ):
            agent_kwargs, _ = _run(cfg=make_config())
        assert agent_kwargs["model"] == DEFAULT_MODEL

    def test_routed_backend_resolution_failure_falls_back(self, monkeypatch):
        """A routed tier naming an unresolvable backend keeps the default."""
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        from hermes_cli import oneshot

        cfg = make_config()
        cfg["routing"]["tiers"]["light"] = {
            "model": LIGHT_MODEL,
            "provider": "openrouter",
        }

        fake_agent = MagicMock()
        fake_agent.run_conversation.return_value = {"final_response": "ok"}
        fake_agent_cls = MagicMock(return_value=fake_agent)

        def resolve(requested=None, target_model=None, explicit_base_url=None):
            if requested == "openrouter":
                raise RuntimeError("no credentials for routed provider")
            return _fake_runtime()

        with patch("hermes_cli.config.load_config", return_value=cfg), patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=resolve,
        ), patch("run_agent.AIAgent", fake_agent_cls), patch.object(
            oneshot, "_create_session_db_for_oneshot", return_value=None
        ):
            response, _ = oneshot._run_agent(
                SHORT_TASK, use_config_toolsets=False
            )

        assert response == "ok"
        assert fake_agent_cls.call_args.kwargs["model"] == DEFAULT_MODEL
