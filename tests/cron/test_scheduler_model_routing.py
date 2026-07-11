"""Cron task-complexity model routing (T25).

``run_job`` fills the GLOBAL-DEFAULT gap only: when the model precedence
(job.model > HERMES_MODEL > config.yaml default) resolved from the config
default, the config-gated router (``agent.model_router``, off by default)
may swap the model for that fire. An explicit job.model or a HERMES_MODEL
env override must always win untouched, routing disabled must be a strict
no-op, and a broken router must never take down cron (fail-open).

Follows the run_job -> AIAgent-constructor assertion pattern from
tests/cron/test_cron_max_iterations.py (T28): drive the real ``run_job``
against a temp HERMES_HOME with a real config.yaml, mock only AIAgent and
the runtime-provider resolution, and assert the ``model`` kwarg that
actually reaches the agent.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import run_job  # noqa: E402


ROUTED_CONFIG = """\
model:
  default: default-model
routing:
  enabled: true
  tiers:
    light:
      model: light-model
"""

DISABLED_CONFIG = """\
model:
  default: default-model
routing:
  enabled: false
  tiers:
    light:
      model: light-model
"""

# No global default: HERMES_MODEL is the only source, so the env override
# path is exercised without depending on the config-vs-env overwrite quirk.
ENV_ONLY_CONFIG = """\
routing:
  enabled: true
  tiers:
    light:
      model: light-model
"""


def _base_job(**overrides):
    job = {
        "id": "model-routing-test",
        "name": "model routing test",
        # Short prompt -> deterministic "light" heuristic verdict.
        "prompt": "hello",
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "base_url": None,
        # Pin per-job toolsets so the router's heavy-toolset heuristic
        # (browser/delegation attached -> heavy) cannot fire from whatever
        # the platform default toolset happens to be.
        "enabled_toolsets": ["file"],
    }
    job.update(overrides)
    return job


def _run_job_with_config(job, config_yaml_text, tmp_path, env=None):
    """Drive the real run_job() against a temp HERMES_HOME config.yaml,
    with AIAgent mocked out. Returns (model_kwarg, mock_agent) —
    model_kwarg is None if AIAgent was never constructed."""
    home = tmp_path / ".hermes"
    home.mkdir()
    if config_yaml_text is not None:
        (home / "config.yaml").write_text(config_yaml_text, encoding="utf-8")

    fake_db = MagicMock()
    with patch.dict(os.environ):
        os.environ.pop("HERMES_MODEL", None)
        os.environ["HERMES_HOME"] = str(home)
        for key, value in (env or {}).items():
            os.environ[key] = value
        with patch("cron.scheduler._hermes_home", home), \
             patch("cron.scheduler._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state.SessionDB", return_value=fake_db), \
             patch(
                 "hermes_cli.runtime_provider.resolve_runtime_provider",
                 return_value={
                     "api_key": "test-key",
                     "base_url": "https://example.invalid/v1",
                     "provider": "openrouter",
                     "api_mode": "chat_completions",
                 },
             ), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            run_job(job)

            if not mock_agent_cls.called:
                return None, mock_agent
            return mock_agent_cls.call_args.kwargs.get("model"), mock_agent


class TestCronModelRouting:
    def test_default_model_job_is_routed(self, tmp_path):
        """(a) No job.model, routing enabled, light tier configured ->
        the routed model reaches the agent."""
        model, _agent = _run_job_with_config(_base_job(), ROUTED_CONFIG, tmp_path)
        assert model == "light-model"

    def test_route_recorded_in_run_stats(self, tmp_path):
        """(a2) A routed fire records a {tier, model, reason} sub-field in
        the run-stats entry stashed on the agent — observability only,
        never the fail-closed model_snapshot drift guard."""
        _model, agent = _run_job_with_config(_base_job(), ROUTED_CONFIG, tmp_path)
        stats = getattr(agent, "_cron_run_stats", None)
        assert isinstance(stats, dict)
        route = stats.get("route")
        assert isinstance(route, dict)
        assert route["tier"] == "light"
        assert route["model"] == "light-model"
        assert route.get("reason")

    def test_explicit_job_model_wins_untouched(self, tmp_path):
        """(b) An explicit per-job model always wins over the router."""
        job = _base_job(model="pinned-model")
        model, agent = _run_job_with_config(job, ROUTED_CONFIG, tmp_path)
        assert model == "pinned-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_routing_disabled_is_a_noop(self, tmp_path):
        """(c) routing.enabled false -> the global default is untouched."""
        model, agent = _run_job_with_config(_base_job(), DISABLED_CONFIG, tmp_path)
        assert model == "default-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_hermes_model_env_wins_untouched(self, tmp_path):
        """(d) A HERMES_MODEL env override always wins over the router."""
        model, agent = _run_job_with_config(
            _base_job(), ENV_ONLY_CONFIG, tmp_path,
            env={"HERMES_MODEL": "env-model"},
        )
        assert model == "env-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_broken_router_fails_open_to_default(self, tmp_path):
        """(e) A router that raises must never take down cron: the job
        still runs on the global default model."""
        with patch(
            "agent.model_router.route_model",
            side_effect=RuntimeError("router exploded"),
        ):
            model, agent = _run_job_with_config(_base_job(), ROUTED_CONFIG, tmp_path)
        assert model == "default-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_pinned_provider_skips_routing(self, tmp_path):
        """A job that pins provider (but not model) is treated as an
        explicit backend choice: routing is skipped entirely so the pinned
        endpoint is never handed a tier model it may not serve. The job
        runs on its resolved default model, untouched."""
        job = _base_job(provider="openrouter")
        model, agent = _run_job_with_config(job, ROUTED_CONFIG, tmp_path)
        assert model == "default-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_pinned_base_url_skips_routing(self, tmp_path):
        """A pinned base_url is likewise an explicit backend choice that
        suppresses routing (e.g. a local LM Studio/ollama endpoint that
        serves only its loaded model). A base_url pin requires a provider
        (the credential-exfil guard blocks a bare base_url), so pin both."""
        job = _base_job(provider="custom", base_url="https://local.invalid/v1")
        model, agent = _run_job_with_config(job, ROUTED_CONFIG, tmp_path)
        assert model == "default-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})

    def test_missing_tier_model_keeps_default(self, tmp_path):
        """A routed tier with no model configured is a no-op decision:
        the global default is kept."""
        config = (
            "model:\n"
            "  default: default-model\n"
            "routing:\n"
            "  enabled: true\n"
            "  tiers: {}\n"
        )
        model, agent = _run_job_with_config(_base_job(), config, tmp_path)
        assert model == "default-model"
        assert "route" not in (getattr(agent, "_cron_run_stats", None) or {})
