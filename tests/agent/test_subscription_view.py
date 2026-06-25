"""Tests for agent.subscription_view — the surface-agnostic /subscription core.

Behavior contracts (not change-detectors): the manage-URL builder's shape, the
payload parser's field mapping + fail-open posture, and the dev-fixture states
that drive the CLI/TUI without a live portal.
"""

from decimal import Decimal

import pytest

from agent.subscription_view import (
    SubscriptionState,
    build_subscription_state,
    dev_fixture_subscription_state,
    subscription_manage_url,
    subscription_state_from_payload,
)


# ── subscription_manage_url ──────────────────────────────────────────


def test_manage_url_attaches_org_and_path_to_portal_origin():
    s = SubscriptionState(
        logged_in=True,
        org_id="org_x",
        portal_url="https://portal.nousresearch.com/billing/whatever",
    )
    # Path is replaced with /manage-subscription; org_id is pinned; origin kept.
    assert (
        subscription_manage_url(s)
        == "https://portal.nousresearch.com/manage-subscription?org_id=org_x"
    )


def test_manage_url_omits_org_when_absent():
    s = SubscriptionState(logged_in=True, org_id=None, portal_url="https://p.example.com/")
    url = subscription_manage_url(s)
    assert url == "https://p.example.com/manage-subscription"
    assert "org_id" not in url


def test_manage_url_none_without_portal():
    assert subscription_manage_url(SubscriptionState(logged_in=True, portal_url=None)) is None


def test_manage_url_none_for_garbage_portal():
    # No scheme/netloc → can't build a deep-link; fail closed (None), not crash.
    assert subscription_manage_url(SubscriptionState(logged_in=True, portal_url="not a url")) is None


# ── payload parser ───────────────────────────────────────────────────


def test_parser_maps_camelCase_payload_fields():
    payload = {
        "org": {"name": "Acme", "id": "org_1", "role": "ADMIN"},
        "context": "personal",
        "current": {
            "tierId": "plus",
            "tierName": "Plus",
            "monthlyCredits": "1000",
            "creditsRemaining": "420",
            "cycleEndsAt": "2026-07-01",
            "cancelAtPeriodEnd": True,
            "cancellationEffectiveAt": "2026-07-01",
        },
    }
    s = subscription_state_from_payload(payload, portal_url="https://p/billing")

    assert s.logged_in is True
    assert s.org_name == "Acme" and s.org_id == "org_1"
    assert s.is_admin is True and s.can_change_plan is True
    assert s.current is not None
    assert s.current.tier_name == "Plus"
    assert s.current.cancel_at_period_end is True
    assert s.current.monthly_credits == Decimal("1000")


def test_parser_no_plan_is_none_not_all_null_object():
    # "No plan" is current:null on the wire; a current-shaped dict with no
    # tierId must parse to None (not an all-null CurrentSubscription).
    s = subscription_state_from_payload({"current": {"tierId": None}}, portal_url=None)
    assert s.current is None


def test_parser_member_role_cannot_change_plan():
    s = subscription_state_from_payload({"org": {"role": "MEMBER"}}, portal_url=None)
    assert s.is_admin is False
    assert s.can_change_plan is False


def test_parser_defaults_unknown_context_to_personal():
    s = subscription_state_from_payload({"context": "wat"}, portal_url=None)
    assert s.context == "personal"


# ── dev fixtures (env-driven, no live portal) ────────────────────────


def test_no_fixture_when_env_unset(monkeypatch):
    monkeypatch.delenv("HERMES_DEV_SUBSCRIPTION_FIXTURE", raising=False)
    assert dev_fixture_subscription_state() is None


@pytest.mark.parametrize(
    "name,checker",
    [
        ("free", lambda s: s.logged_in and s.current is None),
        ("mid", lambda s: s.current and s.current.tier_id == "plus"),
        ("top", lambda s: s.current and s.current.tier_id == "ultra"),
        ("not-admin", lambda s: s.role == "MEMBER" and not s.can_change_plan),
        ("downgrade", lambda s: s.current and s.current.pending_downgrade_tier_name == "Plus"),
        ("cancel", lambda s: s.current and s.current.cancel_at_period_end),
        ("team", lambda s: s.context == "team" and s.current is None),
        ("logged-out", lambda s: not s.logged_in),
    ],
)
def test_dev_fixture_states(monkeypatch, name, checker):
    monkeypatch.setenv("HERMES_DEV_SUBSCRIPTION_FIXTURE", name)
    s = dev_fixture_subscription_state()
    assert s is not None
    assert checker(s)


def test_dev_fixture_unknown_name_fails_safe(monkeypatch):
    monkeypatch.setenv("HERMES_DEV_SUBSCRIPTION_FIXTURE", "bogus")
    s = dev_fixture_subscription_state()
    assert s is not None
    assert s.logged_in is False
    assert s.error and "bogus" in s.error


def test_build_subscription_state_uses_fixture(monkeypatch):
    # build_subscription_state must short-circuit to the fixture (no portal call).
    monkeypatch.setenv("HERMES_DEV_SUBSCRIPTION_FIXTURE", "mid")
    s = build_subscription_state()
    assert s.logged_in is True
    assert s.current is not None and s.current.tier_id == "plus"
    # The manage URL is buildable from the fixture's portal_url + org_id.
    url = subscription_manage_url(s)
    assert url is not None
    assert url.endswith("/manage-subscription?org_id=org_acme")
