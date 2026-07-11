"""Cron per-job iteration cap resolution (T28).

Background: cron's historical ``max_iterations`` default of 90 gives a
confused/looping model a very long unattended rope. The wall-clock cap
(``_resolve_cron_max_runtime``, T4) and the session output-token budget
(``_resolve_cron_session_output_budget``, T23) are the real brakes on a
runaway job; this cap is the third rail, not the primary limit — which is
why the new default is 40 (enough tool-call depth for a legitimate
research job) rather than something much tighter like 15.

Resolution order: per-job override (future — no per-job key exists yet,
this is a seam for one) > ``cron.max_iterations`` config >
global ``agent.max_turns`` (or the legacy root-level ``max_turns``,
since the raw config this resolver reads is never passed through the
``agent.max_turns`` normalization pipeline) > 40 default.

Follows the ``_resolve_cron_max_runtime`` / ``_resolve_cron_session_output_budget``
testing convention: the resolver is a module-level helper unit-tested
directly, plus a temp-HERMES_HOME config.yaml loaded exactly the way the
scheduler loads it, plus a full ``run_job`` integration test (mirroring
tests/cron/test_cron_provider_pin.py) asserting the resolved value
actually reaches the AIAgent constructor.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _resolve_cron_max_iterations, run_job  # noqa: E402


class TestResolution:
    def test_default_when_nothing_configured(self):
        assert _resolve_cron_max_iterations({}) == 40
        assert _resolve_cron_max_iterations({"cron": {}}) == 40
        assert _resolve_cron_max_iterations(None) == 40

    def test_cron_config_overrides_default(self):
        cfg = {"cron": {"max_iterations": 25}}
        assert _resolve_cron_max_iterations(cfg) == 25

    def test_agent_max_turns_wins_over_default_when_no_cron_key(self):
        """(c) global agent.max_turns set, no cron key -> agent.max_turns
        wins over the 40 default."""
        cfg = {"agent": {"max_turns": 120}}
        assert _resolve_cron_max_iterations(cfg) == 120

    def test_cron_config_wins_over_agent_max_turns(self):
        """(c) ... but is overridden by the cron key when present — the
        cron-level key takes priority over the interactive-session
        agent-level default, unlike the output-budget/degeneration
        resolvers where the agent-level key always wins."""
        cfg = {"agent": {"max_turns": 120}, "cron": {"max_iterations": 25}}
        assert _resolve_cron_max_iterations(cfg) == 25

    def test_legacy_root_max_turns_used_as_fallback(self):
        """The raw config this resolver reads (yaml.safe_load of
        config.yaml) never passes through the agent.max_turns
        normalization pipeline, so the legacy root-level ``max_turns``
        key must still be honored as a fallback."""
        cfg = {"max_turns": 75}
        assert _resolve_cron_max_iterations(cfg) == 75

    def test_agent_max_turns_beats_legacy_root_max_turns(self):
        cfg = {"agent": {"max_turns": 33}, "max_turns": 75}
        assert _resolve_cron_max_iterations(cfg) == 33

    def test_invalid_cron_value_falls_back_to_default(self):
        cfg = {"cron": {"max_iterations": "lots"}}
        assert _resolve_cron_max_iterations(cfg) == 40

    def test_zero_cron_value_falls_back_not_a_zero_cap(self):
        """A configured 0 must NOT produce a zero-turn job (0 < 0 is
        False on the loop's first check → the agent makes ZERO API
        calls and silently does nothing). Unlike max_runtime_seconds /
        session_output_token_budget, this key has no 'unlimited' mode:
        a non-positive value is treated as unset and falls through to
        the next tier (here: the 40 default)."""
        cfg = {"cron": {"max_iterations": 0}}
        assert _resolve_cron_max_iterations(cfg) == 40

    def test_negative_cron_value_falls_back(self):
        cfg = {"cron": {"max_iterations": -5}}
        assert _resolve_cron_max_iterations(cfg) == 40

    def test_zero_cron_value_falls_back_to_agent_max_turns(self):
        """Non-positive cron value is unset → the next tier (agent.max_turns)
        governs, not the 40 default."""
        cfg = {"agent": {"max_turns": 120}, "cron": {"max_iterations": 0}}
        assert _resolve_cron_max_iterations(cfg) == 120

    def test_positive_cron_value_still_wins_after_the_guard(self):
        """The non-positive guard must not regress the happy path: a
        positive cron key still takes priority over agent.max_turns."""
        cfg = {"agent": {"max_turns": 120}, "cron": {"max_iterations": 25}}
        assert _resolve_cron_max_iterations(cfg) == 25

    def test_non_dict_cfg_uses_default(self):
        assert _resolve_cron_max_iterations({"cron": "not-a-dict"}) == 40


def test_resolution_from_hermes_home_config(tmp_path, monkeypatch):
    """Integration: a config.yaml in a temp HERMES_HOME, loaded the same
    way the scheduler loads it (yaml.safe_load of <home>/config.yaml),
    resolves to the cron cap when configured, else the agent.max_turns
    fallback, else 40."""
    import yaml

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    (home / "config.yaml").write_text(
        "cron:\n  max_iterations: 25\n", encoding="utf-8"
    )
    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_max_iterations(cfg) == 25

    (home / "config.yaml").write_text(
        "agent:\n  max_turns: 120\n",
        encoding="utf-8",
    )
    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_max_iterations(cfg) == 120

    (home / "config.yaml").write_text(
        "agent:\n  max_turns: 120\ncron:\n  max_iterations: 25\n",
        encoding="utf-8",
    )
    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_max_iterations(cfg) == 25


def _base_job(**overrides):
    job = {
        "id": "max-iter-test",
        "name": "max iter test",
        "prompt": "hello",
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "base_url": None,
    }
    job.update(overrides)
    return job


def _run_job_with_config(job, config_yaml_text, tmp_path):
    """Drive the real run_job() against a temp HERMES_HOME config.yaml,
    with AIAgent mocked out, and return the max_iterations kwarg it was
    constructed with (or None if never constructed)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    if config_yaml_text is not None:
        (home / "config.yaml").write_text(config_yaml_text, encoding="utf-8")

    fake_db = MagicMock()
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
            return None
        return mock_agent_cls.call_args.kwargs.get("max_iterations")


class TestReachesAgent:
    """(d) Integration: the resolved max_iterations actually reaches the
    agent's iteration cap for a cron session (the AIAgent constructor
    kwarg run_job() passes to run_agent.AIAgent, consumed by
    run_conversation() as the per-turn loop bound)."""

    def test_default_reaches_agent(self, tmp_path):
        job = _base_job(model="test-model", provider="openrouter")
        assert _run_job_with_config(job, None, tmp_path) == 40

    def test_cron_config_reaches_agent(self, tmp_path):
        job = _base_job(model="test-model", provider="openrouter")
        assert _run_job_with_config(
            job, "cron:\n  max_iterations: 25\n", tmp_path
        ) == 25

    def test_agent_max_turns_reaches_agent_when_no_cron_key(self, tmp_path):
        job = _base_job(model="test-model", provider="openrouter")
        assert _run_job_with_config(
            job, "agent:\n  max_turns: 120\n", tmp_path
        ) == 120

    def test_cron_config_wins_over_agent_max_turns_at_agent(self, tmp_path):
        job = _base_job(model="test-model", provider="openrouter")
        assert _run_job_with_config(
            job,
            "agent:\n  max_turns: 120\ncron:\n  max_iterations: 25\n",
            tmp_path,
        ) == 25
