"""Injection-scan gate for background-review (curator) memory/skill writes.

OWASP LLM05 (memory poisoning): the background review fork writes summaries
and skill edits that later re-enter the system prompt as memory/skill
content. Unlike interactive writes, there is no user in the loop to notice a
poisoned entry before it lands. This module gates ONLY curator-originated
writes -- interactive/user-directed writes are untouched here; any
pre-existing scanning on those paths (e.g. ``tools/memory_tool.py``'s
unconditional injection scan) is unaffected.

Reuses the same pattern library used for memory writes and skill installs
(``tools/threat_patterns.py``, ``scope="strict"``) rather than a new,
possibly-drifting pattern list. On a hit: the write is dropped, one WARNING
names the matched pattern, and the denial is recorded against the same
per-thread breaker T17 introduced for repeated denied privileged attempts
(``hermes_cli.plugins.record_thread_tool_denial``) -- a poisoning attempt is
itself a denied privileged action.

False-positive trade-off: curator summaries are model-generated prose about
a conversation, and may legitimately QUOTE suspicious text (e.g.
paraphrasing a prompt-injection attempt a user reported). This scan cannot
distinguish an authored attack from an accurately quoted one, so it will
occasionally drop a benign summary that quotes flagged phrasing. That is
accepted: the curator can rephrase without quoting verbatim, and the
higher-severity failure mode is an unscanned write actually poisoning
memory/skills (OWASP LLM05), not an occasional dropped summary.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def scan_curator_write(content: str, label: str) -> Optional[Dict[str, Any]]:
    """Scan a background-review write for injection before it is persisted.

    No-op (returns ``None``) outside the background review fork, or when the
    content is clean -- the caller should proceed with its normal write path
    in both cases. Returns a ``{"success": False, "error": ...}`` dict (the
    shape both ``tools/memory_tool.py`` and ``tools/skill_manager_tool.py``
    already return from their internal write helpers) when the write must be
    dropped.

    ``label`` identifies the write for the WARNING log and the denial
    breaker, e.g. ``"memory:add"``, ``"skill_manage:write_file"`` --
    consistent with the ``skill_manage:{action}`` convention
    ``_deny_background_review_write`` already uses.
    """
    try:
        from tools.skill_provenance import is_background_review
        if not is_background_review():
            return None
    except Exception:
        return None

    try:
        from tools.threat_patterns import scan_for_threats
        findings = scan_for_threats(content or "", scope="strict")
    except Exception:
        logger.debug("curator write scan failed for %s", label, exc_info=True)
        return None

    if not findings:
        return None

    pattern_id = findings[0]
    logger.warning(
        "Dropped curator write (%s): content matched threat pattern '%s'",
        label, pattern_id,
    )

    result: Dict[str, Any] = {
        "success": False,
        "error": (
            f"Refusing curator write ({label}): content matches threat "
            f"pattern '{pattern_id}'. Background review writes must not "
            f"contain injection or exfiltration payloads -- rephrase "
            f"without quoting the flagged text verbatim."
        ),
    }
    try:
        from hermes_cli.plugins import record_thread_tool_denial
        abort_message = record_thread_tool_denial(label)
        if abort_message:
            result["error"] = abort_message
    except Exception:
        logger.debug("denial-breaker recording failed for %s", label, exc_info=True)
    return result


__all__ = ["scan_curator_write"]
