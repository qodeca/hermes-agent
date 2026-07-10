"""Cron session output-token budget resolution.

Cron sessions get a LOWER default output-token budget than interactive
sessions: a runaway cron job once generated 261 KB of looping
self-deliberation for a two-word answer, and nothing bounded it. The
scheduler resolves ``cron.session_output_token_budget`` (default 200000)
for a job's agent — but only when the user has NOT set the agent-level
``agent.session_output_token_budget`` key, which always wins (including
an explicit 0 = unlimited).

Follows the ``_resolve_cron_max_runtime`` testing convention
(tests/cron/test_cron_wall_clock_cap.py): the resolver is a module-level
helper unit-tested directly, plus a temp-HERMES_HOME config.yaml loaded
exactly the way the scheduler loads it.
"""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _resolve_cron_session_output_budget  # noqa: E402


class TestResolution:
    def test_default_when_nothing_configured(self):
        assert _resolve_cron_session_output_budget({}) == 200000
        assert _resolve_cron_session_output_budget({"cron": {}}) == 200000
        assert _resolve_cron_session_output_budget(None) == 200000

    def test_cron_config_overrides_default(self):
        cfg = {"cron": {"session_output_token_budget": 50000}}
        assert _resolve_cron_session_output_budget(cfg) == 50000

    def test_cron_zero_means_unlimited(self):
        cfg = {"cron": {"session_output_token_budget": 0}}
        assert _resolve_cron_session_output_budget(cfg) == 0

    def test_agent_level_key_wins(self):
        """When the user set agent.session_output_token_budget, the
        scheduler must not override it — the resolver returns None and
        the agent keeps the value agent-init read from config."""
        cfg = {"agent": {"session_output_token_budget": 500000}}
        assert _resolve_cron_session_output_budget(cfg) is None

    def test_agent_level_explicit_zero_wins(self):
        """An explicit agent-level 0 (= unlimited) is a deliberate user
        choice and must be honored for cron sessions too."""
        cfg = {"agent": {"session_output_token_budget": 0},
               "cron": {"session_output_token_budget": 50000}}
        assert _resolve_cron_session_output_budget(cfg) is None

    def test_invalid_cron_value_falls_back_to_default(self):
        cfg = {"cron": {"session_output_token_budget": "lots"}}
        assert _resolve_cron_session_output_budget(cfg) == 200000

    def test_negative_cron_value_clamps_to_unlimited(self):
        cfg = {"cron": {"session_output_token_budget": -5}}
        assert _resolve_cron_session_output_budget(cfg) == 0


def test_resolution_from_hermes_home_config(tmp_path, monkeypatch):
    """Integration: a config.yaml in a temp HERMES_HOME, loaded the same
    way the scheduler loads it (yaml.safe_load of <home>/config.yaml),
    resolves to the cron budget when the agent-level key is unset."""
    import yaml

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "cron:\n  session_output_token_budget: 77777\n", encoding="utf-8"
    )

    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_session_output_budget(cfg) == 77777

    # And with the agent-level key present, cron defers to it.
    (home / "config.yaml").write_text(
        "agent:\n  session_output_token_budget: 300000\n"
        "cron:\n  session_output_token_budget: 77777\n",
        encoding="utf-8",
    )
    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_session_output_budget(cfg) is None
