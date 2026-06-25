"""Surface-agnostic core for the ``/subscription`` TUI screen.

Companion to :mod:`agent.billing_view` — same fail-open philosophy: when not
logged in or the portal is unreachable, return a struct with ``logged_in=False``
and let the surface degrade gracefully (never crash). Money is decimal end-to-end
(server emits decimal strings); we only format for display.

The TUI ``SubscriptionOverlay`` is **deep-link only** — it never charges
in-terminal. The manage URL is built locally on the TUI side from the
``portal_url`` and ``org_id`` fields in the subscription state.

WS1 dependency: ``GET /api/billing/subscription`` is a NAS endpoint (WS1 Phase A).
Until it ships, the fail-open contract handles 404s — the builder returns
``logged_in=False`` and the surface degrades gracefully.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from agent.billing_view import parse_money

logger = logging.getLogger(__name__)


# =============================================================================
# Parsed sub-structures
# =============================================================================


@dataclass(frozen=True)
class CurrentSubscription:
    """The user's active subscription. ``None`` (not this object) = no plan.

    When present, ``tier_id`` / ``tier_name`` / ``monthly_credits`` /
    ``cycle_ends_at`` are always set (NAS guarantees a present ``current`` is a
    fully-populated plan). Only ``credits_remaining`` and the cancel/downgrade
    fields are optional.
    """

    tier_id: Optional[str] = None
    tier_name: Optional[str] = None
    monthly_credits: Optional[Decimal] = None
    credits_remaining: Optional[Decimal] = None
    cycle_ends_at: Optional[str] = None  # ISO
    pending_downgrade_tier_name: Optional[str] = None
    pending_downgrade_at: Optional[str] = None  # ISO
    cancel_at_period_end: bool = False
    cancellation_effective_at: Optional[str] = None  # ISO


@dataclass(frozen=True)
class SubscriptionState:
    """Parsed ``GET /api/billing/subscription`` — the overview screen's data.

    Fail-open: ``logged_in=False`` (and empty fields) when not logged in or the
    portal is unreachable.
    """

    logged_in: bool
    org_name: Optional[str] = None
    org_id: Optional[str] = None  # org.id from the NAS response
    role: Optional[str] = None  # "OWNER" | "ADMIN" | "MEMBER"
    context: str = "personal"  # "personal" | "team"
    current: Optional[CurrentSubscription] = None
    portal_url: Optional[str] = None
    # When the fetch failed (vs cleanly not-logged-in), the message for the surface.
    error: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        """True for OWNER/ADMIN — the roles that can change plans."""
        return (self.role or "").upper() in ("OWNER", "ADMIN")

    @property
    def can_change_plan(self) -> bool:
        """True when the UI should offer plan-change actions (role gate from NAS)."""
        return self.is_admin


# =============================================================================
# Payload parsing
# =============================================================================


def _parse_current(raw: Any) -> Optional[CurrentSubscription]:
    # "No plan" is wire-represented as current:null (free personal OR team) —
    # the old all-null-object shape is gone. A present current is a real plan,
    # so guard on a real tier id and return None otherwise.
    if not isinstance(raw, dict):
        return None
    tier_id = raw.get("tierId") or raw.get("id")
    if not tier_id:
        return None
    return CurrentSubscription(
        tier_id=tier_id,
        tier_name=raw.get("tierName") or raw.get("name"),
        monthly_credits=parse_money(raw.get("monthlyCredits")),
        credits_remaining=parse_money(raw.get("creditsRemaining")),
        cycle_ends_at=raw.get("cycleEndsAt"),
        pending_downgrade_tier_name=raw.get("pendingDowngradeTierName"),
        pending_downgrade_at=raw.get("pendingDowngradeAt"),
        cancel_at_period_end=bool(raw.get("cancelAtPeriodEnd")),
        cancellation_effective_at=raw.get("cancellationEffectiveAt") or None,
    )


def subscription_state_from_payload(
    payload: dict[str, Any], *, portal_url: Optional[str] = None
) -> SubscriptionState:
    """Map a raw ``/api/billing/subscription`` JSON dict into :class:`SubscriptionState`."""
    raw_org = payload.get("org")
    org: dict[str, Any] = raw_org if isinstance(raw_org, dict) else {}

    raw_context = payload.get("context")
    context = raw_context if raw_context in ("personal", "team") else "personal"

    return SubscriptionState(
        logged_in=True,
        org_name=org.get("name"),
        org_id=org.get("id") or None,
        role=org.get("role"),
        context=context,
        current=_parse_current(payload.get("current")),
        portal_url=portal_url,
    )


# =============================================================================
# Fail-open builders (the surface front doors)
# =============================================================================


def build_subscription_state(*, timeout: float = 15.0) -> SubscriptionState:
    """Fetch + parse ``GET /api/billing/subscription``. Fail-open.

    Returns ``SubscriptionState(logged_in=False)`` when not logged in. On a
    portal/HTTP failure, returns ``logged_in=False`` with ``error`` set so the
    surface can show a clear message rather than crashing.

    Dev override: when ``HERMES_DEV_SUBSCRIPTION_FIXTURE`` names a fixture state,
    ``/subscription`` renders from that fixture instead of the real portal — so
    every plan/cancel/downgrade/team/not-admin state is testable on both
    the CLI and TUI without a live account. Throwaway scaffolding; see
    :func:`dev_fixture_subscription_state`.
    """
    fixture = dev_fixture_subscription_state()
    if fixture is not None:
        return fixture

    try:
        from hermes_cli.nous_billing import (
            BillingAuthError,
            BillingError,
            _absolutize_portal_url,
            get_subscription_state,
            resolve_portal_base_url,
        )
    except Exception:
        return SubscriptionState(logged_in=False, error="billing client unavailable")

    try:
        payload = get_subscription_state(timeout=timeout)
    except BillingAuthError:
        return SubscriptionState(logged_in=False)
    except BillingError as exc:
        logger.debug("subscription ▸ /state fetch failed (fail-open)", exc_info=True)
        return SubscriptionState(logged_in=False, error=str(exc))
    except Exception:
        logger.debug("subscription ▸ /state unexpected error (fail-open)", exc_info=True)
        return SubscriptionState(logged_in=False, error="could not load subscription state")

    raw_portal = payload.get("portalUrl") if isinstance(payload, dict) else None
    portal_url = _absolutize_portal_url(raw_portal) if raw_portal else None
    if not portal_url:
        try:
            portal_url = resolve_portal_base_url()
        except Exception:
            portal_url = None

    return subscription_state_from_payload(payload, portal_url=portal_url)


def subscription_manage_url(state: SubscriptionState) -> Optional[str]:
    """Build ``{portal_origin}/manage-subscription?org_id=<id>`` from a state.

    Mirrors the TUI's ``buildManageUrl`` (``subscription.ts``): the deep-link
    target is NAS's OWN ``/manage-subscription`` page (NOT the Stripe Billing
    Portal — decided Jun 23), which routes upgrade→Checkout / downgrade→scheduled
    internally. ``org_id`` pins the page to the right account in multi-org
    situations. Returns ``None`` when no portal URL is resolvable.
    """
    from urllib.parse import urlencode, urlsplit, urlunsplit

    if not state.portal_url:
        return None

    try:
        parts = urlsplit(state.portal_url)
    except Exception:
        return None

    if not parts.scheme or not parts.netloc:
        return None

    query = urlencode({"org_id": state.org_id}) if state.org_id else ""
    return urlunsplit((parts.scheme, parts.netloc, "/manage-subscription", query, ""))


# =============================================================================
# Dev fixtures (throwaway scaffolding — env-var driven, no live portal)
# =============================================================================

_DEV_FIXTURE_PORTAL = "https://portal.nousresearch.com/billing"


def _dev_current(**over: Any) -> CurrentSubscription:
    base: dict[str, Any] = dict(
        tier_id="plus",
        tier_name="Plus",
        monthly_credits=Decimal("1000"),
        credits_remaining=Decimal("420"),
        cycle_ends_at="2026-07-01",
    )
    base.update(over)
    return CurrentSubscription(**base)


def dev_fixture_subscription_state() -> Optional[SubscriptionState]:
    """Return a fixture :class:`SubscriptionState` for ``HERMES_DEV_SUBSCRIPTION_FIXTURE``.

    Lets every CLI/TUI subscription state be exercised without a live portal:

        free | mid | top | not-admin | downgrade | cancel | team |
        logged-out

    Returns ``None`` when the env var is unset/empty (the real portal path runs).
    Throwaway scaffolding — mirrors ``HERMES_DEV_CREDITS_FIXTURE``.
    """
    name = (os.getenv("HERMES_DEV_SUBSCRIPTION_FIXTURE") or "").strip().lower()
    if not name:
        return None

    common = dict(org_name="Acme Inc", org_id="org_acme", role="OWNER", portal_url=_DEV_FIXTURE_PORTAL)

    if name in ("logged-out", "logged_out", "loggedout"):
        return SubscriptionState(logged_in=False)
    if name == "free":
        return SubscriptionState(logged_in=True, current=None, **common)
    if name in ("mid", "mid-tier"):
        return SubscriptionState(logged_in=True, current=_dev_current(), **common)
    if name in ("top", "top-tier"):
        return SubscriptionState(
            logged_in=True,
            current=_dev_current(tier_id="ultra", tier_name="Ultra", monthly_credits=Decimal("7000"), credits_remaining=Decimal("5000")),
            **common,
        )
    if name in ("not-admin", "member"):
        return SubscriptionState(logged_in=True, current=_dev_current(), **{**common, "role": "MEMBER"})
    if name == "downgrade":
        return SubscriptionState(
            logged_in=True,
            current=_dev_current(tier_id="super", tier_name="Super", monthly_credits=Decimal("3000"), credits_remaining=Decimal("1500"), pending_downgrade_tier_name="Plus", pending_downgrade_at="2026-07-15"),
            **common,
        )
    if name == "cancel":
        return SubscriptionState(
            logged_in=True,
            current=_dev_current(cancel_at_period_end=True, cancellation_effective_at="2026-07-01"),
            **common,
        )
    if name == "team":
        return SubscriptionState(logged_in=True, context="team", current=None, org_name="Acme Engineering", org_id="org_eng", role="OWNER", portal_url=_DEV_FIXTURE_PORTAL)

    # Unknown name → behave as logged-out so the misconfiguration is visible.
    return SubscriptionState(logged_in=False, error=f"unknown HERMES_DEV_SUBSCRIPTION_FIXTURE: {name}")


