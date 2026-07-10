"""Tests for the background-review denial-count circuit breaker (finding 16).

Incident: the background curator retried denied privileged writes 11 times
across sessions, varying its approach (patch -> write_file -> patch) but
never giving up. Per OWASP AI Agent guidance, retry-after-denial is an
excessive-agency signal that needs a hard stop: after ``max_denials`` denied
privileged attempts accumulate on a single whitelisted thread (the
background review fork), further denials return a terminal message instead
of the normal per-tool deny text, and the caller (``agent/background_review.py``)
aborts the fork and alerts an operator, instead of letting the model keep
retrying variations forever.

Two denial paths feed the SAME counter
(``hermes_cli.plugins.record_thread_tool_denial``):

  1. The whitelist block in ``_get_pre_tool_call_directive_details`` -- a
     tool outright not on the review fork's whitelist (e.g. "terminal").
  2. In-tool guard refusals from *inside* an already-whitelisted tool --
     ``tools/skill_manager_tool.py``'s ``_background_review_write_guard``
     denying a patch/write_file/edit/delete against a pinned/protected/
     external skill. This is what MOST of the incident's denials actually
     were (the whitelist never blocked ``skill_manage`` itself -- it's an
     allowed tool; the denial happens deeper, inside the tool).

Both paths must count toward the same breaker for it to actually catch the
incident pattern -- a breaker that only watched whitelist blocks would have
let the 11-retry incident through untouched.
"""

from hermes_cli.plugins import (
    DEFAULT_MAX_TOOL_DENIALS,
    DENIAL_ABORT_MESSAGE,
    clear_thread_tool_whitelist,
    get_pre_tool_call_block_message,
    record_thread_tool_denial,
    set_thread_tool_whitelist,
)
from tools.skill_manager_tool import _deny_background_review_write


