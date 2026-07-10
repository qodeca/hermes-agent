"""Tests for the gateway restart/stop self-targeting guard hardening (finding 26).

The guard used to be a bare ``os.getenv("_HERMES_GATEWAY") == "1"`` check. If
that env var is stripped in a child context (e.g. a sandboxed terminal-tool
backend), the guard fails open and an agent-reachable `hermes gateway
restart` can self-kill loop against a KeepAlive supervisor.

``_invoked_from_within_gateway()`` adds a second, independent signal: walk
this process's parent-PID ancestry and compare against the PID recorded in
``gateway.pid``. Either signal refuses. These tests exercise
``_invoked_from_within_gateway()`` directly (its two seams,
``gateway.status.get_running_pid`` and ``hermes_cli.gateway._ancestor_pids``,
are monkeypatched rather than driving real OS process trees or file locks —
both underlying primitives already have their own dedicated test coverage in
``tests/gateway/test_status.py``) and confirm ``hermes gateway restart``
wires the result into the refuse/allow decision end-to-end.
"""

from argparse import Namespace

import pytest


class _Reached(Exception):
    """Raised by a sentinel to prove control passed the guard."""


def _restart_args() -> Namespace:
    return Namespace(gateway_command="restart", all=False, system=False)


def _run_restart_expect_refused(monkeypatch):
    import hermes_cli.gateway as gw

    with pytest.raises(SystemExit) as exc_info:
        gw.gateway_command(_restart_args())
    assert exc_info.value.code == 1


def _run_restart_expect_allowed(monkeypatch):
    """Assert the guard does not fire — control reaches past it.

    Short-circuits the first downstream call with a sentinel so the test
    doesn't drive real signal delivery / service dispatch.
    """
    import hermes_cli.gateway as gw

    def _sentinel(*a, **k):
        raise _Reached()

    monkeypatch.setattr(gw, "_dispatch_via_service_manager_if_s6", _sentinel)
    monkeypatch.setattr(gw, "_dispatch_all_via_service_manager_if_s6", _sentinel)
    with pytest.raises(_Reached):
        gw.gateway_command(_restart_args())


class TestInvokedFromWithinGatewayHelper:
    """Direct tests of ``_invoked_from_within_gateway()``'s decision logic."""

    def test_env_var_set_refuses(self, monkeypatch):
        import hermes_cli.gateway as gw

        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        # No recorded gateway PID at all — the env signal alone must suffice.
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: None
        )
        assert gw._invoked_from_within_gateway() is True

    def test_env_unset_ancestry_contains_gateway_pid_refuses(self, monkeypatch):
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: 4242
        )
        # Simulate an ancestry walk that reaches the recorded gateway PID a
        # few hops up (gateway -> agent loop -> terminal tool -> shell).
        monkeypatch.setattr(gw, "_ancestor_pids", lambda: [111, 222, 4242])
        assert gw._invoked_from_within_gateway() is True

    def test_neither_signal_allows(self, monkeypatch):
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: 4242
        )
        monkeypatch.setattr(gw, "_ancestor_pids", lambda: [111, 222, 333])
        assert gw._invoked_from_within_gateway() is False

    def test_ancestry_walk_raising_env_set_still_refuses(self, monkeypatch):
        """On any ancestry-walk error, the env check alone governs — with
        the env var set, that means refuse."""
        import hermes_cli.gateway as gw

        monkeypatch.setenv("_HERMES_GATEWAY", "1")

        def _boom(*a, **k):
            raise RuntimeError("psutil and ps both unavailable")

        monkeypatch.setattr("gateway.status.get_running_pid", _boom)
        assert gw._invoked_from_within_gateway() is True

    def test_ancestry_walk_raising_env_unset_allows(self, monkeypatch):
        """On any ancestry-walk error, the env check alone governs — with
        the env var unset, that means allow. This is the fail-open-to-env
        behavior: the command must stay usable for a human operator at a
        real shell even when psutil/ps are unavailable."""
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

        def _boom(*a, **k):
            raise RuntimeError("psutil and ps both unavailable")

        monkeypatch.setattr("gateway.status.get_running_pid", _boom)
        assert gw._invoked_from_within_gateway() is False

    def test_ancestor_pids_walker_raising_falls_back_to_env(self, monkeypatch):
        """A failure inside the ancestry walker itself (not just PID
        resolution) is covered by the same fallback."""
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: 4242
        )

        def _boom():
            raise RuntimeError("ancestry walk failed")

        monkeypatch.setattr(gw, "_ancestor_pids", _boom)
        assert gw._invoked_from_within_gateway() is False

    def test_missing_pid_file_allows_when_env_unset(self, monkeypatch):
        """No recorded gateway PID (missing gateway.pid) → allowed."""
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: None
        )
        assert gw._invoked_from_within_gateway() is False

    def test_stale_pid_record_allows_when_env_unset(self, monkeypatch):
        """A stale/dead recorded PID: get_running_pid() itself resolves
        this to None (it validates liveness + start-time match), so the
        guard must allow — same code path as a missing PID file."""
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        # get_running_pid() returns None for a stale record (dead process,
        # or a live process whose start time no longer matches the record)
        # — verified by its own dedicated tests in tests/gateway/test_status.py.
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: None
        )
        monkeypatch.setattr(gw, "_ancestor_pids", lambda: [111, 222, 333])
        assert gw._invoked_from_within_gateway() is False


class TestAncestorPids:
    """``_ancestor_pids`` is the isolated ancestry-walk primitive."""

    def test_includes_self(self):
        import hermes_cli.gateway as gw
        import os

        chain = gw._ancestor_pids()
        assert chain[0] == os.getpid()

    def test_depth_is_capped(self, monkeypatch):
        import hermes_cli.gateway as gw

        # A pathological, always-novel parent chain must still stop at the
        # cap rather than looping until _get_parent_pid gives up on its own.
        counter = {"pid": 1000}

        def _fake_parent(pid):
            counter["pid"] += 1
            return counter["pid"]

        monkeypatch.setattr(gw, "_get_parent_pid", _fake_parent)
        chain = gw._ancestor_pids(max_depth=3)
        assert len(chain) == 4  # self + 3 hops


class TestGatewayRestartGuardIntegration:
    """End-to-end through ``hermes gateway restart``."""

    def test_restart_refuses_inside_gateway_env_only(self, monkeypatch):
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
        _run_restart_expect_refused(monkeypatch)

    def test_restart_refuses_via_ancestry_when_env_stripped(self, monkeypatch):
        import hermes_cli.gateway as gw

        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: 4242
        )
        monkeypatch.setattr(gw, "_ancestor_pids", lambda: [111, 4242])
        _run_restart_expect_refused(monkeypatch)

    def test_restart_allows_outside_gateway(self, monkeypatch):
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
        monkeypatch.setattr(
            "gateway.status.get_running_pid", lambda *a, **k: None
        )
        _run_restart_expect_allowed(monkeypatch)
