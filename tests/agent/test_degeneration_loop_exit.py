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

    # The exit must not be a silent empty turn: the turn-completion
    # explainer replaces the blank response with a message naming the
    # detection, so interactive users see WHY it stopped and cron gets a
    # meaningful failure string instead of "agent reported failure".
    _resp = (result["final_response"] or "")
    assert _resp.strip(), "second-strike exit produced a silent empty turn"
    assert "degeneration" in _resp.lower()
    assert result["completed"] is False

    # The steering note rode the newest tool result of request 2.
    steer_req_tool_msgs = _tool_messages(handler.captured_requests[1])
    assert steer_req_tool_msgs, "steered request lost the tool result"
    assert STEER_MARKER in steer_req_tool_msgs[-1]["content"]
    assert "repeat" in steer_req_tool_msgs[-1]["content"].lower()
    # ...and only there — request 1 predates any detection.
    for m in _tool_messages(handler.captured_requests[0]):
        assert STEER_MARKER not in m["content"]

    # Transcript: the loop broke at the top-of-loop check on a complete
    # assistant+tool turn (same terminal shape as output_budget_exhausted);
    # the explainer rides result["final_response"], not the transcript.
    # Strict alternation holds and no user/system message was injected
    # mid-loop.
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


# ── mid-turn ``messages`` rebind (compression / rollback) robustness ────

def test_compression_rebind_does_not_double_count_scored_strike(agent_env, monkeypatch):
    """A mid-turn context-compression rebind of ``messages`` must not
    double-count an already-scored strike into a spurious second strike.

    Simulates the rebind at the REAL seam the production loop uses —
    ``agent.context_compressor.should_compress()`` gating
    ``agent._compress_context()`` in the pre-API pressure check
    (agent/conversation_loop.py) — rather than forcing a real compression
    (which would need a huge synthetic transcript + a real/aux LLM
    summarizer call). ``should_compress`` is forced True for exactly the
    pre-API check that runs right after strike 1 is registered; the stand-in
    ``_compress_context`` returns a brand-new list object (as real
    compression always does) with an extra spliced-in turn near the head,
    so every tail index shifts — reproducing the positional-index mismatch
    that trips the pre-fix bug — while leaving the just-scored assistant+
    tool tail content untouched, mirroring how real compression's
    ``_prune_old_tool_results`` still emits fresh dict copies for
    unchanged/protected tail messages.

    Pre-fix (positional-index tracking), this rebind makes the top-of-loop
    check re-score the SAME strike-1 message as if it were new, producing a
    spurious strike 2 and a `degeneration_detected` exit that starves the
    model of its steered recovery call. Post-fix, the already-scored
    message is recognized (via its "_degen_scored" marker, which survives
    the copy) and is not rescored, so the model gets its real second call.
    """
    make_agent, handler = agent_env
    agent = make_agent()
    agent._degeneration_detection = True
    handler.response_queue = [
        # Call 1: degenerate -> strike 1 (steered).
        _tc_resp("read_file", '{"file_path": "/nonexistent_hermes_degen"}', content=DEGENERATE_TEXT),
        # Call 2: the steered recovery call. If the rebind bug re-scores
        # strike 1 as a spurious strike 2, this response is never fetched.
        _text_resp("Hello Marcin! (final answer, delivered once)"),
    ]

    should_compress_calls = {"n": 0}

    def _fake_should_compress(_tokens=None):
        should_compress_calls["n"] += 1
        # Invocation #1: the pre-API check ahead of call 1 — don't compress
        # yet (call 1 must go out for real to produce strike 1).
        # Invocation #2: the pre-API check ahead of call 2, AFTER strike 1
        # was scored by the top-of-loop check for this iteration — force
        # the rebind here, exactly between strike-1 scoring and call 2.
        return should_compress_calls["n"] == 2

    compress_calls = {"n": 0}

    def _fake_compress_context(compress_messages, system_message, **kwargs):
        compress_calls["n"] += 1
        # Real compression rewrites/shrinks the head and returns a NEW list
        # object while the live tail survives (content-wise) — splice a
        # placeholder "compacted" exchange right after the system message so
        # every tail index shifts, then return copies of the rest so no
        # message keeps its old object identity either (matching
        # ContextCompressor._prune_old_tool_results' unconditional
        # ``[m.copy() for m in messages]``).
        new_messages = (
            compress_messages[:1]
            + [
                {"role": "user", "content": "[compacted summary placeholder]"},
                {"role": "assistant", "content": "Acknowledged."},
            ]
            + [dict(m) for m in compress_messages[1:]]
        )
        return new_messages, system_message

    monkeypatch.setattr(agent.context_compressor, "should_compress", _fake_should_compress)
    monkeypatch.setattr(agent, "_compress_context", _fake_compress_context)

    result = agent.run_conversation("hello", conversation_history=[], task_id="t")

    # The compression seam actually fired once — this exercises the real
    # rebind path, not a bypassed/mocked-out one.
    assert compress_calls["n"] == 1

    # The turn must NOT have exited on a spurious second strike: the model
    # got its real steered recovery call (call 2) and the turn completed
    # normally.
    assert len(handler.captured_requests) == 2
    assert result["turn_exit_reason"] != "degeneration_detected"
    assert str(result["turn_exit_reason"]).startswith("text_response")
    assert "final answer" in (result["final_response"] or "")

    # The recovery request carried the steering note from strike 1 — proof
    # the strike WAS registered (not silently dropped by a naive rebind
    # reset), it just wasn't double-counted.
    tool_msgs = _tool_messages(handler.captured_requests[1])
    assert tool_msgs and STEER_MARKER in tool_msgs[-1]["content"]

    # The internal scoring marker never leaked onto the wire.
    for req in handler.captured_requests:
        for m in req["messages"]:
            assert "_degen_scored" not in m

    _assert_valid_transcript(result["messages"])


# ── config wiring ────────────────────────────────────────────────────────

def test_degeneration_detection_config_wiring(agent_env):
    """agent.degeneration_detection is read from config.yaml."""
    make_agent, _handler = agent_env
    agent = make_agent(config_yaml="agent:\n  degeneration_detection: true\n")
    assert agent._degeneration_detection is True
