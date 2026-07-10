"""Minimal operator-alert helper.

Incident: security warnings and job failures went only to log files nobody
watched overnight (``errors.log`` / ``hermes logs``). This module is the
alerting primitive later work (security-warning routing, cron failure
alerts, ...) is meant to route through — a thin, fire-and-forget wrapper
over the delivery machinery cron already has (``cron/scheduler.py``'s
delivery-target resolver + ``tools/send_message_tool._send_to_platform``).
It intentionally does NOT add a new platform integration, a delivery queue,
retries, or async — kept intentionally minimal; see the rest of this
module docstring for why.

Consumer contract (READ BEFORE CHANGING THE SIGNATURE): the existing guarded
caller, ``cron/scheduler.py::_send_reconcile_alert``, does::

    from hermes_cli.operator_alerts import send_operator_alert
    send_operator_alert(message)

i.e. it calls this with a SINGLE positional string. ``body`` therefore has a
default so that call keeps working — giving it a mandatory second parameter
would make every call from that guarded-import site raise ``TypeError``,
which the caller's ``except BaseException`` would swallow into an
"alert delivery failed" log line, silently defeating the point of this
module ever landing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Alias so tests can monkeypatch the clock without touching the process-wide
# `time` module (`monkeypatch.setattr(operator_alerts, "_monotonic", fake)`).
_monotonic = time.monotonic

# Rate-limit identical alert titles to once per 15 minutes so an alert storm
# (e.g. a flapping check firing every tick) cannot recreate the log-noise
# problem this module exists to solve. Module-level + monotonic clock:
# process-lifetime state, immune to wall-clock adjustments; intentionally not
# persisted (a process restart resets the window, which is fine — a fresh
# process restarting is itself alert-worthy context, not noise).
_RATE_LIMIT_SECONDS = 15 * 60
_last_sent_at: Dict[str, float] = {}
# Guards the check-then-act on _last_sent_at: without it, two threads raising
# the same title concurrently (e.g. parallel cron jobs failing the same way)
# could both pass the rate-limit check and double-send. Held ONLY across the
# bookkeeping (get + set) — never across the dispatch, so a slow platform
# call cannot serialize other alerts behind it.
_rate_limit_lock = threading.Lock()


def send_operator_alert(title: str, body: str = "", *, severity: str = "warning") -> bool:
    """Send a one-line operator alert over the configured delivery target.

    Fire-and-forget: NEVER raises. Returns ``True`` only when a delivery
    attempt was made and reported no error. Returns ``False`` when
    unconfigured (``alerts.deliver`` unset), rate-limited (same ``title``
    sent within the last 15 minutes — the suppressed case; there is no
    separate tri-state, "suppressed" is just one of the ways this can fail
    to deliver right now), or delivery itself failed. Callers that only care
    about "did this break my caller" get that from the fact that this
    function never raises; callers that care about "was it actually
    delivered" get that from the return value.
    """
    try:
        return _send_operator_alert_impl(title, body, severity=severity)
    except BaseException as e:
        # Defense in depth: _send_operator_alert_impl already wraps its own
        # delivery attempt, but this module's entire reason to exist is to
        # never let an alert-routing failure escape into the caller (cron's
        # reconciliation loop, security-warning paths, etc.) — so the outer
        # boundary catches BaseException too, matching the caller-side guard
        # in cron/scheduler.py::_send_reconcile_alert.
        logger.warning("operator alert delivery failed unexpectedly: %s", e)
        return False


def _send_operator_alert_impl(title: str, body: str, *, severity: str) -> bool:
    # Read-only access to one key — skip load_config()'s defensive deepcopy
    # (this path may run inside the cron reconciliation loop at startup).
    from hermes_cli.config import load_config_readonly

    try:
        cfg = load_config_readonly() or {}
    except Exception as e:
        logger.warning("operator alert skipped: failed to load config: %s", e)
        return False

    deliver_value = str((cfg.get("alerts") or {}).get("deliver") or "").strip()
    if not deliver_value or deliver_value == "local":
        return False  # unconfigured: silent no-op by design (default state)

    # Clock read stays OUTSIDE the lock (a marginally stale value only
    # shortens the window by microseconds); the check-then-act on the
    # suppression dict is atomic under the lock. Only the bookkeeping is
    # guarded — dispatch happens after release, so a slow platform send
    # never serializes concurrent alerts with different titles.
    now = _monotonic()
    with _rate_limit_lock:
        last = _last_sent_at.get(title)
        if last is not None and (now - last) < _RATE_LIMIT_SECONDS:
            logger.debug("operator alert suppressed (rate-limited within 15 min): %s", title)
            return False
        # Record the attempt before dispatch (not gated on success) so a
        # persistently-failing delivery channel can't be hammered every call —
        # the same alert storm this rate limit exists to stop would otherwise
        # just turn into a storm of failed delivery attempts instead.
        _last_sent_at[title] = now

    message = f"⚠️ [{severity}] {title}"
    if body:
        message += f"\n{body}"

    target = _resolve_target(deliver_value)
    if not target:
        return False

    return _dispatch(target, message)


def _resolve_target(deliver_value: str) -> Optional[dict]:
    """Resolve ``alerts.deliver`` to a concrete ``{platform, chat_id, thread_id}``.

    Reuses cron's own delivery-target resolver (``cron/scheduler.py``) so
    ``alerts.deliver`` accepts exactly the same ``platform:chat_id`` syntax
    (including human-friendly channel-name resolution) that cron's own
    ``deliver`` field does — one syntax to document, not two. Imported
    lazily: ``cron.scheduler`` guardedly imports THIS module from inside a
    function body precisely to avoid a module-load cycle, so this module
    must return the favor and never import ``cron.scheduler`` at module
    scope either.
    """
    from cron.scheduler import _resolve_single_delivery_target

    # No cron job exists for an operator alert — a job-shaped dict is only
    # consulted by the resolver for "origin" / "all" style values, which
    # `alerts.deliver` never uses (it is always a raw "platform:chat_id").
    target = _resolve_single_delivery_target({}, deliver_value)
    if not target:
        logger.warning(
            "operator alert skipped: could not resolve delivery target %r "
            "(check alerts.deliver in config.yaml)",
            deliver_value,
        )
    return target


def _dispatch(target: dict, message: str) -> bool:
    """Send ``message`` to a resolved target via the same standalone send
    path cron's own ``_deliver_result`` falls back to
    (``tools/send_message_tool._send_to_platform``), run synchronously via
    ``asyncio.run`` since this helper has no event loop of its own and (per
    the task's non-goals) does not add one, a queue, or retries.

    ``_deliver_result`` itself is not reused directly: it is job-shaped
    (response wrapping, thread-continuation, session mirroring, live-adapter
    preference — none of which apply to a one-line operator alert) and
    requires a ``job`` dict throughout, not just at the resolver boundary.
    """
    import asyncio

    from gateway.config import Platform, load_gateway_config
    from tools.send_message_tool import _send_to_platform

    platform_name = target["platform"]
    try:
        platform = Platform(platform_name.lower())
    except (ValueError, KeyError):
        logger.warning("operator alert skipped: unknown platform %r", platform_name)
        return False

    try:
        gw_config = load_gateway_config()
    except Exception as e:
        logger.warning("operator alert skipped: failed to load gateway config: %s", e)
        return False

    pconfig = gw_config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        logger.warning(
            "operator alert skipped: platform %r not configured/enabled", platform_name,
        )
        return False

    coro = _send_to_platform(
        platform, pconfig, target["chat_id"], message, thread_id=target.get("thread_id"),
    )
    try:
        result = asyncio.run(coro)
    except RuntimeError as e:
        # asyncio.run() refuses to run inside a thread that already has a
        # running event loop. No queueing/retry per the task's non-goals —
        # log and report failure rather than risk a deadlock.
        coro.close()
        logger.warning("operator alert delivery failed: %s", e)
        return False
    except Exception as e:
        logger.warning("operator alert delivery failed: %s", e)
        return False

    if isinstance(result, dict) and result.get("error"):
        logger.warning("operator alert delivery failed: %s", result["error"])
        return False

    return True
