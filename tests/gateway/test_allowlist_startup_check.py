"""Tests for the startup allowlist warning check in gateway/run.py."""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _would_warn():
    """Replicate the startup allowlist warning logic. Returns True if warning fires."""
    _any_allowlist = any(
        os.getenv(v)
        for v in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS",
                   "WHATSAPP_ALLOWED_USERS", "SLACK_ALLOWED_USERS",
                   "SIGNAL_ALLOWED_USERS", "SIGNAL_GROUP_ALLOWED_USERS",
                   "EMAIL_ALLOWED_USERS",
                   "SMS_ALLOWED_USERS", "MATTERMOST_ALLOWED_USERS",
                   "MATRIX_ALLOWED_USERS", "DINGTALK_ALLOWED_USERS", "FEISHU_ALLOWED_USERS", "WECOM_ALLOWED_USERS",
                   "GATEWAY_ALLOWED_USERS")
    )
    _allow_all = os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"} or any(
        os.getenv(v, "").lower() in {"true", "1", "yes"}
        for v in ("TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
                   "WHATSAPP_ALLOW_ALL_USERS", "SLACK_ALLOW_ALL_USERS",
                   "SIGNAL_ALLOW_ALL_USERS", "EMAIL_ALLOW_ALL_USERS",
                   "SMS_ALLOW_ALL_USERS", "MATTERMOST_ALLOW_ALL_USERS",
                   "MATRIX_ALLOW_ALL_USERS", "DINGTALK_ALLOW_ALL_USERS", "FEISHU_ALLOW_ALL_USERS", "WECOM_ALLOW_ALL_USERS")
    )
    return not _any_allowlist and not _allow_all


class TestAllowlistStartupCheck:

    def test_no_config_emits_warning(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _would_warn() is True

    def test_signal_group_allowed_users_suppresses_warning(self):
        with patch.dict(os.environ, {"SIGNAL_GROUP_ALLOWED_USERS": "user1"}, clear=True):
            assert _would_warn() is False

    def test_telegram_allow_all_users_suppresses_warning(self):
        with patch.dict(os.environ, {"TELEGRAM_ALLOW_ALL_USERS": "true"}, clear=True):
            assert _would_warn() is False

    def test_gateway_allow_all_users_suppresses_warning(self):
        with patch.dict(os.environ, {"GATEWAY_ALLOW_ALL_USERS": "yes"}, clear=True):
            assert _would_warn() is False


# --------------------------------------------------------------------------
# Per-platform allow-all + loud startup alerting.
#
# These tests drive the REAL ``GatewayRunner.start()`` (not a reimplemented
# copy of the logic, unlike ``_would_warn()`` above) so they exercise the
# production deprecation-warning and operator-alert wiring end to end.
# Platforms are kept ``enabled=False`` throughout so ``start()`` takes the
# "cron-only mode" path and never attempts to construct a real adapter (see
# ``test_runner_allows_cron_only_mode_when_no_platforms_are_enabled`` in
# test_runner_startup_failures.py for the same pattern).
# --------------------------------------------------------------------------

_ALLOW_ALL_ENV_VARS = (
    "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS",
    "TELEGRAM_ALLOW_ALL_USERS", "TELEGRAM_ALLOWED_USERS",
    "DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOWED_USERS",
)


@pytest.fixture(autouse=True)
def _isolate_allow_all_env(monkeypatch):
    for var in _ALLOW_ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _two_platform_config(tmp_path):
    return GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=False, token="***"),
            Platform.DISCORD: PlatformConfig(enabled=False, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )


class TestPerPlatformAllowAllStartupAlert:

    @pytest.mark.asyncio
    async def test_global_allow_all_warns_deprecated_and_alerts_all_platforms(
        self, monkeypatch, tmp_path, caplog,
    ):
        """(a) Global flag set -> deprecation WARNING + one alert naming all platforms."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
        alert_mock = MagicMock(return_value=True)
        monkeypatch.setattr(
            "hermes_cli.operator_alerts.send_operator_alert", alert_mock,
        )
        runner = GatewayRunner(_two_platform_config(tmp_path))

        with caplog.at_level(logging.WARNING, logger="gateway.run"):
            ok = await runner.start()

        assert ok is True
        assert any(
            "deprecated" in r.message.lower() and "GATEWAY_ALLOW_ALL_USERS" in r.message
            for r in caplog.records
        )
        alert_mock.assert_called_once()
        _title, body = alert_mock.call_args[0][:2]
        assert "telegram" in body.lower()
        assert "discord" in body.lower()

    @pytest.mark.asyncio
    async def test_telegram_only_allow_all_authorizes_telegram_not_discord(
        self, monkeypatch, tmp_path,
    ):
        """(b) TELEGRAM_ALLOW_ALL_USERS=true -> only telegram senders authorized,
        discord still denied, and the alert names telegram only."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
        alert_mock = MagicMock(return_value=True)
        monkeypatch.setattr(
            "hermes_cli.operator_alerts.send_operator_alert", alert_mock,
        )
        runner = GatewayRunner(_two_platform_config(tmp_path))

        ok = await runner.start()

        assert ok is True
        alert_mock.assert_called_once()
        _title, body = alert_mock.call_args[0][:2]
        assert "telegram" in body.lower()
        assert "discord" not in body.lower()

        telegram_source = SessionSource(
            platform=Platform.TELEGRAM, chat_id="1", chat_type="dm", user_id="u1",
        )
        discord_source = SessionSource(
            platform=Platform.DISCORD, chat_id="2", chat_type="dm", user_id="u2",
        )
        assert runner._is_user_authorized(telegram_source) is True
        assert runner._is_user_authorized(discord_source) is False

    @pytest.mark.asyncio
    async def test_no_allow_all_flags_sends_no_alert(self, monkeypatch, tmp_path):
        """(c) Regression: no allow-all flags configured -> no operator alert."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        alert_mock = MagicMock(return_value=True)
        monkeypatch.setattr(
            "hermes_cli.operator_alerts.send_operator_alert", alert_mock,
        )
        runner = GatewayRunner(_two_platform_config(tmp_path))

        ok = await runner.start()

        assert ok is True
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_delivery_failure_does_not_block_startup(
        self, monkeypatch, tmp_path,
    ):
        """(d) An exploding alert helper must not abort gateway startup."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("delivery exploded")

        monkeypatch.setattr(
            "hermes_cli.operator_alerts.send_operator_alert", _boom,
        )
        runner = GatewayRunner(_two_platform_config(tmp_path))

        ok = await runner.start()

        assert ok is True
