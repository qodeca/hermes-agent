"""Transition-only logging for ``_check_fn_cached`` (finding 13 / Task T13).

Before this change, every honored check_fn failure logged a WARNING on every
call once the TTL cache expired (30s), which on idle/gateway sessions (turns
>30s apart) meant an identical WARNING on every single turn -- ~9 lines/turn
in one overnight run. These tests pin the fix: WARNING only on a True->False
transition, INFO only on False->True, and DEBUG for repeated steady state.
"""

import logging

import pytest

from tools import registry


@pytest.fixture(autouse=True)
def _clean_check_fn_state():
    """Isolate the module-level TTL/log-state caches across tests."""
    registry.invalidate_check_fn_cache()
    registry._check_fn_last_logged_state.clear()
    yield
    registry.invalidate_check_fn_cache()
    registry._check_fn_last_logged_state.clear()


@pytest.fixture(autouse=True)
def _zero_ttl(monkeypatch):
    """Force every call to re-probe instead of serving the TTL cache."""
    monkeypatch.setattr(registry, "_CHECK_FN_TTL_SECONDS", 0.0)


def _warning_records(records, fn_name):
    return [
        r for r in records
        if r.levelno == logging.WARNING and fn_name in r.getMessage()
    ]


def _info_records(records, fn_name):
    return [
        r for r in records
        if r.levelno == logging.INFO and fn_name in r.getMessage()
    ]


def _debug_records(records, fn_name):
    return [
        r for r in records
        if r.levelno == logging.DEBUG and fn_name in r.getMessage()
    ]


def test_repeated_false_logs_exactly_one_warning(caplog):
    """3 consecutive False calls -> exactly one honored-failure WARNING."""

    def always_false():
        return False

    with caplog.at_level(logging.DEBUG, logger="tools.registry"):
        for _ in range(3):
            assert registry._check_fn_cached(always_false) is False

    warnings = _warning_records(caplog.records, "always_false")
    assert len(warnings) == 1
    assert "unavailable" in warnings[0].getMessage()

    # The 2nd and 3rd calls are steady-state repeats -> DEBUG, not WARNING.
    debugs = _debug_records(caplog.records, "always_false")
    assert len(debugs) == 2


def test_false_to_true_transition_logs_one_info(caplog):
    """Flipping a previously-False check_fn to True logs exactly one INFO."""
    state = {"value": False}

    def flippable():
        return state["value"]

    with caplog.at_level(logging.DEBUG, logger="tools.registry"):
        assert registry._check_fn_cached(flippable) is False
        assert registry._check_fn_cached(flippable) is False

        state["value"] = True
        assert registry._check_fn_cached(flippable) is True

    infos = _info_records(caplog.records, "flippable")
    assert len(infos) == 1
    assert "available again" in infos[0].getMessage()

    # No WARNING should fire once it recovers.
    warnings_after_recovery = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "flippable" in r.getMessage()
    ]
    assert len(warnings_after_recovery) == 1  # only the initial False


def test_repeated_true_does_not_log_warning_or_info(caplog):
    """Steady-state True (never having failed) never logs WARNING/INFO."""

    def always_true():
        return True

    with caplog.at_level(logging.DEBUG, logger="tools.registry"):
        for _ in range(3):
            assert registry._check_fn_cached(always_true) is True

    assert _warning_records(caplog.records, "always_true") == []
    assert _info_records(caplog.records, "always_true") == []


def test_invalidate_check_fn_cache_clears_logged_state(caplog):
    """A manual invalidation resets last-logged state so the next honored
    failure produces a fresh WARNING instead of being suppressed as steady
    state (recommended semantics from the task brief)."""

    def always_false():
        return False

    with caplog.at_level(logging.DEBUG, logger="tools.registry"):
        assert registry._check_fn_cached(always_false) is False
        assert registry._check_fn_cached(always_false) is False  # steady -> DEBUG

        registry.invalidate_check_fn_cache()

        assert registry._check_fn_cached(always_false) is False  # fresh -> WARNING

    warnings = _warning_records(caplog.records, "always_false")
    assert len(warnings) == 2
