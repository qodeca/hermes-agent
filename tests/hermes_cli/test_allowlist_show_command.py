"""Tests for the CLI /allowlist show command.

The owner himself was denied after a restart with no way to answer "why was
I denied?" without reading code. ``/allowlist show`` surfaces the effective
authorization sources (env allowlists, pairing grants, allow-all flags) as
plain text.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cli import HermesCLI
from hermes_cli.commands import resolve_command


def _make_cli():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.config = {}
    cli_obj.console = MagicMock()
    return cli_obj


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


def test_allowlist_command_is_registered():
    cmd = resolve_command("allowlist")
    assert cmd is not None
    assert cmd.category == "Info"
    assert cmd.cli_only is False
    assert cmd.gateway_only is False


def test_process_command_allowlist_show_dispatches(capsys):
    cli_obj = _make_cli()

    assert cli_obj.process_command("/allowlist show") is True

    out = capsys.readouterr().out
    assert "effective authorization sources" in out


def test_allowlist_show_reports_nothing_configured(capsys):
    cli_obj = _make_cli()

    cli_obj._handle_allowlist_command("/allowlist show")

    out = capsys.readouterr().out
    assert "GATEWAY_ALLOW_ALL_USERS: (not set)" in out
    assert "GATEWAY_ALLOWED_USERS: (not set)" in out
    assert "(none set)" in out
    assert "(none)" in out


def test_allowlist_show_reports_configured_env_allowlists(capsys, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "12345,67890")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")

    cli_obj = _make_cli()
    cli_obj._handle_allowlist_command("/allowlist show")

    out = capsys.readouterr().out
    assert "TELEGRAM_ALLOWED_USERS: 12345,67890" in out
    assert "DISCORD_ALLOW_ALL_USERS: true" in out


def test_allowlist_show_lists_paired_users(capsys, monkeypatch):
    from gateway.pairing import PairingStore

    store = PairingStore()
    store._approve_user("telegram", "owner-id", "Marcin")

    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default"
    )

    cli_obj = _make_cli()
    cli_obj._handle_allowlist_command("/allowlist show")

    out = capsys.readouterr().out
    assert "telegram: owner-id (Marcin)" in out


def test_allowlist_bare_prints_usage(capsys):
    cli_obj = _make_cli()

    cli_obj._handle_allowlist_command("/allowlist")

    out = capsys.readouterr().out
    assert "Usage: /allowlist show" in out
