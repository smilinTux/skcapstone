"""
Model Router — automatic model selection based on task requirements.

Reads a TaskSignal (description, tags, privacy flags, token estimate) and
returns a RouteDecision that identifies the optimal model tier and a concrete
model name to use for the task.

Decision precedence:
    1. privacy_sensitive=True  → LOCAL tier (never leaves the node)
    2. requires_localhost=True → LOCAL tier on the originating node
    3. Tag-rule matching       → highest-priority matching TagRule wins
    4. Token-based fallback    → estimated_tokens > 16 000 → REASON, else FAST
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from skcapstone.blueprints.schema import ModelTier


# ---------------------------------------------------------------------------
# Supporting models
# ---------------------------------------------------------------------------


class TagRule(BaseModel):
    """Maps a set of keywords to a model tier with a priority weight.

    When any keyword in *keywords* matches a tag in the incoming
    :class:`TaskSignal`, this rule is considered a candidate.  Among all
    candidates, the one with the highest *priority* wins.
    """

    keywords: List[str] = Field(description="Keywords that trigger this rule")
    tier: ModelTier = Field(description="Target tier when the rule fires")
    priority: int = Field(default=0, description="Higher value wins on conflict")


class TaskSignal(BaseModel):
    """Describes the nature of a task so the router can pick the right model.

    Args:
        description: Human-readable summary of what the task involves.
        tags: Free-form labels (e.g. ["code", "refactor"]).
        requires_localhost: If True, the task must run on the originating node.
        privacy_sensitive: If True, forces LOCAL tier regardless of other signals.
        estimated_tokens: Rough token budget hint (context + expected output).
    """

    description: str = Field(description="What the task is about")
    tags: List[str] = Field(default_factory=list, description="Classification tags")
    requires_localhost: bool = Field(
        default=False, description="Must run on the originating node"
    )
    privacy_sensitive: bool = Field(
        default=False, description="Forces LOCAL tier"
    )
    estimated_tokens: int = Field(
        default=0, description="Estimated token usage hint"
    )


class RouteDecision(BaseModel):
    """The output of the router describing which model/tier to use.

    Args:
        tier: Selected model tier.
        model_name: Specific model identifier within that tier.
        reasoning: Human-readable explanation of why this decision was made.
        preferred_node: Optional node hostname if a specific node is required.
    """

    tier: ModelTier
    model_name: str
    reasoning: str
    preferred_node: Optional[str] = Field(
        default=None, description="Specific node if required"
    )


class ModelRouterConfig(BaseModel):
    """Configuration for the :class:`ModelRouter`.

    Args:
        tier_models: Maps tier name (e.g. ``"code"``) to an ordered list of
            model names.  The first entry is the preferred model for that tier.
        tag_rules: Ordered list of keyword-to-tier mappings.
    """

    tier_models: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Maps tier name to list of model names (first = preferred)",
    )
    tag_rules: List[TagRule] = Field(
        default_factory=list,
        description="Keyword→tier rules evaluated against task tags",
    )

    @classmethod
    def default(cls) -> "ModelRouterConfig":
        """Return the default configuration with NVIDIA-aligned model assignments.

        Returns:
            ModelRouterConfig: Sensible defaults covering all five tiers and
            the four primary tag-rule groups.
        """
        return cls(
            tier_models={
                ModelTier.FAST.value: ["nemotron-49b", "claude-haiku-3-5"],
                ModelTier.CODE.value: ["devstral", "qwen3-coder"],
                ModelTier.REASON.value: ["deepseek-r1", "claude-opus-4-5"],
                ModelTier.NUANCE.value: ["kimi-k2.5", "claude-sonnet-4-5"],
                ModelTier.LOCAL.value: ["llama-3.3-70b-local", "mistral-7b-local"],
            },
            tag_rules=[
                TagRule(
                    keywords=["code", "refactor", "debug", "test", "implement"],
                    tier=ModelTier.CODE,
                    priority=10,
                ),
                TagRule(
                    keywords=[
                        "architecture",
                        "design",
                        "analyze",
                        "research",
                        "plan",
                    ],
                    tier=ModelTier.REASON,
                    priority=10,
                ),
                TagRule(
                    keywords=[
                        "marketing",
                        "creative",
                        "email",
                        "copy",
                        "comms",
                        "writing",
                    ],
                    tier=ModelTier.NUANCE,
                    priority=10,
                ),
                TagRule(
                    keywords=["format", "rename", "lint", "simple", "trivial"],
                    tier=ModelTier.FAST,
                    priority=10,
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_LARGE_TOKEN_THRESHOLD = 16_000


class ModelRouter:
    """Routes a :class:`TaskSignal` to the most appropriate model tier and name.

    Args:
        config: Router configuration containing tier-to-model mappings and
            tag rules.  Defaults to :meth:`ModelRouterConfig.default`.
    """

    def __init__(self, config: Optional[ModelRouterConfig] = None) -> None:
        self.config: ModelRouterConfig = config or ModelRouterConfig.default()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, signal: TaskSignal) -> RouteDecision:
        """Select the optimal model tier and name for *signal*.

        Decision precedence (first match wins):
            1. ``privacy_sensitive=True`` → LOCAL
            2. ``requires_localhost=True`` → LOCAL (pinned to originating node)
            3. Tag-rule matching (highest-priority rule wins)
            4. Token fallback: > 16 000 → REASON, otherwise FAST

        Args:
            signal: Describes the task to be routed.

        Returns:
            RouteDecision: Tier, concrete model name, reasoning, and optional
            preferred node.
        """
        # --- Privacy gate ---------------------------------------------------
        if signal.privacy_sensitive:
            return self._decide(
                tier=ModelTier.LOCAL,
                reasoning="Task marked privacy_sensitive; forced to LOCAL tier.",
                preferred_node=None,
            )

        # --- Localhost gate --------------------------------------------------
        if signal.requires_localhost:
            return self._decide(
                tier=ModelTier.LOCAL,
                reasoning="Task requires localhost execution; forced to LOCAL tier.",
                preferred_node="localhost",
            )

        # --- Tag-rule matching -----------------------------------------------
        best_rule = self._best_tag_rule(signal.tags)
        if best_rule is not None:
            return self._decide(
                tier=best_rule.tier,
                reasoning=(
                    f"Tag rule matched (keywords={best_rule.keywords}, "
                    f"priority={best_rule.priority})."
                ),
                preferred_node=None,
            )

        # --- Token-based fallback -------------------------------------------
        if signal.estimated_tokens > _LARGE_TOKEN_THRESHOLD:
            return self._decide(
                tier=ModelTier.REASON,
                reasoning=(
                    f"No tag rule matched; estimated_tokens={signal.estimated_tokens} "
                    f"exceeds {_LARGE_TOKEN_THRESHOLD}, using REASON tier."
                ),
                preferred_node=None,
            )

        return self._decide(
            tier=ModelTier.FAST,
            reasoning=(
                "No tag rule matched and token budget is small; defaulting to FAST tier."
            ),
            preferred_node=None,
        )

    @classmethod
    def from_config(cls, path: Path) -> "ModelRouter":
        """Load a :class:`ModelRouter` from a YAML configuration file.

        The YAML file should serialise a :class:`ModelRouterConfig` dict, e.g.:

        .. code-block:: yaml

            tier_models:
              fast: [nemotron-49b]
              code: [devstral]
            tag_rules:
              - keywords: [code]
                tier: code
                priority: 10

        Args:
            path: Filesystem path to the YAML configuration file.

        Returns:
            ModelRouter: Initialised router using the loaded configuration.

        Raises:
            FileNotFoundError: If *path* does not exist.
            ValueError: If the YAML content cannot be parsed into a valid config.
        """
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = ModelRouterConfig.model_validate(raw)
        return cls(config=config)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _best_tag_rule(self, tags: List[str]) -> Optional[TagRule]:
        """Return the highest-priority rule whose keywords overlap with *tags*.

        Args:
            tags: List of tags from the incoming :class:`TaskSignal`.

        Returns:
            The matching :class:`TagRule` with the highest priority, or ``None``
            if no rule matches.
        """
        normalised = {t.lower() for t in tags}
        best: Optional[TagRule] = None
        for rule in self.config.tag_rules:
            rule_keywords = {kw.lower() for kw in rule.keywords}
            if rule_keywords & normalised:  # any intersection
                if best is None or rule.priority > best.priority:
                    best = rule
        return best

    def _decide(
        self,
        tier: ModelTier,
        reasoning: str,
        preferred_node: Optional[str],
    ) -> RouteDecision:
        """Build a :class:`RouteDecision` for *tier*, picking the first known model.

        Args:
            tier: The selected model tier.
            reasoning: Human-readable explanation.
            preferred_node: Optional node constraint.

        Returns:
            RouteDecision: Fully populated routing decision.
        """
        models = self.config.tier_models.get(tier.value, [])
        model_name = models[0] if models else f"unknown-{tier.value}"
        return RouteDecision(
            tier=tier,
            model_name=model_name,
            reasoning=reasoning,
            preferred_node=preferred_node,
        )
