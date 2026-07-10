"""Session output-token budget + default per-call max_tokens.

Nothing used to bound the VOLUME of model output a session could generate:
a trivial cron job once produced 261,333 bytes of looping self-deliberation
for a two-word answer. Two independent caps close that hole:

1. ``agent.session_output_token_budget`` (0 = unlimited): checked at the
   top of the conversation loop, before any message is appended, so the
   transcript always ends on a complete assistant(+tool) turn. On the
   first trip the model gets ONE grace call (the pre-existing
   ``_budget_grace_call`` mechanism) to produce a final summary; after
   that the turn ends with exit reason ``output_budget_exhausted``.
2. ``agent.default_max_tokens`` (0 = provider default): fills
   ``agent.max_tokens`` when nothing else set it, flowing through the
   existing ``_max_tokens_param`` plumbing as a FIXED per-call cap.

Exercised end-to-end through ``AIAgent.run_conversation`` against an
in-process mock provider (same harness style as
``test_conversation_breaker.py``), extended to carry token usage on the
final stream chunk so ``session_output_tokens`` accumulates realistically.
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


def _tc_resp(name: str, args: str = "{}", output_tokens: int = 0) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": name, "arguments": args}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": output_tokens,
                  "total_tokens": 10 + output_tokens},
    }


def _text_resp(text: str, output_tokens: int = 0) -> dict:
    return {
        "id": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": output_tokens,
                  "total_tokens": 10 + output_tokens},
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
            # Final chunk carries usage — the loop reads usage off any chunk
            # that has it, mirroring OpenAI's stream_options include_usage.
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
    """Mock provider + isolated HERMES_HOME; yields (make_agent, handler).

    ``make_agent(config_yaml=..., **kwargs)`` optionally writes a
    config.yaml into the isolated HERMES_HOME BEFORE constructing the
    agent, so config-driven knobs (agent.default_max_tokens,
    agent.session_output_token_budget) are exercised through the real
    config-loading path.
    """
    _MockHandler.captured_requests = []
    _MockHandler.response_queue = []
    _MockHandler.default_response = {}
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    test_home = tempfile.mkdtemp(prefix="hermes_outbudget_")
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
    tool results (parallel tool batches), and every tool message belongs to
    a preceding assistant tool_calls message (directly or via another tool
    result)."""
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


# ── (a) budget enforcement + grace call ──────────────────────────────────

def test_output_budget_grace_call_yields_final_summary(agent_env):
    """Blowing the budget grants ONE grace call; when the model answers it
    with text, the turn ends normally on that assistant summary."""
    make_agent, handler = agent_env
    agent = make_agent()
    agent._session_output_token_budget = 100
    handler.response_queue = [
        _tc_resp("frobnicate_xyz", output_tokens=150),   # trips the budget
        _text_resp("BUDGET SUMMARY OK", output_tokens=20),
    ]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert len(handler.captured_requests) == 2
    assert "BUDGET SUMMARY OK" in (result["final_response"] or "")
    assert str(result["turn_exit_reason"]).startswith("text_response")
    # The grace call must be told to wrap up — via the newest tool result
    # (alternation- and cache-safe: that message is new this turn).
    grace_req_msgs = handler.captured_requests[1]["messages"]
    tool_msgs = [m for m in grace_req_msgs if m.get("role") == "tool"]
    assert tool_msgs, "grace request lost the tool result"
    assert "output-token budget" in tool_msgs[-1]["content"]
    # Transcript shape: ends on the assistant summary, strict alternation.
    msgs = result["messages"]
    assert msgs[-1]["role"] == "assistant"
    _assert_valid_transcript(msgs)


def test_output_budget_exhausted_after_stubborn_grace_call(agent_env):
    """If the grace call still tool-calls, the loop exits at the next
    top-of-loop check with output_budget_exhausted — before any new message
    is appended, so the transcript ends on a complete assistant+tool turn."""
    make_agent, handler = agent_env
    agent = make_agent()
    agent._session_output_token_budget = 100
    handler.default_response = _tc_resp("frobnicate_xyz", output_tokens=150)

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    # Call 1 trips the budget; call 2 is the grace call; then hard exit.
    assert len(handler.captured_requests) == 2
    assert result["turn_exit_reason"] == "output_budget_exhausted"
    msgs = result["messages"]
    assert msgs[-1]["role"] == "tool"          # complete assistant+tool turn
    _assert_valid_transcript(msgs)


def test_output_budget_zero_is_unlimited(agent_env):
    """Budget 0 (the default) never bounds the loop — regression guard."""
    make_agent, handler = agent_env
    agent = make_agent()
    assert agent._session_output_token_budget == 0
    handler.response_queue = [
        _tc_resp("frobnicate_xyz", output_tokens=5000),
        _tc_resp("frobnicate_xyz", output_tokens=5000),
        _text_resp("ALL DONE", output_tokens=5000),
    ]

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    assert len(handler.captured_requests) == 3
    assert "ALL DONE" in (result["final_response"] or "")
    assert str(result["turn_exit_reason"]).startswith("text_response")


def test_output_budget_config_wiring(agent_env):
    """agent.session_output_token_budget is read from config.yaml."""
    make_agent, _handler = agent_env
    agent = make_agent(config_yaml="agent:\n  session_output_token_budget: 12345\n")
    assert agent._session_output_token_budget == 12345


# ── (c) default per-call max_tokens ──────────────────────────────────────

def test_default_max_tokens_config_caps_outgoing_request(agent_env):
    """agent.default_max_tokens > 0 with no explicit max_tokens → the
    outgoing payload carries the fixed cap."""
    make_agent, handler = agent_env
    agent = make_agent(config_yaml="agent:\n  default_max_tokens: 1234\n")
    handler.response_queue = [_text_resp("ok", output_tokens=5)]

    agent.run_conversation("hello", conversation_history=[], task_id="t")

    req = handler.captured_requests[0]
    assert req.get("max_tokens") == 1234 or req.get("max_completion_tokens") == 1234


def test_explicit_max_tokens_beats_default(agent_env):
    """An explicitly set agent.max_tokens wins over the config default."""
    make_agent, handler = agent_env
    agent = make_agent(
        config_yaml="agent:\n  default_max_tokens: 1234\n",
        max_tokens=777,
    )
    handler.response_queue = [_text_resp("ok", output_tokens=5)]

    agent.run_conversation("hello", conversation_history=[], task_id="t")

    req = handler.captured_requests[0]
    assert req.get("max_tokens") == 777 or req.get("max_completion_tokens") == 777


def test_default_max_tokens_zero_adds_no_cap(agent_env):
    """default_max_tokens 0 (the default) preserves current behavior: no
    output-cap key on the outgoing request."""
    make_agent, handler = agent_env
    agent = make_agent()
    assert agent.max_tokens is None
    handler.response_queue = [_text_resp("ok", output_tokens=5)]

    agent.run_conversation("hello", conversation_history=[], task_id="t")

    req = handler.captured_requests[0]
    assert "max_tokens" not in req
    assert "max_completion_tokens" not in req
