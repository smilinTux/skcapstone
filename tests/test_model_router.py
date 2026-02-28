"""Tests for the ModelRouter — automatic model selection layer.

Covers:
- Routing by tag to each primary tier (CODE, NUANCE, FAST)
- Privacy-sensitive forcing LOCAL tier
- Token-based fallback to REASON
- Tag-rule priority conflict resolution
- Config load from YAML
- Model name resolution per tier
- MCP tool handler integration
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


# ---------------------------------------------------------------------------
# Model name resolution
# ---------------------------------------------------------------------------


class TestModelNameResolution:
    """Verify the correct concrete model is selected per tier."""

    def test_default_fast_model(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="quick task", tags=["simple"])
        decision = router.route(signal)
        assert decision.model_name == "nemotron-49b"

    def test_default_code_model(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="implement feature", tags=["code"])
        decision = router.route(signal)
        assert decision.model_name == "devstral"

    def test_default_reason_model(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="system design", tags=["architecture"])
        decision = router.route(signal)
        assert decision.model_name == "deepseek-r1"

    def test_default_nuance_model(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="write copy", tags=["marketing"])
        decision = router.route(signal)
        assert decision.model_name == "kimi-k2.5"

    def test_default_local_model(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="private task", privacy_sensitive=True)
        decision = router.route(signal)
        assert decision.model_name == "llama-3.3-70b-local"

    def test_unknown_tier_sentinel(self) -> None:
        """Missing tier config produces an unknown-{tier} sentinel."""
        config = ModelRouterConfig(tier_models={}, tag_rules=[])
        router = ModelRouter(config=config)
        signal = TaskSignal(description="task")
        decision = router.route(signal)
        assert decision.model_name == "unknown-fast"

    def test_custom_model_name(self) -> None:
        config = ModelRouterConfig(
            tier_models={"code": ["my-custom-coder"]},
            tag_rules=[TagRule(keywords=["code"], tier=ModelTier.CODE, priority=10)],
        )
        router = ModelRouter(config=config)
        signal = TaskSignal(description="code task", tags=["code"])
        decision = router.route(signal)
        assert decision.model_name == "my-custom-coder"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Boundary conditions and unusual inputs."""

    def test_empty_description(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="", tags=["code"])
        decision = router.route(signal)
        assert decision.tier == ModelTier.CODE

    def test_token_boundary_16000_is_fast(self, router: ModelRouter) -> None:
        """Exactly 16000 tokens (not strictly >) stays FAST."""
        signal = TaskSignal(description="boundary", estimated_tokens=16_000)
        decision = router.route(signal)
        assert decision.tier == ModelTier.FAST

    def test_token_boundary_16001_is_reason(self, router: ModelRouter) -> None:
        signal = TaskSignal(description="boundary", estimated_tokens=16_001)
        decision = router.route(signal)
        assert decision.tier == ModelTier.REASON

    def test_model_dump_serializable(self, router: ModelRouter) -> None:
        """RouteDecision.model_dump() produces JSON-serializable dict."""
        import json

        signal = TaskSignal(description="test", tags=["code"])
        decision = router.route(signal)
        dumped = decision.model_dump()
        serialized = json.dumps(dumped)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["tier"] == "code"

    def test_all_tag_keywords_covered(self, router: ModelRouter) -> None:
        """Each default tag rule keyword individually routes to its tier."""
        tier_keywords = {
            ModelTier.CODE: ["code", "refactor", "debug", "test", "implement"],
            ModelTier.REASON: ["architecture", "design", "analyze", "research", "plan"],
            ModelTier.NUANCE: [
                "marketing", "creative", "email", "copy", "comms", "writing",
            ],
            ModelTier.FAST: ["format", "rename", "lint", "simple", "trivial"],
        }
        for expected_tier, keywords in tier_keywords.items():
            for kw in keywords:
                signal = TaskSignal(description=f"task-{kw}", tags=[kw])
                decision = router.route(signal)
                assert decision.tier == expected_tier, (
                    f"keyword '{kw}' routed to {decision.tier}, expected {expected_tier}"
                )


# ---------------------------------------------------------------------------
# MCP tool handler integration
# ---------------------------------------------------------------------------


class TestMCPModelRouteHandler:
    """Test the _handle_model_route MCP tool handler."""

    @pytest.fixture(autouse=True)
    def _import_handler(self):
        from skcapstone.mcp_server import _handle_model_route

        self.handler = _handle_model_route

    @pytest.mark.asyncio
    async def test_basic_route(self) -> None:
        result = await self.handler({"description": "implement login"})
        assert len(result) == 1
        import json

        data = json.loads(result[0].text)
        assert "tier" in data
        assert "model_name" in data
        assert "reasoning" in data

    @pytest.mark.asyncio
    async def test_route_with_tags(self) -> None:
        import json

        result = await self.handler({
            "description": "refactor auth module",
            "tags": ["code", "refactor"],
        })
        data = json.loads(result[0].text)
        assert data["tier"] == "code"

    @pytest.mark.asyncio
    async def test_route_privacy_sensitive(self) -> None:
        import json

        result = await self.handler({
            "description": "process medical records",
            "privacy_sensitive": True,
        })
        data = json.loads(result[0].text)
        assert data["tier"] == "local"

    @pytest.mark.asyncio
    async def test_route_localhost(self) -> None:
        import json

        result = await self.handler({
            "description": "local benchmark",
            "requires_localhost": True,
        })
        data = json.loads(result[0].text)
        assert data["tier"] == "local"
        assert data["preferred_node"] == "localhost"

    @pytest.mark.asyncio
    async def test_route_with_token_estimate(self) -> None:
        import json

        result = await self.handler({
            "description": "big analysis",
            "estimated_tokens": 30_000,
        })
        data = json.loads(result[0].text)
        assert data["tier"] == "reason"

    @pytest.mark.asyncio
    async def test_route_minimal_args(self) -> None:
        """Handler works with only the required 'description' field."""
        import json

        result = await self.handler({"description": "anything"})
        data = json.loads(result[0].text)
        assert data["tier"] == "fast"

    @pytest.mark.asyncio
    async def test_route_empty_description(self) -> None:
        import json

        result = await self.handler({"description": ""})
        data = json.loads(result[0].text)
        assert "tier" in data

    @pytest.mark.asyncio
    async def test_route_all_fields(self) -> None:
        """Handler accepts all optional fields together."""
        import json

        result = await self.handler({
            "description": "sensitive local code review",
            "tags": ["code"],
            "requires_localhost": False,
            "privacy_sensitive": True,
            "estimated_tokens": 50_000,
        })
        data = json.loads(result[0].text)
        # privacy_sensitive takes precedence
        assert data["tier"] == "local"