def _stub_invoke_hook(monkeypatch):
    """No plugin hooks registered -- isolates the whitelist logic under test."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda hook_name, **kwargs: [])


class TestRecordThreadToolDenialCounter:
    def test_noop_outside_active_whitelist(self, monkeypatch):
        # No set_thread_tool_whitelist call on this thread -- must not track.
        clear_thread_tool_whitelist()
        assert record_thread_tool_denial("terminal") is None

    def test_returns_none_under_threshold(self):
        set_thread_tool_whitelist({"memory"}, max_denials=5)
        try:
            for _ in range(4):
                assert record_thread_tool_denial("terminal") is None
        finally:
            clear_thread_tool_whitelist()

    def test_returns_abort_message_at_threshold(self):
        set_thread_tool_whitelist({"memory"}, max_denials=5)
        try:
            for _ in range(4):
                record_thread_tool_denial("terminal")
            assert record_thread_tool_denial("terminal") == DENIAL_ABORT_MESSAGE
        finally:
            clear_thread_tool_whitelist()

    def test_stays_aborted_on_further_denials(self):
        set_thread_tool_whitelist({"memory"}, max_denials=5)
        try:
            for _ in range(7):
                result = record_thread_tool_denial("terminal")
            # 7th call (past the threshold of 5) still returns the terminal
            # message -- the breaker does not reset itself mid-fork.
            assert result == DENIAL_ABORT_MESSAGE
        finally:
            clear_thread_tool_whitelist()

    def test_on_denial_exceeded_fires_exactly_once(self):
        calls = []
        set_thread_tool_whitelist(
            {"memory"}, max_denials=5,
            on_denial_exceeded=lambda denied: calls.append(list(denied)),
        )
        try:
            for _ in range(6):
                record_thread_tool_denial("terminal")
            assert len(calls) == 1
            assert calls[0] == ["terminal"] * 5
        finally:
            clear_thread_tool_whitelist()

    def test_denial_count_cleared_with_whitelist(self):
        """Requirement (c): the next review fork starts with a fresh counter."""
        set_thread_tool_whitelist({"memory"}, max_denials=5)
        for _ in range(4):
            record_thread_tool_denial("terminal")
        clear_thread_tool_whitelist()

        set_thread_tool_whitelist({"memory"}, max_denials=5)
        try:
            # A fresh whitelist should need another 5 denials to trip --
            # if the counter had leaked across the clear, this single call
            # would already be at "5" and return the abort message.
            assert record_thread_tool_denial("terminal") is None
        finally:
            clear_thread_tool_whitelist()


class TestWhitelistBlockDirectiveUsesDenialCounter:
    def test_message_is_normal_deny_text_under_threshold(self, monkeypatch):
        _stub_invoke_hook(monkeypatch)
        set_thread_tool_whitelist({"memory"}, deny_msg_fmt="denied: {tool_name}", max_denials=5)
        try:
            msg = get_pre_tool_call_block_message("terminal", {})
            assert msg == "denied: terminal"
        finally:
            clear_thread_tool_whitelist()

    def test_message_becomes_terminal_at_threshold(self, monkeypatch):
        _stub_invoke_hook(monkeypatch)
        set_thread_tool_whitelist({"memory"}, deny_msg_fmt="denied: {tool_name}", max_denials=5)
        try:
            for _ in range(4):
                get_pre_tool_call_block_message("terminal", {})
            msg = get_pre_tool_call_block_message("terminal", {})
            assert msg == DENIAL_ABORT_MESSAGE
        finally:
            clear_thread_tool_whitelist()


class TestLegacyCallersGetDefaultMaxDenials:
    """Requirement (d): callers that don't pass max_denials get the default
    cap rather than unbounded retries -- and a caller that only ever denies
    once (the common single-mistake case, e.g. existing test_plugins.py
    coverage) sees completely unchanged behavior."""

    def test_default_is_five(self):
        assert DEFAULT_MAX_TOOL_DENIALS == 5

    def test_no_max_denials_kwarg_uses_default(self):
        set_thread_tool_whitelist({"memory"})  # no max_denials passed
        try:
            for _ in range(4):
                assert record_thread_tool_denial("terminal") is None
            assert record_thread_tool_denial("terminal") == DENIAL_ABORT_MESSAGE
        finally:
            clear_thread_tool_whitelist()

    def test_single_denial_caller_sees_unchanged_message(self, monkeypatch):
        _stub_invoke_hook(monkeypatch)
        set_thread_tool_whitelist({"memory"}, deny_msg_fmt="denied: {tool_name}")
        try:
            msg = get_pre_tool_call_block_message("terminal", {})
            assert msg == "denied: terminal"
        finally:
            clear_thread_tool_whitelist()


class TestBackgroundReviewWriteGuardCountsTowardBreaker:
    """tools/skill_manager_tool.py's in-tool guard denials (pinned/external/
    protected-builtin/hub-installed/bundled skill refusals) must count
    against the SAME breaker as whitelist blocks -- this is the seam the
    task brief asked for, since these guard refusals were most of the
    incident's actual denied calls."""

    def test_guard_denial_recorded_and_terminalized_at_threshold(self):
        set_thread_tool_whitelist({"skill_manage"}, max_denials=5)
        try:
            for _ in range(4):
                result = _deny_background_review_write("patch", "pinned skill, refused")
                assert result["error"] == "pinned skill, refused"

            result = _deny_background_review_write("patch", "pinned skill, refused")
            assert result["success"] is False
            assert result["error"] == DENIAL_ABORT_MESSAGE
        finally:
            clear_thread_tool_whitelist()

    def test_guard_denial_noop_outside_review_thread(self):
        # No whitelist active (e.g. a foreground call path) -- must not
        # raise and must preserve the caller's own error text.
        clear_thread_tool_whitelist()
        result = _deny_background_review_write("delete", "some other refusal")
        assert result == {"success": False, "error": "some other refusal"}


class TestMixedDenialPathsFakeReviewLoop:
    """TDD point (a): a fake review loop issuing 6 denied calls, mixing
    whitelist blocks (a tool outright outside the fork's allowed set) and
    in-tool guard refusals (skill_manage denying a privileged write from
    inside an allowed tool) -- exactly the shape of the real incident.
    Abort must trip after the 5th denial, the terminal message must be
    returned, and the alert/abort callback must fire exactly once, carrying
    every denied name seen so far."""

    def test_six_mixed_denials_trip_breaker_once(self, monkeypatch):
        _stub_invoke_hook(monkeypatch)
        alert_calls = []
        set_thread_tool_whitelist(
            {"skill_manage", "memory"},
            deny_msg_fmt="Background review denied non-whitelisted tool: {tool_name}.",
            max_denials=5,
            on_denial_exceeded=lambda denied: alert_calls.append(list(denied)),
        )
        try:
            results = []

            # 1: whitelist block (tool not allowed at all)
            results.append(get_pre_tool_call_block_message("terminal", {}))
            # 2: in-tool guard denial (patch against a pinned skill)
            results.append(
                _deny_background_review_write("patch", "pinned skill refused")["error"]
            )
            # 3: in-tool guard denial (write_file against the same skill --
            # the model varying its approach, as in the incident)
            results.append(
                _deny_background_review_write("write_file", "pinned skill refused")["error"]
            )
            # 4: whitelist block (a different disallowed tool)
            results.append(get_pre_tool_call_block_message("terminal", {}))
            # 5: in-tool guard denial -- this is the one that trips the breaker
            results.append(
                _deny_background_review_write("patch", "pinned skill refused")["error"]
            )
            # 6: one more denial after the trip
            results.append(get_pre_tool_call_block_message("terminal", {}))

            # Denials 1-4 got their own (non-terminal) denial text.
            assert results[0] == "Background review denied non-whitelisted tool: terminal."
            assert results[1] == "pinned skill refused"
            assert results[2] == "pinned skill refused"
            assert results[3] == "Background review denied non-whitelisted tool: terminal."
            # Denial 5 (crossing the threshold) and denial 6 (after it) are
            # both terminalized.
            assert results[4] == DENIAL_ABORT_MESSAGE
            assert results[5] == DENIAL_ABORT_MESSAGE

            # Alert/abort callback fired exactly once, with the tool/action
            # names denied so far (in order), suitable for an operator alert.
            assert len(alert_calls) == 1
            assert alert_calls[0] == [
                "terminal",
                "skill_manage:patch",
                "skill_manage:write_file",
                "terminal",
                "skill_manage:patch",
            ]
        finally:
            clear_thread_tool_whitelist()


