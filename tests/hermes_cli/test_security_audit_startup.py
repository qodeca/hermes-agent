"""Tests for the startup security posture audit (hermes_cli.security_audit_startup)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import hermes_cli.security_audit_startup as audit


@pytest.fixture(autouse=True)
def _reset_audit_sentinel():
    audit._AUDIT_RAN = False
    yield
    audit._AUDIT_RAN = False


# ── root check ────────────────────────────────────────────────────────────


def test_root_check_flags_uid_zero(monkeypatch):
    monkeypatch.setattr(audit, "_is_root", lambda: True)
    finding = audit._running_as_root()
    assert finding is not None
    severity, msg = finding
    assert severity == "info"
    assert "ROOT" in msg


def test_root_check_silent_for_non_root(monkeypatch):
    monkeypatch.setattr(audit, "_is_root", lambda: False)
    assert audit._running_as_root() is None


# ── SSH password-auth check ─────────────────────────────────────────────────


def test_ssh_password_auth_enabled_explicit_yes(monkeypatch):
    monkeypatch.setattr(
        audit, "_iter_sshd_config_lines",
        lambda: ["PasswordAuthentication yes", "PermitRootLogin no"],
    )
    finding = audit._ssh_password_auth_enabled()
    assert finding is not None
    severity, msg = finding
    assert severity == "high"
    assert "password authentication is enabled" in msg.lower()


def test_ssh_password_auth_disabled(monkeypatch):
    monkeypatch.setattr(
        audit, "_iter_sshd_config_lines",
        lambda: ["PasswordAuthentication no"],
    )
    assert audit._ssh_password_auth_enabled() is None


def test_ssh_password_auth_default_is_yes(monkeypatch):
    """No explicit directive → sshd default is 'yes' → warn (with qualifier)."""
    monkeypatch.setattr(
        audit, "_iter_sshd_config_lines",
        lambda: ["PermitRootLogin prohibit-password"],
    )
    finding = audit._ssh_password_auth_enabled()
    assert finding is not None
    severity, msg = finding
    assert severity == "high"
    assert "default" in msg.lower()


def test_ssh_check_silent_when_no_config(monkeypatch):
    """No sshd config readable (e.g. Windows / SSH not installed) → no finding."""
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: [])
    assert audit._ssh_password_auth_enabled() is None


def test_ssh_last_directive_wins(monkeypatch):
    monkeypatch.setattr(
        audit, "_iter_sshd_config_lines",
        lambda: ["PasswordAuthentication yes", "PasswordAuthentication no"],
    )
    assert audit._ssh_password_auth_enabled() is None


# ── container / volume-mount check ──────────────────────────────────────────


def test_container_no_mount_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_in_container", lambda: True)
    monkeypatch.setattr(audit, "_path_is_mounted", lambda p: False)
    finding = audit._container_no_volume_mount(tmp_path / ".hermes")
    assert finding is not None
    severity, msg = finding
    assert severity == "info"
    assert "persistent volume" in msg


def test_container_with_mount_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_in_container", lambda: True)
    monkeypatch.setattr(audit, "_path_is_mounted", lambda p: True)
    assert audit._container_no_volume_mount(tmp_path / ".hermes") is None


def test_not_in_container_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    assert audit._container_no_volume_mount(tmp_path / ".hermes") is None


# ── network listener without auth ──────────────────────────────────────────


def test_api_server_network_no_key_flags(monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    cfg = {"platforms": {"api_server": {"enabled": True, "extra": {"host": "0.0.0.0", "key": ""}}}}
    findings = audit._network_listener_without_auth(cfg)
    assert any(severity == "high" and "NO API_SERVER_KEY" in msg for severity, msg in findings)


def test_api_server_loopback_silent(monkeypatch):
    cfg = {"platforms": {"api_server": {"enabled": True, "extra": {"host": "127.0.0.1", "key": ""}}}}
    assert audit._network_listener_without_auth(cfg) == []


def test_api_server_with_key_silent(monkeypatch):
    cfg = {"platforms": {"api_server": {"enabled": True, "extra": {"host": "0.0.0.0", "key": "a-strong-key-1234567890"}}}}
    assert audit._network_listener_without_auth(cfg) == []


# ── orchestration + logging ─────────────────────────────────────────────────


def test_run_security_audit_aggregates(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_is_root", lambda: True)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication yes"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    findings = audit.run_security_audit(hermes_home=tmp_path, config={})
    assert len(findings) == 2  # root + ssh
    severities = {severity for severity, _msg in findings}
    assert severities == {"info", "high"}


def test_run_security_audit_clean_posture(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "_is_root", lambda: False)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication no"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    assert audit.run_security_audit(hermes_home=tmp_path, config={}) == []


def test_log_startup_security_warnings_emits_and_is_idempotent(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setattr(audit, "_is_root", lambda: True)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: [])
    monkeypatch.setattr(audit, "_in_container", lambda: False)

    with caplog.at_level(logging.WARNING, logger="hermes.security_audit"):
        first = audit.log_startup_security_warnings(hermes_home=tmp_path, config={})
    assert len(first) == 1
    assert any("ROOT" in r.message for r in caplog.records)

    # Second call is a no-op (idempotent within a process) unless forced.
    second = audit.log_startup_security_warnings(hermes_home=tmp_path, config={})
    assert second == []
    forced = audit.log_startup_security_warnings(hermes_home=tmp_path, config={}, force=True)
    assert len(forced) == 1


def test_log_lines_unchanged_shape_for_severity_tagged_findings(monkeypatch, tmp_path, caplog):
    """Findings are now ``(severity, message)`` tuples, but the emitted log
    lines must stay byte-identical in shape to the pre-tuple format — only
    the message text is printed, no severity tag leaks into the log line."""
    import logging

    import hermes_cli.operator_alerts as operator_alerts

    monkeypatch.setattr(audit, "_is_root", lambda: True)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication yes"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    monkeypatch.setattr(operator_alerts, "send_operator_alert", lambda *a, **kw: True)

    with caplog.at_level(logging.WARNING, logger="hermes.security_audit"):
        findings = audit.log_startup_security_warnings(hermes_home=tmp_path, config={}, force=True)

    assert len(findings) == 2
    messages = [r.message for r in caplog.records if r.name == "hermes.security_audit"]
    assert messages[0] == "Security posture audit found 2 issue(s) — review your deployment:"
    assert messages[1] == f"  [security 1/2] {findings[0][1]}"
    assert messages[2] == f"  [security 2/2] {findings[1][1]}"


def test_audit_never_raises_on_broken_check(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(audit, "_is_root", _boom)
    # Must not propagate — the broken check is swallowed, others still run.
    findings = audit.run_security_audit(hermes_home=tmp_path, config={})
    assert isinstance(findings, list)


# ── severity tagging ─────────────────────────────────────────────────────


def test_severity_tags_pinned_per_check(monkeypatch, tmp_path):
    """Pin which checks are high vs info — a silent change here changes
    what pages an operator overnight vs. what only shows up in logs."""
    monkeypatch.setattr(audit, "_is_root", lambda: True)
    finding = audit._running_as_root()
    assert finding is not None and finding[0] == "info"

    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication yes"])
    finding = audit._ssh_password_auth_enabled()
    assert finding is not None and finding[0] == "high"

    monkeypatch.setattr(audit, "_in_container", lambda: True)
    monkeypatch.setattr(audit, "_path_is_mounted", lambda p: False)
    finding = audit._container_no_volume_mount(tmp_path)
    assert finding is not None and finding[0] == "info"

    cfg = {"platforms": {"api_server": {"enabled": True, "extra": {"host": "0.0.0.0", "key": ""}}}}
    findings = audit._network_listener_without_auth(cfg)
    assert findings and all(severity == "high" for severity, _msg in findings)


# ── high-severity alert routing ─────────────────────────────────────────────


def test_high_severity_findings_trigger_one_combined_alert(monkeypatch, tmp_path):
    """N high findings must collapse into exactly one alert call (not one
    per finding, and not silently dropped by the title-based rate limit) —
    one combined message listing every high finding."""
    import hermes_cli.operator_alerts as operator_alerts

    monkeypatch.setattr(audit, "_is_root", lambda: False)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication yes"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    monkeypatch.setattr(
        audit, "_network_listener_without_auth",
        lambda config: [("high", "API server is network-accessible with NO API_SERVER_KEY.")],
    )

    calls = []
    monkeypatch.setattr(
        operator_alerts, "send_operator_alert",
        lambda title, body="", **kw: calls.append((title, body, kw.get("severity"))) or True,
    )

    audit.log_startup_security_warnings(hermes_home=tmp_path, config={}, force=True)

    assert len(calls) == 1
    title, body, severity = calls[0]
    assert title == "security posture"
    assert severity == "high"
    assert "SSH password authentication is ENABLED" in body
    assert "API_SERVER_KEY" in body


def test_info_only_findings_do_not_trigger_alert(monkeypatch, tmp_path):
    import hermes_cli.operator_alerts as operator_alerts

    monkeypatch.setattr(audit, "_is_root", lambda: True)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication no"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)

    calls = []
    monkeypatch.setattr(
        operator_alerts, "send_operator_alert",
        lambda *a, **kw: calls.append((a, kw)) or True,
    )

    findings = audit.log_startup_security_warnings(hermes_home=tmp_path, config={}, force=True)
    assert len(findings) == 1
    assert findings[0][0] == "info"
    assert calls == []


def test_audit_completes_when_alerts_module_absent(monkeypatch, tmp_path, caplog):
    """hermes_cli.operator_alerts unavailable — the audit module must not
    hard-depend on it: findings still compute, the normal security-posture
    warning still logs, and nothing raises."""
    import logging
    import sys

    monkeypatch.setattr(audit, "_is_root", lambda: False)
    monkeypatch.setattr(audit, "_iter_sshd_config_lines", lambda: ["PasswordAuthentication yes"])
    monkeypatch.setattr(audit, "_in_container", lambda: False)
    monkeypatch.setitem(sys.modules, "hermes_cli.operator_alerts", None)

    with caplog.at_level(logging.WARNING, logger="hermes.security_audit"):
        findings = audit.log_startup_security_warnings(hermes_home=tmp_path, config={}, force=True)

    assert len(findings) == 1
    assert findings[0][0] == "high"
    assert any("Security posture audit found" in r.message for r in caplog.records)
