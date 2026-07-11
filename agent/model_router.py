"""Task-complexity model router — pick a model tier per task at conversation start.

Motivation: entry points that inherit one global default model treat a
one-line reminder and a multi-hour research job identically. On a heavyweight
(or heavily quantized local) default that wastes capacity on trivial tasks
and, worse, can send long agentic work to a backend that degenerates on it.
This module is the pure decision core: callers ask once, at conversation
start, which tier a task belongs to and which configured backend that maps
to. It never mutates a live conversation (per-conversation prompt caching
stays intact) and it exposes no tool surface.

Contract
--------
:func:`route_model` returns a :class:`RouteDecision`. ``model=None`` (with
``provider``/``base_url`` also ``None``) means **no-op**: keep whatever model
resolution the caller would have used anyway. Every disabled, override, and
failure path returns a no-op decision, so caller integration is uniformly
``if decision.model: use it``.

Precedence (highest first) — the router never overrides an explicit choice:
explicit per-job/per-call model > ``HERMES_MODEL`` env > session ``/model`` /
channel overrides > router > global default.

Decision pipeline
-----------------
Stage A — ordered deterministic heuristics, first decisive match, thresholds
from ``routing.heuristics``: explicit override → no-op; routing disabled or
origin not in ``routing.apply_to`` → no-op; heavy toolsets attached → heavy;
long text → heavy; heavy keywords / ≥2 URLs / multi-step lists → heavy;
short text or light keywords → light; else ambiguous. Heavy signals are
checked before light ones so a short "investigate the outage" one-liner is
not misrouted light purely on length. Attachments (and attached skills)
floor the result at ``standard``.

Stage B — only when ``routing.classifier: llm`` AND Stage A was ambiguous:
one short single-token classification call through the auxiliary client
(``auxiliary.routing`` config block, tight timeout). Any failure, timeout,
or unparseable output falls back to ``routing.default_tier``.

Safety: the whole body is fail-open (any exception → no-op + one WARNING),
and before a tier is returned its model must resolve via
:func:`_tier_model_available` — the read-only local model-catalog check that
is also the seam where a future host-capacity predicate slots in. An
unresolvable tier model falls through to the default tier with a reason.

Everything is config-gated and OFF by default (``routing.enabled: false``).
Structural precedent: :mod:`agent.image_routing`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ── public dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteContext:
    """Facts about the task known at conversation start."""

    origin: str                        # "cron" | "delegate" | "gateway" | "oneshot"
    toolsets: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    has_attachments: bool = False
    explicit_model: str | None = None  # set ⇒ user/job pinned a model; router must no-op


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of one routing decision.

    ``model is None`` (and then ``provider``/``base_url`` are ``None`` too)
    means no-op — keep the caller's existing model resolution.
    """

    tier: str                          # "light" | "standard" | "heavy"
    provider: str | None
    model: str | None
    base_url: str | None
    reason: str                        # human-readable, for the single INFO line
    source: str                        # "override" | "heuristic" | "llm" | "default" | "disabled" | "error"


# ── constants ────────────────────────────────────────────────────────────────

_TIER_ORDER: Dict[str, int] = {"light": 0, "standard": 1, "heavy": 2}
_VALID_CLASSIFIERS = frozenset({"heuristic", "llm", "off"})

_DEFAULT_APPLY_TO: Tuple[str, ...] = ("cron", "delegate")
_DEFAULT_LIGHT_MAX_CHARS = 280
_DEFAULT_HEAVY_MIN_CHARS = 4000
_DEFAULT_LIGHT_KEYWORDS: Tuple[str, ...] = (
    "remind", "greet", "ping", "notify", "send a message",
)
_DEFAULT_HEAVY_KEYWORDS: Tuple[str, ...] = (
    "research", "investigate", "comprehensive", "deep dive", "migrate",
    "refactor", "crawl",
)
_DEFAULT_HEAVY_TOOLSETS: Tuple[str, ...] = ("browser", "delegation")

