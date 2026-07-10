"""Tests for the minimal operator-alert helper (hermes_cli/operator_alerts.py).

Incident: security warnings and job failures went only to log files nobody
watched overnight. This module is the alerting primitive later work routes
through — a thin, fire-and-forget wrapper over cron's own delivery-target
resolver (``cron/scheduler.py::_resolve_single_delivery_target``) and the
standalone send path (``tools/send_message_tool._send_to_platform``).

Covers:
  (a) configured + send patched -> sends once, returns True, message
      contains severity + title + body;
  (b) rate-limiting: second identical title within the 15-minute window is
      suppressed (returns False, nothing sent); a different title still
      sends; after the window elapses (patched clock) the same title sends
      again;
  (c) unconfigured (``alerts.deliver`` unset/empty) -> False, no raise,
      no delivery attempted;
  (d) delivery raising -> False, no raise, one warning logged;
  (e) integration/"lights-up" test: cron/scheduler.py::_send_reconcile_alert
      now routes through the real send_operator_alert (no more guarded
      ImportError fallback) and reaches the platform send.

Suppression contract: a rate-limited alert returns False, the same value as
"unconfigured" and "delivery failed" — this module intentionally does not
expose a three-way (sent / suppressed / failed) result. The one thing a
caller can rely on is that True means "an attempt was made and reported no
error"; anything else means "the alert did not go out this call, for some
reason logged already" (mirrors the pre-existing degrade-to-logger.warning
contract cron/scheduler.py::_send_reconcile_alert was built to lean on).
"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """Each test starts with a clean rate-limit table — it is module-level
    global state, so a previous test's title (or the same title reused
    across tests, e.g. the default "Title") must not bleed across tests."""
    from hermes_cli import operator_alerts

    operator_alerts._last_sent_at.clear()
    yield
    operator_alerts._last_sent_at.clear()


@pytest.fixture
def configured(monkeypatch):
    """Patch config so alerts.deliver resolves to a concrete telegram target."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"alerts": {"deliver": "telegram:123456"}},
    )


@pytest.fixture
def patched_send(monkeypatch):
    """Patch the gateway config + platform send boundary send_operator_alert
    dispatches through, mirroring the pattern tests/cron/test_scheduler.py
    already uses for `_deliver_result`."""
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    mock_cfg = MagicMock()
    mock_cfg.platforms = {Platform.TELEGRAM: pconfig}

    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: mock_cfg)
    send_mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", send_mock)
    return send_mock


class TestSendConfiguredSuccess:
    """(a) configured + send patched -> sends once, returns True, message
    contains severity + title + body."""

    def test_sends_once_and_returns_true(self, configured, patched_send):
        from hermes_cli.operator_alerts import send_operator_alert

        result = send_operator_alert("Disk space critical", "Only 2% free on /data", severity="critical")

        assert result is True
        patched_send.assert_called_once()
        _args, kwargs = patched_send.call_args
        sent_message = kwargs.get("message")
        if sent_message is None:
            # positional call shape: (platform, pconfig, chat_id, message, ...)
            sent_message = patched_send.call_args[0][3]
        assert "critical" in sent_message
        assert "Disk space critical" in sent_message
        assert "Only 2% free on /data" in sent_message

    def test_defaults_severity_to_warning_and_body_to_empty(self, configured, patched_send):
        """The sole existing caller (cron/scheduler.py::_send_reconcile_alert)
        calls send_operator_alert(message) with a single positional arg —
        body and severity must default so that call shape keeps working."""
        from hermes_cli.operator_alerts import send_operator_alert

        result = send_operator_alert("Job interrupted mid-run")

        assert result is True
        patched_send.assert_called_once()
        sent_message = patched_send.call_args.kwargs.get("message") or patched_send.call_args[0][3]
        assert "warning" in sent_message
        assert "Job interrupted mid-run" in sent_message


class TestRateLimiting:
    """(b) identical titles are rate-limited to once per 15 minutes."""

    def test_second_identical_title_within_window_is_suppressed(self, configured, patched_send):
        from hermes_cli.operator_alerts import send_operator_alert

        first = send_operator_alert("Repeated alert", "first")
        second = send_operator_alert("Repeated alert", "second")

        assert first is True
        assert second is False
        patched_send.assert_called_once()  # only the first attempt reached delivery

    def test_different_title_still_sends(self, configured, patched_send):
        from hermes_cli.operator_alerts import send_operator_alert

        first = send_operator_alert("Alert A")
        second = send_operator_alert("Alert B")

        assert first is True
        assert second is True
        assert patched_send.call_count == 2

    def test_sends_again_after_window_expires(self, configured, patched_send, monkeypatch):
        from hermes_cli import operator_alerts

        clock = {"t": 1000.0}
        monkeypatch.setattr(operator_alerts, "_monotonic", lambda: clock["t"])

        first = operator_alerts.send_operator_alert("Flapping check")
        assert first is True

        clock["t"] += 5 * 60  # still inside the 15-minute window
        still_suppressed = operator_alerts.send_operator_alert("Flapping check")
        assert still_suppressed is False

        clock["t"] += 11 * 60  # now past the 15-minute window (16 min elapsed)
        sends_again = operator_alerts.send_operator_alert("Flapping check")
        assert sends_again is True

        assert patched_send.call_count == 2


