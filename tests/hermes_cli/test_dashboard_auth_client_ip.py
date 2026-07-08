"""Tests for the trusted-proxy-aware client-IP resolver.

Security contract: on a direct bind (``dashboard.trusted_proxy`` unset), a
client-supplied ``X-Forwarded-For`` header MUST be ignored so the login rate
limiter keys on the real transport peer and cannot be bypassed by varying the
header. When explicitly behind a trusted proxy, the first XFF hop is honoured.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from hermes_cli.dashboard_auth.client_ip import client_ip, trust_forwarded_for


def _make_request(headers: dict[str, str], client=("10.0.0.5", 1234)) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/password-login",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": client,
    }
    return Request(scope)


@pytest.fixture
def set_trusted_proxy(monkeypatch):
    """Patch config so trust_forwarded_for() returns the requested value."""

    def _apply(value: bool) -> None:
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"dashboard": {"trusted_proxy": value}},
        )

    return _apply


def test_xff_ignored_by_default(set_trusted_proxy):
    set_trusted_proxy(False)
    req = _make_request({"x-forwarded-for": "1.2.3.4"})
    # The spoofable header is ignored; the real peer wins.
    assert client_ip(req) == "10.0.0.5"


def test_spoofed_xff_cannot_split_the_rate_limit_bucket(set_trusted_proxy):
    set_trusted_proxy(False)
    # Two requests from the SAME peer with DIFFERENT spoofed XFF values must
    # resolve to the same key, so they land in the same rate-limit bucket.
    a = _make_request({"x-forwarded-for": "9.9.9.1"})
    b = _make_request({"x-forwarded-for": "9.9.9.2"})
    assert client_ip(a) == client_ip(b) == "10.0.0.5"


def test_xff_honoured_behind_trusted_proxy(set_trusted_proxy):
    set_trusted_proxy(True)
    req = _make_request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    # First hop (the real client in front of the proxy) is used.
    assert client_ip(req) == "203.0.113.7"
    assert trust_forwarded_for() is True


def test_no_xff_uses_peer(set_trusted_proxy):
    set_trusted_proxy(False)
    req = _make_request({})
    assert client_ip(req) == "10.0.0.5"


def test_missing_client_returns_empty(set_trusted_proxy):
    set_trusted_proxy(False)
    req = _make_request({"x-forwarded-for": "1.2.3.4"}, client=None)
    assert client_ip(req) == ""


def test_default_config_key_absent_is_untrusted(monkeypatch):
    # No dashboard.trusted_proxy key at all → default False → XFF ignored.
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    req = _make_request({"x-forwarded-for": "1.2.3.4"})
    assert trust_forwarded_for() is False
    assert client_ip(req) == "10.0.0.5"