_URL_RE = re.compile(r"https?://\S+")
# Numbered step lines ("1. do x" / "2) do y") — three or more reads as a
# multi-step work plan.
_STEP_LINE_RE = re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE)
_MIN_STEP_LINES = 3
_MIN_URLS = 2

# Only the first slice of the task text goes to the LLM classifier — enough
# signal for a tier call, small enough to stay fast and cheap.
_CLASSIFIER_SNIPPET_CHARS = 500
_CLASSIFIER_MAX_TOKENS = 8
_DEFAULT_CLASSIFIER_TIMEOUT = 8.0

_CLASSIFIER_SYSTEM_PROMPT = (
    "You classify the complexity of a task so the right model size can be "
    "chosen. Reply with exactly one word — light, standard, or heavy.\n"
    "light: trivial single-step tasks (reminders, greetings, one-line "
    "notifications).\n"
    "heavy: long-running or multi-step work (research, investigations, "
    "large refactors or migrations, web crawling).\n"
    "standard: everything else."
)


# ── strict config coercion helpers ───────────────────────────────────────────
# YAML users write ``enabled: "false"`` and ``apply_to: cron``; coerce
# defensively (mirroring agent/image_routing.py) instead of trusting truthiness.

_TRUE_TOKENS = frozenset({"true", "yes", "on", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "off", "0"})


def _coerce_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _TRUE_TOKENS:
            return True
        if s in _FALSE_TOKENS:
            return False
    return default