class TestUnconfigured:
    """(c) unconfigured -> False, no raise, nothing dispatched."""

    def test_empty_deliver_returns_false_without_dispatch(self, monkeypatch, patched_send):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"alerts": {"deliver": ""}})
        from hermes_cli.operator_alerts import send_operator_alert

        result = send_operator_alert("Should not send", "body")

        assert result is False
        patched_send.assert_not_called()

    def test_missing_alerts_block_returns_false_without_dispatch(self, monkeypatch, patched_send):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        from hermes_cli.operator_alerts import send_operator_alert

        result = send_operator_alert("Should not send")

        assert result is False
        patched_send.assert_not_called()

    def test_deliver_local_returns_false_without_dispatch(self, monkeypatch, patched_send):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"alerts": {"deliver": "local"}})
        from hermes_cli.operator_alerts import send_operator_alert

        result = send_operator_alert("Should not send")

        assert result is False
        patched_send.assert_not_called()


class TestDeliveryFailureNeverRaises:
    """(d) delivery raising -> False, no raise, one warning logged."""

    def test_send_to_platform_raising_is_swallowed(self, configured, monkeypatch, caplog):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: mock_cfg)
        monkeypatch.setattr(
            "tools.send_message_tool._send_to_platform",
            AsyncMock(side_effect=RuntimeError("platform API down")),
        )

        from hermes_cli.operator_alerts import send_operator_alert

        with caplog.at_level(logging.WARNING, logger="hermes_cli.operator_alerts"):
            result = send_operator_alert("Delivery will fail", "body")

        assert result is False
        assert any(
            "operator alert delivery failed" in rec.message for rec in caplog.records
        ), "expected exactly one delivery-failure warning to be logged"

    def test_error_result_dict_is_treated_as_failure(self, configured, monkeypatch, caplog):
        from unittest.mock import AsyncMock, MagicMock

        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: mock_cfg)
        monkeypatch.setattr(
            "tools.send_message_tool._send_to_platform",
            AsyncMock(return_value={"error": "bot token invalid"}),
        )

        from hermes_cli.operator_alerts import send_operator_alert

        with caplog.at_level(logging.WARNING, logger="hermes_cli.operator_alerts"):
            result = send_operator_alert("Delivery reports an error")

        assert result is False
        assert any("bot token invalid" in rec.message for rec in caplog.records)


class TestReconcileAlertLightsUp:
    """(e) cron/scheduler.py::_send_reconcile_alert routes through the real
    send_operator_alert now that hermes_cli.operator_alerts exists — the
    guarded ImportError fallback is no longer taken."""

    def test_reconcile_alert_reaches_platform_send(self, configured, patched_send):
        import cron.scheduler as s

        # sys.modules is not stubbed here (unlike
        # tests/cron/test_startup_reconciliation.py's guarded-import tests) —
        # the whole point of this test is that the REAL module (already
        # importable now that hermes_cli/operator_alerts.py exists) is
        # reached, not a fake stand-in. Deliberately not manipulating
        # sys.modules: doing so would make the reimported submodule diverge
        # from the `hermes_cli.operator_alerts` package attribute other
        # tests in this file rely on for monkeypatching.
        s._send_reconcile_alert("job-1", "nightly digest", "interrupted mid-run")

        patched_send.assert_called_once()
        sent_message = patched_send.call_args.kwargs.get("message") or patched_send.call_args[0][3]
        assert "job-1" in sent_message
        assert "nightly digest" in sent_message

    def test_reconcile_alert_survives_operator_alerts_raising(self, monkeypatch, caplog):
        """_send_reconcile_alert's own BaseException guard must still hold
        even with a real (not stubbed) operator_alerts module underneath —
        a raise from deep inside must not propagate to the reconciliation
        loop."""
        import cron.scheduler as s
        from hermes_cli import operator_alerts

        def _boom(*a, **k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(operator_alerts, "send_operator_alert", _boom)

        with caplog.at_level(logging.WARNING, logger="cron.scheduler"):
            s._send_reconcile_alert("job-2", "flaky job", "interrupted mid-run")

        assert any("alert delivery failed" in rec.message for rec in caplog.records)
