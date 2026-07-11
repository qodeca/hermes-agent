"""Cron degeneration-detection default resolution.

Cron sessions default degeneration detection ON — nobody is watching an
unattended job loop (a trivial cron job once emitted 261 KB of looping
self-deliberation before answering) — while interactive sessions default
it OFF (a human can see and interrupt a loop). The scheduler resolves
``cron.degeneration_detection`` (default true) for a job's agent, but only
when the user has NOT set the agent-level ``agent.degeneration_detection``
key in config.yaml, which always wins (including an explicit false).

Mirrors the ``_resolve_cron_session_output_budget`` testing convention
(tests/cron/test_cron_output_token_budget.py): the resolver is a
module-level helper unit-tested directly, plus a temp-HERMES_HOME
config.yaml loaded exactly the way the scheduler loads it.
"""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _resolve_cron_degeneration_detection  # noqa: E402


class TestResolution:
    def test_default_on_when_nothing_configured(self):
        assert _resolve_cron_degeneration_detection({}) is True
        assert _resolve_cron_degeneration_detection({"cron": {}}) is True
        assert _resolve_cron_degeneration_detection(None) is True

    def test_cron_config_can_disable(self):
        cfg = {"cron": {"degeneration_detection": False}}
        assert _resolve_cron_degeneration_detection(cfg) is False

    def test_cron_config_explicit_enable(self):
        cfg = {"cron": {"degeneration_detection": True}}
        assert _resolve_cron_degeneration_detection(cfg) is True

    def test_agent_level_key_wins(self):
        """When the user set agent.degeneration_detection, the scheduler
        must not override it — the resolver returns None and the agent
        keeps the value agent-init read from config."""
        cfg = {"agent": {"degeneration_detection": True}}
        assert _resolve_cron_degeneration_detection(cfg) is None

    def test_agent_level_explicit_false_wins(self):
        """An explicit agent-level false is a deliberate user choice and
        must be honored for cron sessions too."""
        cfg = {"agent": {"degeneration_detection": False},
               "cron": {"degeneration_detection": True}}
        assert _resolve_cron_degeneration_detection(cfg) is None


def test_resolution_from_hermes_home_config(tmp_path, monkeypatch):
    """Integration: a config.yaml in a temp HERMES_HOME, loaded the same
    way the scheduler loads it (yaml.safe_load of <home>/config.yaml)."""
    import yaml

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "cron:\n  degeneration_detection: false\n", encoding="utf-8"
    )

    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_degeneration_detection(cfg) is False

    # And with the agent-level key present, cron defers to it.
    (home / "config.yaml").write_text(
        "agent:\n  degeneration_detection: false\n"
        "cron:\n  degeneration_detection: true\n",
        encoding="utf-8",
    )
    with open(home / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    assert _resolve_cron_degeneration_detection(cfg) is None