def _coerce_int(raw: Any, default: int) -> int:
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _coerce_float(raw: Any, default: float) -> float:
    if isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _coerce_str_tuple(raw: Any, default: Sequence[str]) -> Tuple[str, ...]:
    """Coerce a config value into a tuple of lowercase non-empty strings."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return tuple(default)
    out = []
    for item in raw:
        if isinstance(item, str):
            s = item.strip().lower()
            if s:
                out.append(s)
    return tuple(out) if out else tuple(default)


def _coerce_tier(raw: Any, default: str) -> str:
    if isinstance(raw, str) and raw.strip().lower() in _TIER_ORDER:
        return raw.strip().lower()
    return default


def _dict_at(mapping: Any, key: str) -> Dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


# ── tier backend resolution ──────────────────────────────────────────────────


def _tier_backend(tiers: Dict[str, Any], tier: str) -> Tuple[str, str, str]:
    """Return (provider, model, base_url) strings for a tier ('' = unset)."""
    entry = _dict_at(tiers, tier)
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    base_url = str(entry.get("base_url") or "").strip()
    return provider, model, base_url


def _tier_model_available(provider: str, model: str, base_url: str) -> bool:
    """Read-only check that a tier's model resolves in the local model catalog.

    This is the seam where a future host-capacity predicate (e.g. "is the
    local server able to load this model right now?") slots in. Today it
    consults the cached provider catalogs in :mod:`hermes_cli.models`.
    Unverifiable cases (custom base_url, unknown provider, empty catalog,
    lookup errors) are accepted — the check guards against config typos, it
    must not veto legitimately configured offline backends.
    """
    if base_url:
        return True  # user-pinned endpoint; no catalog to consult
    prov = (provider or "").strip().lower()
    if not prov or prov == "auto":
        return True
    try:
        from hermes_cli.models import cached_provider_model_ids, normalize_provider

        normalized = normalize_provider(prov) or prov
        catalog = cached_provider_model_ids(normalized)
        if not catalog:
            return True  # no catalog data — unverifiable, accept
        wanted = model.strip().lower()
        return any(wanted == entry.strip().lower() for entry in catalog)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("model_router: catalog lookup failed for %s:%s — %s", prov, model, exc)
        return True


def _decision_for_tier(
    tier: str,
    routing: Dict[str, Any],
    default_tier: str,
    source: str,
    reason: str,
) -> RouteDecision:
    """Map a chosen tier onto its configured backend, catalog-verified."""
    tiers = _dict_at(routing, "tiers")
    provider, model, base_url = _tier_backend(tiers, tier)
    if not model:
        return _noop(
            tier,
            f"{reason}; tier '{tier}' has no model configured — keeping existing resolution",
            source,
        )
    if _tier_model_available(provider, model, base_url):
        return RouteDecision(
            tier=tier,
            provider=provider or None,
            model=model,
            base_url=base_url or None,
            reason=reason,
            source=source,
        )
    miss = f"tier '{tier}' model '{model}' does not resolve in the local model catalog"
    if tier != default_tier:
        d_provider, d_model, d_base_url = _tier_backend(tiers, default_tier)
        if d_model and _tier_model_available(d_provider, d_model, d_base_url):
            return RouteDecision(
                tier=default_tier,
                provider=d_provider or None,
                model=d_model,
                base_url=d_base_url or None,
                reason=f"{miss}; falling through to default tier '{default_tier}'",
                source="default",
            )
    return _noop(default_tier, f"{miss}; keeping existing resolution", source)


# ── stage A: deterministic heuristics ────────────────────────────────────────


def _stage_a_classify(
    text: str,
    context: RouteContext,
    heuristics: Dict[str, Any],
) -> Tuple[Optional[str], str]:
    """Ordered heuristics; first decisive match wins. None = ambiguous."""
    heavy_toolsets = frozenset(
        _coerce_str_tuple(heuristics.get("heavy_toolsets"), _DEFAULT_HEAVY_TOOLSETS)
    )
    attached = {str(t).strip().lower() for t in (context.toolsets or ()) if str(t).strip()}
    heavy_hits = sorted(attached & heavy_toolsets)
    if heavy_hits:
        return "heavy", f"heavy toolset(s) attached: {', '.join(heavy_hits)}"

    length = len(text)
    heavy_min = _coerce_int(heuristics.get("heavy_min_chars"), _DEFAULT_HEAVY_MIN_CHARS)
    if length >= heavy_min:
        return "heavy", f"task text length {length} >= heavy_min_chars {heavy_min}"

    lowered = text.lower()
    for keyword in _coerce_str_tuple(heuristics.get("heavy_keywords"), _DEFAULT_HEAVY_KEYWORDS):
        if keyword in lowered:
            return "heavy", f"heavy keyword match: '{keyword}'"

    url_count = len(_URL_RE.findall(text))
    if url_count >= _MIN_URLS:
        return "heavy", f"{url_count} URLs referenced"
    step_count = len(_STEP_LINE_RE.findall(text))
    if step_count >= _MIN_STEP_LINES:
        return "heavy", f"multi-step list detected ({step_count} numbered steps)"

    light_max = _coerce_int(heuristics.get("light_max_chars"), _DEFAULT_LIGHT_MAX_CHARS)
    if 0 < length <= light_max:
        return "light", f"task text length {length} <= light_max_chars {light_max}"
    for keyword in _coerce_str_tuple(heuristics.get("light_keywords"), _DEFAULT_LIGHT_KEYWORDS):
        if keyword in lowered:
            return "light", f"light keyword match: '{keyword}'"

    return None, "no decisive heuristic"


def _floor_for(context: RouteContext) -> str:
    """Attachments or attached skills floor the tier at standard."""
    if context.has_attachments:
        return "standard"
    if context.skills:
        return "standard"
    return "light"


# ── stage B: optional LLM classification ─────────────────────────────────────


def _invoke_classifier_llm(*, messages: list, timeout: float) -> Any:
    """One auxiliary-client call for the routing classifier (test seam)."""
    from agent.auxiliary_client import call_llm

    return call_llm(
        task="routing",
        messages=messages,
        max_tokens=_CLASSIFIER_MAX_TOKENS,
        temperature=0.0,
        timeout=timeout,
    )


def _stage_b_llm_classify(text: str, config: Dict[str, Any]) -> Optional[str]:
    """Return a tier from the LLM classifier, or None on any failure.

    Strict single-token parse: the reply must be exactly ``light``,
    ``standard``, or ``heavy`` (after whitespace strip). Anything else —
    including timeouts and transport errors — returns None so the caller
    falls back to ``routing.default_tier``.
    """
    try:
        aux = _dict_at(_dict_at(config, "auxiliary"), "routing")
        timeout = _coerce_float(aux.get("timeout"), _DEFAULT_CLASSIFIER_TIMEOUT)
        snippet = text[:_CLASSIFIER_SNIPPET_CHARS]
        response = _invoke_classifier_llm(
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": snippet},
            ],
            timeout=timeout,
        )
        content = str(response.choices[0].message.content or "")
        token = content.strip().lower()
        if token in _TIER_ORDER:
            return token
        logger.debug("model_router: llm classifier returned unparseable output %r", content[:80])
        return None
    except Exception as exc:
        logger.debug(
            "model_router: llm classifier failed (%s: %s); falling back to default tier",
            type(exc).__name__, exc,
        )
        return None


# ── core decision ────────────────────────────────────────────────────────────


def _noop(tier: str, reason: str, source: str) -> RouteDecision:
    return RouteDecision(
        tier=tier, provider=None, model=None, base_url=None, reason=reason, source=source
    )


def _route(
    task_text: str,
    context: RouteContext,
    config: Dict[str, Any],
) -> Tuple[RouteDecision, bool]:
    """Compute the decision. Returns (decision, routing_enabled)."""
    cfg = config if isinstance(config, dict) else {}
    routing = _dict_at(cfg, "routing")
    default_tier = _coerce_tier(routing.get("default_tier"), "standard")
    enabled = _coerce_bool(routing.get("enabled"), False)

    # Ordered gates — explicit override always wins over the router.
    if context.explicit_model:
        return _noop(default_tier, "explicit model override present", "override"), enabled
    if not enabled:
        return _noop(default_tier, "routing disabled", "disabled"), False
    apply_to = _coerce_str_tuple(routing.get("apply_to"), _DEFAULT_APPLY_TO)
    origin = str(context.origin or "").strip().lower()
    if origin not in apply_to:
        return _noop(default_tier, f"origin '{origin}' not in routing.apply_to", "disabled"), True

    classifier = str(routing.get("classifier") or "").strip().lower()
    if classifier not in _VALID_CLASSIFIERS:
        classifier = "heuristic"
    text = task_text if isinstance(task_text, str) else ""

    if classifier == "off":
        tier: Optional[str] = default_tier
        source = "default"
        reason = "classifier off; using default tier"
    else:
        tier, reason = _stage_a_classify(text, context, _dict_at(routing, "heuristics"))
        source = "heuristic"
        if tier is None and classifier == "llm":
            llm_tier = _stage_b_llm_classify(text, cfg)
            if llm_tier is not None:
                tier, source, reason = llm_tier, "llm", "llm classifier verdict"
        if tier is None:
            tier = default_tier
            source = "default"
            reason = f"{reason}; using default tier"

    floor = _floor_for(context)
    if _TIER_ORDER[floor] > _TIER_ORDER[tier]:
        reason = f"{reason}; floored at '{floor}' (attachments/skills present)"
        tier = floor

    return _decision_for_tier(tier, routing, default_tier, source, reason), True


def route_model(
    task_text: str,
    *,
    context: RouteContext,
    config: dict,
) -> RouteDecision:
    """Decide which model tier (and configured backend) a task should use.

    Pure and fail-open: reads only the passed config (plus cached model
    catalogs), never raises, and returns a no-op decision (``model=None``)
    whenever routing is disabled, overridden, inapplicable, or broken.
    Emits exactly one INFO line per decision while routing is enabled.
    """
    try:
        decision, log_enabled = _route(task_text, context, config)
        if log_enabled:
            logger.info(
                "model_router: origin=%s tier=%s model=%s source=%s reason=%s",
                (context.origin or "").strip().lower() if isinstance(context.origin, str) else context.origin,
                decision.tier,
                decision.model or "(no-op)",
                decision.source,
                decision.reason,
            )
        return decision
    except Exception as exc:
        logger.warning(
            "model_router: routing failed open (%s: %s); keeping existing model resolution",
            type(exc).__name__, exc,
        )
        return _noop("standard", f"router error: {exc}", "error")


__all__ = [
    "RouteContext",
    "RouteDecision",
    "route_model",
]
