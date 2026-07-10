"""Consecutive-API-failure circuit breaker for the conversation loop.

A backend that fails deterministically on every call can keep the retry
cascade alive far longer than ``api_max_retries`` suggests: fallback
activation, credential-pool rotation, and primary-transport recovery all
reset ``retry_count`` (7+ reset sites), so a session can hammer a dead
backend for hours (observed: 1,832 identical 400s over ~2 h against a
local oMLX runner). The breaker counts *consecutive failed API calls* in
a counter that lives outside that reset machinery, resets it only on a
successful API response, and aborts the turn with ``failed=True`` once
``agent.max_consecutive_api_failures`` (default 10, 0 = disabled) is hit.

Exercised end-to-end through ``AIAgent.run_conversation`` against an
in-process mock provider (same harness style as
``test_empty_tool_name_loop_dampening.py``) — no green mocks around the
retry cascade itself.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

import pytest

# Repo root = three levels up from tests/agent/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _err(status: int, body: dict) -> dict:
    """Queue entry for an HTTP error response."""
    return {"_status": status, "_body": body}


def _server_error() -> dict:
    return _err(500, {"error": {"message": "internal server error"}})


def _backend_capacity_error() -> dict:
    return _err(400, {"error": {"code": "prefill_memory_exceeded",
                                "message": "prefill_memory_exceeded"}})


def _tc_resp(name: str, args: str = "{}") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }


class _MockHandler(BaseHTTPRequestHandler):
    # Set by the fixture before each request cycle.
    captured_requests: list = []
    # Queue of response dicts.  Entries created by ``_err`` are returned as
    # HTTP errors; anything else as a normal chat completion.  When the
    # queue is empty, ``default_response`` repeats forever (so "always
    # failing backend" doesn't need a pre-sized queue).
    response_queue: list = []
    default_response: dict = {}
    # Optional hook called with the 1-based chat-request index before the
    # response is sent — lets a test flip agent state (e.g. request an
    # interrupt) at an exact point in the failure sequence.
    on_chat_request: Optional[Callable[[int], None]] = None

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        # Endpoint-probe traffic (context-length detection POSTs like
        # /api/show) must not consume the scripted queue or count as an
        # agent API call — only chat completions do.
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).captured_requests.append(req)
        _hook = type(self).on_chat_request
        if _hook is not None:
            _hook(len(type(self).captured_requests))
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = type(self).default_response or _text_resp("DONE")

        if "_status" in resp:
            body = json.dumps(resp["_body"]).encode()
            self.send_response(resp["_status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        is_stream = req.get("stream") is True
        msg = resp["choices"][0]["message"]
        if is_stream:
            content = msg.get("content") or ""
            tcs = msg.get("tool_calls")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks: list = [{"id": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
            if content:
                chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
            if tcs:
                for ti, tc in enumerate(tcs):
                    chunks.append({"id": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": ti, "id": tc["id"], "type": "function",
                        "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}]}, "finish_reason": None}]})
            chunks.append({"id": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}]})
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — endpoint probes; not agent API calls
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a, **kw):  # silence the default stderr logging
        pass


@pytest.fixture()
def agent_env(monkeypatch):
    """Mock provider + isolated HERMES_HOME; yields (agent, handler)."""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    _MockHandler.default_response = {}
    _MockHandler.on_chat_request = None
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_breaker_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")

    # Import fresh so the isolated HERMES_HOME is honored even when the
    # module was imported earlier in the same worker.
    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
        provider="openai-compat", model="test-model",
        max_iterations=30, enabled_toolsets=[],
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        save_trajectories=False, platform="cli",
    )
    agent.valid_tool_names = {"terminal", "read_file"}

    # Zero out retry backoff so error cascades run at test speed.
    import agent.conversation_loop as _cl
    monkeypatch.setattr(_cl, "jittered_backoff", lambda *a, **kw: 0.0)

    try:
        yield agent, _MockHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _stub_fallbacks(agent, activations: int):
    """Make ``_try_activate_fallback`` report success ``activations`` times.

    Exercises the loop's real fallback-reset lines (``retry_count = 0`` /
    ``compression_attempts = 0``) without needing a second live provider —
    the "fallback" keeps pointing at the same mock backend, which is exactly
    the repeated-failure scenario the breaker exists for.
    """
    # A non-empty chain so _has_pending_fallback() reflects remaining stubs.
    agent._fallback_chain = [
        {"provider": "openai-compat", "model": f"fb-{i}"} for i in range(activations)
    ]
    agent._fallback_index = 0
    state = {"left": activations}

    def _fake_activate(reason=None):
        if state["left"] <= 0:
            return False
        state["left"] -= 1
        agent._fallback_index += 1
        agent._fallback_activated = True
        return True

    agent._try_activate_fallback = _fake_activate


def test_breaker_trips_after_threshold(agent_env):
    """An always-failing backend must stop after exactly the configured
    number of consecutive failed API calls, with a clear final message."""
    agent, handler = agent_env
    handler.default_response = _server_error()
    agent._api_max_retries = 10          # natural exhaustion would need 10
    agent._max_consecutive_api_failures = 4

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "failing repeatedly" in result["final_response"]
    assert "server_error" in result["final_response"]
    assert len(handler.captured_requests) == 4


def test_breaker_counter_survives_fallback_resets(agent_env):
    """Fallback activation resets retry_count/compression_attempts — the
    breaker counter must keep counting across those resets."""
    agent, handler = agent_env
    handler.default_response = _server_error()
    agent._api_max_retries = 2
    agent._max_consecutive_api_failures = 5
    # Two fallback activations: retry_count is reset after attempts 2 and 4,
    # so without a reset-immune counter the loop would only ever see
    # retry_count <= 2. The breaker must still trip on total attempt 5.
    _stub_fallbacks(agent, activations=2)

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "failing repeatedly" in result["final_response"]
    assert len(handler.captured_requests) == 5


def test_success_resets_breaker_counter(agent_env):
    """One successful API call mid-stream must reset the consecutive-failure
    count — only an unbroken run of failures may trip the breaker."""
    agent, handler = agent_env
    handler.response_queue = [
        _server_error(),
        _server_error(),
        _tc_resp("frobnicate_xyz"),   # success: unknown tool → error fed back to model
    ]
    handler.default_response = _server_error()
    agent._api_max_retries = 10
    agent._max_consecutive_api_failures = 4

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "failing repeatedly" in result["final_response"]
    # 2 failures + 1 success (reset) + 4 fresh failures to trip.
    assert len(handler.captured_requests) == 2 + 1 + 4


def test_pending_interrupt_wins_over_breaker_trip(agent_env):
    """If an interrupt is already pending when the threshold-tripping failure
    is classified, the turn must exit via the interrupt path (interrupted,
    flag cleared) — not as a breaker failure. A stale flag would otherwise
    instantly abort the NEXT run_conversation at the top-of-loop check.

    The window under test is narrow: an interrupt raised DURING the API call
    is already converted to InterruptedError by the interruptible call
    wrapper, so to land the flag between "exception raised" and "breaker
    check" deterministically we set it from the api_request_error hook,
    which the loop invokes immediately before the breaker branch."""
    agent, handler = agent_env
    handler.default_response = _server_error()
    agent._api_max_retries = 10
    agent._max_consecutive_api_failures = 2

    _orig_hook = agent._invoke_api_request_error_hook
    _calls = {"n": 0}

    def _hook_and_interrupt(*args, **kwargs):
        _calls["n"] += 1
        if _calls["n"] == 2:  # the threshold-tripping failure
            agent._interrupt_requested = True
        return _orig_hook(*args, **kwargs)

    agent._invoke_api_request_error_hook = _hook_and_interrupt

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result.get("interrupted") is True
    assert result.get("failed") is not True
    assert "failing repeatedly" not in (result.get("final_response") or "")
    assert agent._interrupt_requested is False  # flag cleared

    # The next turn must run normally, not abort instantly on a stale flag.
    handler.on_chat_request = None
    handler.default_response = _text_resp("second turn ok")
    before = len(handler.captured_requests)
    result2 = agent.run_conversation("again", conversation_history=[], task_id="t")

    assert result2.get("interrupted") is not True
    assert len(handler.captured_requests) > before  # an API call was made
    assert "second turn ok" in (result2.get("final_response") or "")


def test_threshold_zero_disables_breaker(agent_env):
    """0 = disabled: the loop exits via the normal max-retries path."""
    agent, handler = agent_env
    handler.default_response = _server_error()
    agent._api_max_retries = 2
    agent._max_consecutive_api_failures = 0

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert "failing repeatedly" not in result["final_response"]
    assert len(handler.captured_requests) == 2   # natural max-retries exit


def test_backend_capacity_aborts_before_threshold(agent_env):
    """A deterministic backend memory-guard 400 (backend_capacity,
    retryable=False) must abort immediately via the non-retryable
    client-error path — not burn the breaker threshold."""
    agent, handler = agent_env
    handler.default_response = _backend_capacity_error()
    agent._api_max_retries = 10
    agent._max_consecutive_api_failures = 10

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert len(handler.captured_requests) == 1
    # It must not be the breaker that ended the turn.
    assert "failing repeatedly" not in result["final_response"]


def test_backend_capacity_gets_one_fallback_attempt(agent_env):
    """With a configured fallback, backend_capacity gives the fallback one
    chance, then aborts when it fails the same way."""
    agent, handler = agent_env
    handler.default_response = _backend_capacity_error()
    agent._api_max_retries = 10
    agent._max_consecutive_api_failures = 10
    _stub_fallbacks(agent, activations=1)

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert result["failed"] is True
    assert len(handler.captured_requests) == 2


def test_breaker_threshold_config_default():
    """The knob is wired from config with a sane default (10)."""
    test_home = tempfile.mkdtemp(prefix="hermes_breaker_cfg_")
    os.makedirs(os.path.join(test_home, ".hermes"))
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = os.path.join(test_home, ".hermes")
    try:
        for mod in list(sys.modules):
            if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
                del sys.modules[mod]
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="http://127.0.0.1:1/v1",
            provider="openai-compat", model="test-model",
            max_iterations=5, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        assert agent._max_consecutive_api_failures == 10
    finally:
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home
