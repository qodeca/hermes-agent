"""Degeneration detection wired into the conversation loop.

End-to-end through ``AIAgent.run_conversation`` against an in-process mock
provider (same harness style as ``test_output_token_budget.py`` /
``test_conversation_breaker.py``). Verifies the escalation contract:

- FIRST detection: a steering instruction is appended to the NEWEST tool
  result (never a synthetic user/system message — the cached prompt prefix
  and strict role alternation stay intact).
- SECOND consecutive detection: the turn ends via the same clean terminal
  path the budget/breaker exits use, with exit reason
  ``degeneration_detected`` and the transcript on a complete
  assistant(+tool) turn.
- A clean (non-degenerate) message in between resets the strike count.
- Detection OFF (the interactive default) → no intervention at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# Repo root = three levels up from tests/agent/<file>.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The incident loop unit: the same line, hundreds of times.
INCIDENT_LINE = "I'm done. Let me write the response now. Final: Hello Marcin!"
DEGENERATE_TEXT = "\n".join([INCIDENT_LINE] * 40)

VARIED_TEXT = (
    "Progress update: the log rotation is configured, the health probe "
    "returns ready, and the remaining work is the retention policy for "
    "archived sessions. I will check the storage layout next."
)

STEER_MARKER = "[HERMES SYSTEM NOTICE"


def _tc_resp(name: str, args: str = "{}", content: str = "") -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": content,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
    }


def _text_resp(text: str) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50, "total_tokens": 60},
    }


class _MockHandler(BaseHTTPRequestHandler):
    captured_requests: list = []
    response_queue: list = []
    default_response: dict = {}

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        # Endpoint probes must not consume the scripted queue.
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).captured_requests.append(req)
        if type(self).response_queue:
            resp = type(self).response_queue.pop(0)
        else:
            resp = type(self).default_response or _text_resp("DONE")

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
            chunks.append({"id": "m",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tcs else "stop"}],
                           "usage": resp.get("usage")})
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

    def log_message(self, *a, **kw):
        pass


@pytest.fixture()
def agent_env(monkeypatch):
    """Mock provider + isolated HERMES_HOME; yields (make_agent, handler)."""
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    _MockHandler.default_response = {}
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_degen_")
    hermes_home = os.path.join(test_home, ".hermes")
    os.makedirs(hermes_home)
    prev_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = hermes_home

    def make_agent(config_yaml: str = None, **kwargs):
        if config_yaml is not None:
            with open(os.path.join(hermes_home, "config.yaml"), "w", encoding="utf-8") as f:
                f.write(config_yaml)
        # Import fresh so the isolated HERMES_HOME (and its config.yaml)
        # is honored even when modules were imported earlier in the worker.
        for mod in list(sys.modules):
            if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
                del sys.modules[mod]
        from run_agent import AIAgent

        agent_kwargs = dict(
            api_key="test-key", base_url=f"http://127.0.0.1:{port}/v1",
            provider="openai-compat", model="test-model",
            max_iterations=30, enabled_toolsets=[],
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            save_trajectories=False, platform="cli",
        )
        agent_kwargs.update(kwargs)
        agent = AIAgent(**agent_kwargs)
        agent.valid_tool_names = {"terminal", "read_file"}

        import agent.conversation_loop as _cl
        monkeypatch.setattr(_cl, "jittered_backoff", lambda *a, **kw: 0.0)
        return agent

    try:
        yield make_agent, _MockHandler
    finally:
        srv.shutdown()
        shutil.rmtree(test_home, ignore_errors=True)
        if prev_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev_home


def _assert_valid_transcript(messages):
    """Transcript invariants: no two consecutive same-role messages except
    tool results (parallel tool batches), and every tool message follows an
    assistant tool_calls message (directly or via another tool result)."""
    prev_role = None
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == prev_role and role != "tool":
            raise AssertionError(
                f"role alternation violated at index {i}: two consecutive {role!r} messages"
            )
        if role == "tool":
            assert prev_role in {"assistant", "tool"}, (
                f"dangling tool message at index {i} after {prev_role!r}"
            )
        prev_role = role


def _tool_messages(request: dict) -> list:
    return [m for m in request["messages"] if m.get("role") == "tool"]


# ── detection ON: steer, then exit ───────────────────────────────────────

def test_first_detection_steers_second_consecutive_exits(agent_env):
    """Strike 1 appends the steering note to the newest tool result; a
    second consecutive degenerate message ends the turn with exit reason
    degeneration_detected — no synthetic message anywhere."""
    make_agent, handler = agent_env
    agent = make_agent()
    agent._degeneration_detection = True
    handler.default_response = _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT)

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    # Call 1 produces strike 1 (steered); call 2 produces strike 2 → exit
    # before any third call.
    assert len(handler.captured_requests) == 2
    assert result["turn_exit_reason"] == "degeneration_detected"

    # The steering note rode the newest tool result of request 2.
    steer_req_tool_msgs = _tool_messages(handler.captured_requests[1])
    assert steer_req_tool_msgs, "steered request lost the tool result"
    assert STEER_MARKER in steer_req_tool_msgs[-1]["content"]
    assert "repeat" in steer_req_tool_msgs[-1]["content"].lower()
    # ...and only there — request 1 predates any detection.
    for m in _tool_messages(handler.captured_requests[0]):
        assert STEER_MARKER not in m["content"]

    # Transcript: ends on a complete assistant+tool turn, strict alternation,
    # and no user/system message was injected mid-loop.
    msgs = result["messages"]
    assert msgs[-1]["role"] == "tool"
    _assert_valid_transcript(msgs)
    first_user_idx = next(i for i, m in enumerate(msgs) if m.get("role") == "user")
    assert all(m.get("role") != "system" for m in msgs[first_user_idx:])
    assert sum(1 for m in msgs[first_user_idx:] if m.get("role") == "user") == 1


def test_steered_model_that_recovers_finishes_normally(agent_env):
    """The steering note works: a clean final answer after strike 1 ends
    the turn normally."""
    make_agent, handler = agent_env
    agent = make_agent()
    agent._degeneration_detection = True
    handler.response_queue = [
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        _text_resp("Hello Marcin! (final answer, delivered once)"),
    ]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert len(handler.captured_requests) == 2
    assert str(result["turn_exit_reason"]).startswith("text_response")
    assert "final answer" in (result["final_response"] or "")
    # The recovery request did carry the steering note.
    tool_msgs = _tool_messages(handler.captured_requests[1])
    assert tool_msgs and STEER_MARKER in tool_msgs[-1]["content"]
    _assert_valid_transcript(result["messages"])


def test_non_consecutive_detections_do_not_exit(agent_env):
    """A clean message between two degenerate ones resets the strike count:
    detections at calls 1 and 3 never reach two CONSECUTIVE strikes."""
    make_agent, handler = agent_env
    agent = make_agent()
    agent._degeneration_detection = True
    handler.response_queue = [
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=VARIED_TEXT),
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        _text_resp("All done — final answer after wandering."),
    ]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert len(handler.captured_requests) == 4
    assert str(result["turn_exit_reason"]).startswith("text_response")
    _assert_valid_transcript(result["messages"])


# ── detection OFF (interactive default): no intervention ─────────────────

def test_detection_off_by_default_no_intervention(agent_env):
    """Interactive sessions default the knob OFF — degenerate output is
    neither steered nor stopped."""
    make_agent, handler = agent_env
    agent = make_agent()
    assert agent._degeneration_detection is False
    handler.response_queue = [
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        _text_resp("Hello Marcin!"),
    ]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert len(handler.captured_requests) == 3
    assert str(result["turn_exit_reason"]).startswith("text_response")
    for req in handler.captured_requests:
        for m in _tool_messages(req):
            assert STEER_MARKER not in m["content"]


# ── config wiring ────────────────────────────────────────────────────────

def test_degeneration_detection_config_wiring(agent_env):
    """agent.degeneration_detection is read from config.yaml."""
    make_agent, _handler = agent_env
    agent = make_agent(config_yaml="agent:\n  degeneration_detection: true\n")
    assert agent._degeneration_detection is True
