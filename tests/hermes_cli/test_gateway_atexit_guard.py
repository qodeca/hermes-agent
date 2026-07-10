"""Tests for the gateway exit-diagnostic atexit hook registration guard.

Verifies that the atexit hook is registered exactly once even if the
registration path is called multiple times in the same process.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_atexit_hook_registered_exactly_once(monkeypatch):
    """Calling run_gateway twice should register the atexit hook only once."""
    # Track how many times atexit.register is called with _atexit_hook
    hook_registrations = []

    original_register = __import__("atexit").register

    def mock_register(func):
        # Only count registrations of our specific hook function
        if hasattr(func, "__name__") and func.__name__ == "_atexit_hook":
            hook_registrations.append(func)
        return original_register(func)

    # Patch functions that run_gateway calls before the atexit registration
    with patch("hermes_cli.gateway._guard_official_docker_root_gateway"), \
         patch("hermes_cli.gateway._guard_named_profile_under_multiplexer"), \
         patch("hermes_cli.gateway._guard_supervised_gateway_conflict"), \
         patch("hermes_cli.gateway._guard_existing_gateway_process_conflict"), \
         patch("hermes_cli.gateway.supports_systemd_services", return_value=False), \
         patch("asyncio.run", side_effect=KeyboardInterrupt()), \
         patch("atexit.register", side_effect=mock_register):
        from hermes_cli.gateway import run_gateway

        # Call run_gateway twice (it catches KeyboardInterrupt and returns normally)
        run_gateway(replace=False)
        run_gateway(replace=False)

    # Verify that the _atexit_hook was registered exactly once
    # BEFORE the fix, this would be 2. AFTER the fix, it should be 1.
    assert len(hook_registrations) == 1, f"Expected _atexit_hook to be registered once, but was registered {len(hook_registrations)} times"
