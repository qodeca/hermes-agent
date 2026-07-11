"""Tests for agent/model_router.py — the task-complexity model router.

Invariant-style: tests assert the decision *relationships* (tier ordering,
no-op contract, fail-open behaviour) against configs the tests construct
themselves — never snapshots of the shipped default keyword lists.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent.model_router import (
    RouteContext,
    RouteDecision,
    _tier_model_available,
    route_model,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

LIGHT_MODEL = "vendor/light-model"
STANDARD_MODEL = "vendor/standard-model"
HEAVY_MODEL = "vendor/heavy-model"

GREETING = "Good morning! Wishing you a great day."  # < 280 chars, no keywords
RESEARCH_5K = (
    "Research the current landscape of vector databases and produce a "
    "comparison. " * 60
)  # ~4600+ chars and a heavy keyword
AMBIGUOUS_TEXT = (
    "Take the notes from yesterday and turn them into a tidy summary for the "
    "team, keeping the tone informal and the structure loose. " * 5
)  # between light_max and heavy_min, no keywords, no URLs, no lists


def make_config(**routing_overrides):
    routing = {
        "enabled": True,
        "apply_to": ["cron", "delegate"],
        "classifier": "heuristic",
        "default_tier": "standard",
        "tiers": {
            "light": {"provider": "openrouter", "model": LIGHT_MODEL, "base_url": ""},
            "standard": {"provider": "openrouter", "model": STANDARD_MODEL, "base_url": ""},
            "heavy": {"provider": "openrouter", "model": HEAVY_MODEL, "base_url": ""},
        },
        "heuristics": {
            "light_max_chars": 280,
            "heavy_min_chars": 4000,
            "light_keywords": ["remind", "notify"],
            "heavy_keywords": ["research", "investigate"],
            "heavy_toolsets": ["browser", "delegation"],
        },
    }
    routing.update(routing_overrides)
    return {"routing": routing}


def ctx(**kwargs):
    kwargs.setdefault("origin", "cron")
    return RouteContext(**kwargs)


def assert_noop(decision):
    assert isinstance(decision, RouteDecision)
    assert decision.model is None
    assert decision.provider is None
    assert decision.base_url is None


@pytest.fixture(autouse=True)
def _catalog_always_resolves(monkeypatch):
    """Default: every tier model resolves. Individual tests override."""
    monkeypatch.setattr(
        "agent.model_router._tier_model_available", lambda provider, model, base_url: True
    )


# ─── heuristic tier table ────────────────────────────────────────────────────


class TestHeuristicTiers:
    def test_short_greeting_no_toolsets_routes_light(self):
        d = route_model(GREETING, context=ctx(), config=make_config())
        assert d.tier == "light"
        assert d.model == LIGHT_MODEL
        assert d.source == "heuristic"
        assert d.reason

    def test_long_research_with_browser_toolset_routes_heavy(self):
        assert len(RESEARCH_5K) >= 4000
        d = route_model(
            RESEARCH_5K, context=ctx(toolsets=("browser",)), config=make_config()
        )
        assert d.tier == "heavy"
        assert d.model == HEAVY_MODEL
        assert d.source == "heuristic"

    def test_heavy_toolset_alone_is_decisive_even_for_short_text(self):
        d = route_model(GREETING, context=ctx(toolsets=("browser",)), config=make_config())
        assert d.tier == "heavy"

    def test_attachments_floor_at_least_standard(self):
        d = route_model(GREETING, context=ctx(has_attachments=True), config=make_config())
        assert d.tier in ("standard", "heavy")
        assert d.model != LIGHT_MODEL

    def test_attachments_do_not_demote_heavy(self):
        d = route_model(
            RESEARCH_5K,
            context=ctx(toolsets=("browser",), has_attachments=True),
            config=make_config(),
        )
        assert d.tier == "heavy"

    def test_heavy_keyword_beats_light_length(self):
        # Short text containing a configured heavy keyword must not be
        # misrouted light purely on length.
        d = route_model("Investigate the outage.", context=ctx(), config=make_config())
        assert d.tier == "heavy"

    def test_multiple_urls_bump_heavy(self):
        text = "Compare https://example.com/a and https://example.org/b for me."
        d = route_model(text, context=ctx(), config=make_config())
        assert d.tier == "heavy"

    def test_skills_floor_at_standard_when_no_heavy_signal(self):
        # A short greeting would classify light, but attached skills floor
        # the tier at standard (T25/T26 build against this shape).
        d = route_model(GREETING, context=ctx(skills=("some-skill",)), config=make_config())
        assert d.tier == "standard"
        assert d.model == STANDARD_MODEL

    def test_skills_do_not_promote_past_standard(self):
        # The floor is exactly standard — skills must not push to heavy.
        d = route_model(GREETING, context=ctx(skills=("a", "b")), config=make_config())
        assert d.tier == "standard"

    def test_heavy_toolset_still_wins_over_skills_floor(self):
        d = route_model(
            GREETING,
            context=ctx(skills=("some-skill",), toolsets=("browser",)),
            config=make_config(),
        )
        assert d.tier == "heavy"

    def test_ambiguous_text_uses_default_tier(self):
        text = AMBIGUOUS_TEXT
        cfg = make_config()
        heur = cfg["routing"]["heuristics"]
        assert heur["light_max_chars"] < len(text) < heur["heavy_min_chars"]
        d = route_model(text, context=ctx(), config=cfg)
        assert d.tier == cfg["routing"]["default_tier"]
        assert d.source == "default"
        assert d.model == STANDARD_MODEL


# ─── override / gating no-ops ────────────────────────────────────────────────


class TestGating:
    def test_explicit_model_is_override_noop(self):
        d = route_model(
            GREETING, context=ctx(explicit_model="some/model"), config=make_config()
        )
        assert_noop(d)
        assert d.source == "override"

    def test_disabled_is_noop(self):
        d = route_model(GREETING, context=ctx(), config=make_config(enabled=False))
        assert_noop(d)
        assert d.source == "disabled"

    def test_origin_not_in_apply_to_is_noop(self):
        d = route_model(
            GREETING, context=ctx(origin="gateway"), config=make_config(apply_to=["cron"])
        )
        assert_noop(d)
        assert d.source == "disabled"

    def test_classifier_off_uses_default_tier(self):
        d = route_model(GREETING, context=ctx(), config=make_config(classifier="off"))
        assert d.tier == "standard"
        assert d.source == "default"

    def test_empty_tier_model_is_noop_for_that_tier(self):
        cfg = make_config()
        cfg["routing"]["tiers"]["light"] = {"provider": "", "model": "", "base_url": ""}
        d = route_model(GREETING, context=ctx(), config=cfg)
        assert_noop(d)
        assert d.reason  # explains the fall-through


# ─── catalog resolution ──────────────────────────────────────────────────────


class TestCatalogFallThrough:
    def test_unresolvable_tier_model_falls_through_to_default(self, monkeypatch):
        monkeypatch.setattr(
            "agent.model_router._tier_model_available",
            lambda provider, model, base_url: model != LIGHT_MODEL,
        )
        d = route_model(GREETING, context=ctx(), config=make_config())
        assert d.tier == "standard"
        assert d.model == STANDARD_MODEL
        assert d.source == "default"
        assert d.reason  # carries why the picked tier was skipped

    def test_nothing_resolvable_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            "agent.model_router._tier_model_available",
            lambda provider, model, base_url: False,
        )
        d = route_model(GREETING, context=ctx(), config=make_config())
        assert_noop(d)
        assert d.reason


class TestTierModelAvailableIsReadOnly:
    """The catalog check must never touch the network on the hot path.

    It consults only the on-disk provider-catalog cache — never the live
    ``provider_model_ids`` / ``cached_provider_model_ids`` fetch path, which
    can block on a synchronous HTTP GET during conversation start.
    """

    def _forbid_live_fetch(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("live catalog fetch must not run in the router hot path")

        monkeypatch.setattr("hermes_cli.models.provider_model_ids", _boom)
        monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", _boom)

    def test_never_calls_live_fetch_path(self, monkeypatch):
        self._forbid_live_fetch(monkeypatch)
        monkeypatch.setattr(
            "hermes_cli.models._load_provider_models_cache",
            lambda: {"openrouter": {"models": [LIGHT_MODEL]}},
        )
        # Present in cache → available; absent → not; neither path fetches live.
        assert _tier_model_available("openrouter", LIGHT_MODEL, "") is True
        assert _tier_model_available("openrouter", "vendor/absent-model", "") is False

    def test_cold_cache_accepts_tier(self, monkeypatch):
        # Cold/empty/absent cache must NOT suppress routing (accept → True).
        self._forbid_live_fetch(monkeypatch)
        monkeypatch.setattr(
            "hermes_cli.models._load_provider_models_cache", lambda: {}
        )
        assert _tier_model_available("openrouter", LIGHT_MODEL, "") is True

    def test_provider_missing_from_cache_accepts_tier(self, monkeypatch):
        self._forbid_live_fetch(monkeypatch)
        monkeypatch.setattr(
            "hermes_cli.models._load_provider_models_cache",
            lambda: {"anthropic": {"models": ["claude-x"]}},
        )
        assert _tier_model_available("openrouter", LIGHT_MODEL, "") is True

    def test_base_url_and_auto_provider_skip_catalog(self, monkeypatch):
        def _boom():
            raise AssertionError("catalog must not be read for base_url/auto tiers")

        monkeypatch.setattr("hermes_cli.models._load_provider_models_cache", _boom)
        assert _tier_model_available("custom", "m", "http://localhost:1234/v1") is True
        assert _tier_model_available("auto", "m", "") is True
        assert _tier_model_available("", "m", "") is True

    def test_end_to_end_route_uses_only_disk_cache(self, monkeypatch):
        # Full route_model path with the real _tier_model_available: a cold
        # cache leaves a configured tier usable and never fetches live.
        # Restore the real check (the autouse fixture stubbed it True).
        monkeypatch.setattr(
            "agent.model_router._tier_model_available", _tier_model_available
        )
        self._forbid_live_fetch(monkeypatch)
        monkeypatch.setattr(
            "hermes_cli.models._load_provider_models_cache", lambda: {}
        )
        d = route_model(GREETING, context=ctx(), config=make_config())
        assert d.tier == "light"
        assert d.model == LIGHT_MODEL


# ─── fail-open safety ────────────────────────────────────────────────────────


class TestFailOpen:
    def test_internal_exception_returns_error_noop_with_one_warning(
        self, monkeypatch, caplog
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("synthetic router failure")

        monkeypatch.setattr("agent.model_router._route", boom)
        with caplog.at_level(logging.WARNING, logger="agent.model_router"):
            d = route_model(GREETING, context=ctx(), config=make_config())
        assert_noop(d)
        assert d.source == "error"
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "agent.model_router"
        ]
        assert len(warnings) == 1


# ─── llm classifier (stage B) ────────────────────────────────────────────────


def _llm_response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class TestLlmClassifier:
    def test_timeout_falls_back_to_default_tier(self, monkeypatch):
        def timeout(**kwargs):
            raise TimeoutError("classifier timed out")

        monkeypatch.setattr("agent.model_router._invoke_classifier_llm", timeout)
        d = route_model(AMBIGUOUS_TEXT, context=ctx(), config=make_config(classifier="llm"))
        assert d.tier == "standard"
        assert d.source == "default"
        assert d.model == STANDARD_MODEL

    def test_unparseable_output_falls_back_to_default_tier(self, monkeypatch):
        monkeypatch.setattr(
            "agent.model_router._invoke_classifier_llm",
            lambda **kwargs: _llm_response("well, it depends on the task"),
        )
        d = route_model(AMBIGUOUS_TEXT, context=ctx(), config=make_config(classifier="llm"))
        assert d.tier == "standard"
        assert d.source == "default"

    def test_single_token_answer_is_honoured(self, monkeypatch):
        monkeypatch.setattr(
            "agent.model_router._invoke_classifier_llm",
            lambda **kwargs: _llm_response("heavy"),
        )
        d = route_model(AMBIGUOUS_TEXT, context=ctx(), config=make_config(classifier="llm"))
        assert d.tier == "heavy"
        assert d.source == "llm"
        assert d.model == HEAVY_MODEL

    def test_llm_not_consulted_when_stage_a_is_decisive(self, monkeypatch):
        def boom(**kwargs):
            raise AssertionError("classifier must not run for decisive heuristics")

        monkeypatch.setattr("agent.model_router._invoke_classifier_llm", boom)
        d = route_model(GREETING, context=ctx(), config=make_config(classifier="llm"))
        assert d.tier == "light"
        assert d.source == "heuristic"


# ─── config coercion / malformed input ───────────────────────────────────────


class TestConfigCoercion:
    def test_empty_config_is_safe_noop(self):
        d = route_model(GREETING, context=ctx(), config={})
        assert_noop(d)

    def test_non_dict_routing_block_is_safe_noop(self):
        d = route_model(GREETING, context=ctx(), config={"routing": "banana"})
        assert_noop(d)

    def test_partial_routing_block_missing_tiers_is_safe(self):
        d = route_model(GREETING, context=ctx(), config={"routing": {"enabled": True}})
        assert isinstance(d, RouteDecision)
        assert_noop(d)  # no tier models configured anywhere

    def test_malformed_heuristics_values_do_not_crash(self):
        cfg = make_config()
        cfg["routing"]["heuristics"] = {
            "light_max_chars": "lots",
            "heavy_min_chars": None,
            "light_keywords": "remind",
            "heavy_keywords": 7,
            "heavy_toolsets": {"nested": True},
        }
        d = route_model(GREETING, context=ctx(), config=cfg)
        assert isinstance(d, RouteDecision)
        assert d.source != "error"

    def test_string_apply_to_is_coerced(self):
        d = route_model(GREETING, context=ctx(), config=make_config(apply_to="cron"))
        assert d.tier == "light"
        assert d.model == LIGHT_MODEL

    def test_quoted_bool_enabled_is_not_truthy_garbage(self):
        # YAML users write enabled: "false"; that must stay disabled.
        d = route_model(GREETING, context=ctx(), config=make_config(enabled="false"))
        assert_noop(d)
        assert d.source == "disabled"


# ─── observability ───────────────────────────────────────────────────────────


class TestLogging:
    def test_one_info_line_per_decision_when_enabled(self, caplog):
        with caplog.at_level(logging.INFO, logger="agent.model_router"):
            route_model(GREETING, context=ctx(), config=make_config())
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "agent.model_router"
        ]
        assert len(infos) == 1
        line = infos[0].getMessage()
        assert "cron" in line and "light" in line

    def test_noop_decisions_are_logged_when_enabled(self, caplog):
        with caplog.at_level(logging.INFO, logger="agent.model_router"):
            route_model(
                GREETING,
                context=ctx(origin="gateway"),
                config=make_config(apply_to=["cron"]),
            )
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "agent.model_router"
        ]
        assert len(infos) == 1

    def test_disabled_routing_stays_silent(self, caplog):
        with caplog.at_level(logging.INFO, logger="agent.model_router"):
            route_model(GREETING, context=ctx(), config=make_config(enabled=False))
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "agent.model_router"
        ]
        assert infos == []


# ─── config schema ───────────────────────────────────────────────────────────


class TestConfigSchema:
    def test_default_config_ships_routing_disabled(self):
        from hermes_cli.config import DEFAULT_CONFIG

        routing = DEFAULT_CONFIG.get("routing")
        assert isinstance(routing, dict)
        assert routing.get("enabled") is False
        tiers = routing.get("tiers")
        assert isinstance(tiers, dict)
        assert set(tiers) == {"light", "standard", "heavy"}
        for tier_cfg in tiers.values():
            assert isinstance(tier_cfg, dict)
            # Empty model everywhere == pure no-op out of the box.
            assert tier_cfg.get("model") == ""
        heur = routing.get("heuristics")
        assert isinstance(heur, dict)
        for key in (
            "light_max_chars",
            "heavy_min_chars",
            "light_keywords",
            "heavy_keywords",
            "heavy_toolsets",
        ):
            assert key in heur

    def test_default_config_ships_auxiliary_routing_block(self):
        from hermes_cli.config import DEFAULT_CONFIG

        auxiliary = DEFAULT_CONFIG.get("auxiliary")
        assert isinstance(auxiliary, dict)
        aux = auxiliary.get("routing")
        assert isinstance(aux, dict)
        # Mirrors the shape of every other auxiliary task block.
        for key in ("provider", "model", "base_url", "api_key", "timeout", "extra_body"):
            assert key in aux

    def test_routing_is_a_known_root_key(self):
        from hermes_cli.config import _KNOWN_ROOT_KEYS

        assert "routing" in _KNOWN_ROOT_KEYS

    def test_route_model_defaults_are_noop_end_to_end(self):
        from hermes_cli.config import DEFAULT_CONFIG

        d = route_model(GREETING, context=ctx(), config=DEFAULT_CONFIG)
        assert_noop(d)
        assert d.source == "disabled"
