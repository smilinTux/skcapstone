"""Tests for the ModelRouter — automatic model selection layer.

Covers:
- Routing by tag to each primary tier (CODE, NUANCE, FAST)
- Privacy-sensitive forcing LOCAL tier
- Token-based fallback to REASON
- Tag-rule priority conflict resolution
- Config load from YAML
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from skcapstone.blueprints.schema import ModelTier
from skcapstone.model_router import (
    ModelRouter,
    ModelRouterConfig,
    TagRule,
    TaskSignal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def router() -> ModelRouter:
    """Return a ModelRouter loaded with the default configuration."""
    return ModelRouter()


# ---------------------------------------------------------------------------
# Tag-based routing
# ---------------------------------------------------------------------------


class TestTagRouting:
    """Routing decisions driven by tags in the TaskSignal."""

    def test_code_tags_route_to_code_tier(self, router: ModelRouter) -> None:
        """A task tagged 'code' and 'refactor' should land on the CODE tier."""
        signal = TaskSignal(
            description="Refactor the authentication module",
            tags=["code", "refactor"],
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.CODE

    def test_code_tier_returns_known_model(self, router: ModelRouter) -> None:
        """CODE tier must resolve to a non-empty model name."""
        signal = TaskSignal(description="Implement login flow", tags=["implement"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.CODE
        assert decision.model_name  # not empty

    def test_marketing_tags_route_to_nuance_tier(self, router: ModelRouter) -> None:
        """A task tagged 'marketing' and 'creative' should land on NUANCE tier."""
        signal = TaskSignal(
            description="Write landing page copy",
            tags=["marketing", "creative"],
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.NUANCE

    def test_format_tag_routes_to_fast_tier(self, router: ModelRouter) -> None:
        """A task tagged 'format' should resolve to the FAST tier."""
        signal = TaskSignal(description="Reformat this file", tags=["format"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_architecture_tag_routes_to_reason_tier(
        self, router: ModelRouter
    ) -> None:
        """A task tagged 'architecture' should land on the REASON tier."""
        signal = TaskSignal(
            description="Design the data pipeline",
            tags=["architecture", "design"],
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.REASON

    def test_case_insensitive_tag_matching(self, router: ModelRouter) -> None:
        """Tags are matched case-insensitively."""
        signal = TaskSignal(description="Write some code", tags=["CODE", "DEBUG"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.CODE


# ---------------------------------------------------------------------------
# Privacy / localhost gates
# ---------------------------------------------------------------------------


class TestPrivacyGates:
    """LOCAL tier is forced by privacy or localhost flags."""

    def test_privacy_sensitive_forces_local_tier(self, router: ModelRouter) -> None:
        """privacy_sensitive=True must route to LOCAL regardless of tags."""
        signal = TaskSignal(
            description="Process patient health records",
            tags=["code", "implement"],
            privacy_sensitive=True,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.LOCAL

    def test_privacy_sensitive_returns_local_model(self, router: ModelRouter) -> None:
        """LOCAL tier should resolve to a configured local model name."""
        signal = TaskSignal(
            description="Summarise confidential notes",
            privacy_sensitive=True,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.LOCAL
        assert decision.model_name

    def test_requires_localhost_forces_local_tier(self, router: ModelRouter) -> None:
        """requires_localhost=True must route to LOCAL with preferred_node set."""
        signal = TaskSignal(
            description="Run local GPU benchmark",
            requires_localhost=True,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.LOCAL
        assert decision.preferred_node == "localhost"

    def test_privacy_takes_precedence_over_localhost(
        self, router: ModelRouter
    ) -> None:
        """privacy_sensitive wins; preferred_node should not be set to localhost."""
        signal = TaskSignal(
            description="Private task on local machine",
            privacy_sensitive=True,
            requires_localhost=True,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.LOCAL
        # privacy path doesn't pin a preferred_node
        assert decision.preferred_node is None


# ---------------------------------------------------------------------------
# Token-based fallback
# ---------------------------------------------------------------------------


class TestTokenFallback:
    """When no tags match, token count drives the fallback tier."""

    def test_large_token_count_routes_to_reason(self, router: ModelRouter) -> None:
        """A task with > 16 000 tokens and no matching tags should use REASON."""
        signal = TaskSignal(
            description="Analyse a large codebase",
            tags=["unknown-tag"],
            estimated_tokens=20_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.REASON

    def test_small_token_count_routes_to_fast(self, router: ModelRouter) -> None:
        """A task with no matching tags and small token budget should use FAST."""
        signal = TaskSignal(
            description="Some unknown task",
            tags=[],
            estimated_tokens=100,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_exactly_threshold_tokens_routes_to_fast(
        self, router: ModelRouter
    ) -> None:
        """estimated_tokens == 16 000 (not strictly greater) should remain FAST."""
        signal = TaskSignal(
            description="Borderline task",
            tags=[],
            estimated_tokens=16_000,
        )
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST


# ---------------------------------------------------------------------------
# Priority conflict resolution
# ---------------------------------------------------------------------------


class TestTagRulePriority:
    """Higher-priority rules win when multiple rules match."""

    def test_higher_priority_rule_wins(self) -> None:
        """When two rules match the same tags, the higher priority one wins."""
        config = ModelRouterConfig(
            tier_models={
                ModelTier.CODE.value: ["devstral"],
                ModelTier.REASON.value: ["deepseek-r1"],
            },
            tag_rules=[
                TagRule(keywords=["analyze"], tier=ModelTier.CODE, priority=5),
                TagRule(keywords=["analyze"], tier=ModelTier.REASON, priority=15),
            ],
        )
        router = ModelRouter(config=config)
        signal = TaskSignal(description="Analyze dependencies", tags=["analyze"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.REASON

    def test_lower_priority_rule_loses(self) -> None:
        """The lower-priority rule must not override the higher-priority one."""
        config = ModelRouterConfig(
            tier_models={
                ModelTier.FAST.value: ["haiku"],
                ModelTier.NUANCE.value: ["kimi-k2.5"],
            },
            tag_rules=[
                TagRule(keywords=["copy"], tier=ModelTier.NUANCE, priority=20),
                TagRule(keywords=["copy"], tier=ModelTier.FAST, priority=1),
            ],
        )
        router = ModelRouter(config=config)
        signal = TaskSignal(description="Write marketing copy", tags=["copy"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.NUANCE

    def test_no_overlap_falls_through_to_fallback(self) -> None:
        """Rules with no keyword overlap must not fire."""
        config = ModelRouterConfig(
            tier_models={
                ModelTier.FAST.value: ["haiku"],
                ModelTier.CODE.value: ["devstral"],
            },
            tag_rules=[
                TagRule(keywords=["code"], tier=ModelTier.CODE, priority=10),
            ],
        )
        router = ModelRouter(config=config)
        signal = TaskSignal(description="Some unrelated task", tags=["unknown"])
        decision = router.route(signal)
        # No rule matched; small token budget → FAST
        assert decision.tier == ModelTier.FAST


# ---------------------------------------------------------------------------
# Config load from YAML
# ---------------------------------------------------------------------------


class TestConfigFromYaml:
    """ModelRouter.from_config loads settings from a YAML file."""

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        """A minimal valid YAML config should load without errors."""
        yaml_content = textwrap.dedent(
            """\
            tier_models:
              fast: [my-fast-model]
              code: [my-code-model]
              reason: [my-reason-model]
              nuance: [my-nuance-model]
              local: [my-local-model]
            tag_rules:
              - keywords: [code, implement]
                tier: code
                priority: 10
              - keywords: [writing, email]
                tier: nuance
                priority: 10
            """
        )
        config_file = tmp_path / "router_config.yaml"
        config_file.write_text(yaml_content)

        router = ModelRouter.from_config(config_file)

        code_signal = TaskSignal(description="Implement feature X", tags=["code"])
        decision = router.route(code_signal)
        assert decision.tier == ModelTier.CODE
        assert decision.model_name == "my-code-model"

    def test_yaml_nuance_rule(self, tmp_path: Path) -> None:
        """YAML-loaded NUANCE rule fires correctly on matching tags."""
        yaml_content = textwrap.dedent(
            """\
            tier_models:
              nuance: [yaml-nuance-model]
              fast: [yaml-fast-model]
            tag_rules:
              - keywords: [writing, email]
                tier: nuance
                priority: 10
            """
        )
        config_file = tmp_path / "router.yaml"
        config_file.write_text(yaml_content)

        router = ModelRouter.from_config(config_file)
        signal = TaskSignal(description="Draft an email", tags=["email"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.NUANCE
        assert decision.model_name == "yaml-nuance-model"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Loading from a non-existent path must raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ModelRouter.from_config(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# RouteDecision content
# ---------------------------------------------------------------------------


class TestRouteDecisionContent:
    """RouteDecision always contains a non-empty reasoning string."""

    def test_reasoning_is_non_empty(self, router: ModelRouter) -> None:
        """Every decision must include a human-readable reasoning string."""
        signal = TaskSignal(description="Do something", tags=["code"])
        decision = router.route(signal)
        assert decision.reasoning
        assert len(decision.reasoning) > 0

    def test_preferred_node_none_by_default(self, router: ModelRouter) -> None:
        """preferred_node should be None unless locality is required."""
        signal = TaskSignal(description="Regular coding task", tags=["implement"])
        decision = router.route(signal)
        assert decision.preferred_node is None