class TestUnderThresholdNoAbort:
    """TDD point (b): denials under the threshold must never trip the
    breaker or fire the alert callback."""

    def test_four_denials_no_abort_no_alert(self, monkeypatch):
        _stub_invoke_hook(monkeypatch)
        alert_calls = []
        set_thread_tool_whitelist(
            {"skill_manage"},
            deny_msg_fmt="denied: {tool_name}",
            max_denials=5,
            on_denial_exceeded=lambda denied: alert_calls.append(list(denied)),
        )
        try:
            for _ in range(2):
                msg = get_pre_tool_call_block_message("terminal", {})
                assert msg == "denied: terminal"
            for _ in range(2):
                result = _deny_background_review_write("patch", "pinned skill refused")
                assert result["error"] == "pinned skill refused"

            assert alert_calls == []
        finally:
            clear_thread_tool_whitelist()


class TestBackgroundReviewWiresAbortCallback:
    """Source-level check that agent/background_review.py actually installs
    the abort callback on set_thread_tool_whitelist -- catches the case
    where the breaker logic below is correct in isolation but never gets
    wired into the real review fork."""

    def test_run_review_in_thread_wires_on_denial_exceeded(self):
        import inspect
        import agent.background_review as background_review

        source = inspect.getsource(background_review._run_review_in_thread)
        assert "on_denial_exceeded=" in source
        assert "record_thread_tool_denial" not in source  # counting stays in plugins.py
        assert "_interrupt_requested = True" in source
        assert "send_operator_alert" in source


class TestBackgroundReviewSetsInterruptOnAbort:
    """Functional test of the exact abort-callback pattern
    agent/background_review.py installs (same operations, same order):
    setting the review agent's _interrupt_requested flag -- the same flag
    run_conversation's tool loop already checks at the top of every round
    (agent/conversation_loop.py) -- and sending exactly one operator alert.
    Exercised against a fake agent/monkeypatched alert sender rather than a
    real review fork, which needs live model credentials to spin up."""

    def test_abort_callback_sets_interrupt_flag(self, monkeypatch):
        class _FakeAgent:
            def __init__(self):
                self._interrupt_requested = False

        review_agent = _FakeAgent()
        alert_sent = {}

        def _fake_send_operator_alert(title, body="", **kwargs):
            alert_sent["title"] = title
            alert_sent["body"] = body
            return True

        monkeypatch.setattr(
            "hermes_cli.operator_alerts.send_operator_alert",
            _fake_send_operator_alert,
        )

        # Recreate the closure background_review.py installs, bound to our
        # fake agent, to verify its behavior without spinning up a real
        # review fork (which needs live model credentials).
        def _abort_review_on_repeated_denials(denied):
            review_agent._interrupt_requested = True
            from hermes_cli.operator_alerts import send_operator_alert
            send_operator_alert(
                "Background review aborted: repeated denied privileged attempts",
                "Denied tool/action calls: " + ", ".join(denied),
            )

        set_thread_tool_whitelist(
            {"memory"}, max_denials=5,
            on_denial_exceeded=_abort_review_on_repeated_denials,
        )
        try:
            for _ in range(5):
                record_thread_tool_denial("skill_manage:patch")
        finally:
            clear_thread_tool_whitelist()

        assert review_agent._interrupt_requested is True
        assert alert_sent["title"] == (
            "Background review aborted: repeated denied privileged attempts"
        )
        assert "skill_manage:patch" in alert_sent["body"]
