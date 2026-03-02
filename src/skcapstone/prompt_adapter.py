"""
Prompt Adapter — per-model best-practice prompt formatting.

Each LLM family has different expectations for system prompts,
temperatures, thinking modes, and structural formatting. The
PromptAdapter reads ModelProfile configs and reformats prompts
to match each model's optimal input format.

Architecture:
    ModelProfile   — Pydantic model describing a model's prompt expectations
    AdaptedPrompt  — The output: messages, system_param, temperature, extras
    PromptAdapter  — Loads profiles, matches models, adapts prompts
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from skcapstone.blueprints.schema import ModelTier

logger = logging.getLogger("skcapstone.prompt_adapter")

_BUNDLED_PROFILES = Path(__file__).parent / "data" / "model_profiles.yaml"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelProfile(BaseModel):
    """Prompt formatting profile for a specific model or model family."""

    model_pattern: str
    family: str

    # System prompt behavior
    system_prompt_mode: str = "standard"  # "standard" | "separate_param" | "omit"

    # Structural formatting preference
    structure_format: str = "markdown"  # "xml" | "markdown" | "plain"

    # Temperature defaults
    default_temperature: Optional[float] = None
    code_temperature: Optional[float] = None
    reasoning_temperature: Optional[float] = None

    # Thinking/reasoning
    thinking_enabled: bool = False
    thinking_mode: str = "none"  # "none" | "budget" | "toggle" | "auto"
    thinking_budget_tokens: int = 4096

    # Max system prompt length
    max_system_tokens: int = 4000

    # Special instructions
    no_few_shot: bool = False
    no_cot_instructions: bool = False
    supports_tool_calling: bool = True
    tool_format: str = "openai"  # "openai" | "anthropic" | "mistral"

    # Metadata
    last_updated: str = ""
    source_url: str = ""
    notes: str = ""


class AdaptedPrompt(BaseModel):
    """Result of prompt adaptation — ready to send to provider."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    system_param: Optional[str] = None
    temperature: Optional[float] = None
    extra_params: dict[str, Any] = Field(default_factory=dict)
    profile_used: str = ""
    adaptations_applied: list[str] = Field(default_factory=list)


# Fallback profile for unknown models
_GENERIC_PROFILE = ModelProfile(
    model_pattern=".*",
    family="generic",
    system_prompt_mode="standard",
    structure_format="markdown",
)


# ---------------------------------------------------------------------------
# PromptAdapter
# ---------------------------------------------------------------------------


class PromptAdapter:
    """Reformats system+user prompts to match the target model's expectations.

    Loads model profiles from YAML, matches model names via regex,
    and produces AdaptedPrompt objects ready for each provider.

    Args:
        profiles_path: Path to a YAML profiles file. Falls back to
            the bundled default if None or missing.
    """

    def __init__(self, profiles_path: Optional[Path] = None) -> None:
        self._profiles: list[ModelProfile] = []
        self._load_profiles(profiles_path)

    def _load_profiles(self, profiles_path: Optional[Path] = None) -> None:
        """Load profiles from YAML file(s).

        Priority: custom path > bundled defaults.
        """
        paths_to_try = []
        if profiles_path and profiles_path.exists():
            paths_to_try.append(profiles_path)
        if _BUNDLED_PROFILES.exists():
            paths_to_try.append(_BUNDLED_PROFILES)

        loaded_families: set[str] = set()
        for path in paths_to_try:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not raw or "profiles" not in raw:
                    continue
                for entry in raw["profiles"]:
                    profile = ModelProfile.model_validate(entry)
                    if profile.family not in loaded_families:
                        self._profiles.append(profile)
                        loaded_families.add(profile.family)
            except Exception as exc:
                logger.warning("Failed to load profiles from %s: %s", path, exc)

        if not self._profiles:
            logger.warning("No model profiles loaded — using generic fallback")

    def resolve_profile(self, model_name: str) -> ModelProfile:
        """Match model_name against profiles via regex.

        Falls back to a generic profile if nothing matches.

        Args:
            model_name: The model identifier (e.g. "claude-opus-4-5").

        Returns:
            The best matching ModelProfile.
        """
        for profile in self._profiles:
            try:
                if re.search(profile.model_pattern, model_name, re.IGNORECASE):
                    return profile
            except re.error:
                continue
        return _GENERIC_PROFILE

    def adapt(
        self,
        system_prompt: str,
        user_message: str,
        model_name: str,
        tier: ModelTier,
    ) -> AdaptedPrompt:
        """Transform system+user into model-optimal format.

        Args:
            system_prompt: The system-level context (identity, soul, etc.).
            user_message: The user/peer message content.
            model_name: Target model name for profile lookup.
            tier: The routing tier (affects temperature selection).

        Returns:
            AdaptedPrompt ready for the provider callback.
        """
        profile = self.resolve_profile(model_name)
        adaptations: list[str] = []

        # Format system prompt structure
        formatted_system = self._format_system_for_model(
            system_prompt, profile, tier
        )

        # Build messages array
        messages: list[dict[str, Any]] = []
        system_param: Optional[str] = None

        if profile.system_prompt_mode == "omit":
            # DeepSeek R1: no system prompt, merge into user message
            combined = f"{formatted_system}\n\n{user_message}" if formatted_system else user_message
            messages.append({"role": "user", "content": combined})
            adaptations.append("omitted_system_prompt")
        elif profile.system_prompt_mode == "separate_param":
            # Claude: system goes as separate kwarg
            system_param = formatted_system
            messages.append({"role": "user", "content": user_message})
            adaptations.append("system_as_separate_param")
        else:
            # Standard: system as first message
            if formatted_system:
                messages.append({"role": "system", "content": formatted_system})
            messages.append({"role": "user", "content": user_message})
            adaptations.append("system_as_first_message")

        # Resolve temperature
        temperature = self._resolve_temperature(profile, tier)
        if temperature is not None:
            adaptations.append(f"set_temp_{temperature}")

        # Thinking/reasoning config
        extra_params = self._apply_thinking_config(profile, tier)
        if extra_params:
            adaptations.append(f"thinking_{profile.thinking_mode}")

        return AdaptedPrompt(
            messages=messages,
            system_param=system_param,
            temperature=temperature,
            extra_params=extra_params,
            profile_used=profile.family,
            adaptations_applied=adaptations,
        )

    def reload_profiles(self, profiles_path: Optional[Path] = None) -> None:
        """Hot-reload profiles from YAML.

        Args:
            profiles_path: Optional custom profiles path.
        """
        self._profiles.clear()
        self._load_profiles(profiles_path)
        logger.info("Reloaded %d model profiles", len(self._profiles))

    def update_profile(self, model_pattern: str, updates: dict) -> None:
        """Update a single profile entry in memory.

        Args:
            model_pattern: The profile's model_pattern to find.
            updates: Dict of field names and new values.
        """
        for profile in self._profiles:
            if profile.model_pattern == model_pattern:
                for key, value in updates.items():
                    if hasattr(profile, key):
                        setattr(profile, key, value)
                return
        logger.warning("Profile not found for pattern: %s", model_pattern)

    @property
    def profiles(self) -> list[ModelProfile]:
        """All loaded profiles."""
        return list(self._profiles)

    # -------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------

    def _format_system_for_model(
        self,
        system_prompt: str,
        profile: ModelProfile,
        tier: ModelTier,
    ) -> str:
        """Reformat structural markup for the target model.

        Args:
            system_prompt: Raw system prompt text.
            profile: Target model's profile.
            tier: Routing tier (may affect formatting).

        Returns:
            Reformatted system prompt string.
        """
        if not system_prompt:
            return ""

        fmt = profile.structure_format

        if fmt == "xml":
            # Wrap in XML tags for Claude
            return (
                "<instructions>\n"
                f"{system_prompt}\n"
                "</instructions>"
            )
        elif fmt == "plain":
            # Strip markdown formatting
            cleaned = system_prompt
            cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
            cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
            cleaned = re.sub(r"\*(.*?)\*", r"\1", cleaned)
            return cleaned
        else:
            # markdown — return as-is (default)
            return system_prompt

    def _resolve_temperature(
        self,
        profile: ModelProfile,
        tier: ModelTier,
    ) -> Optional[float]:
        """Pick the right temperature based on tier and profile.

        Args:
            profile: Model profile with temp settings.
            tier: Current routing tier.

        Returns:
            Temperature float or None for provider default.
        """
        if tier == ModelTier.CODE and profile.code_temperature is not None:
            return profile.code_temperature
        if tier == ModelTier.REASON and profile.reasoning_temperature is not None:
            return profile.reasoning_temperature
        return profile.default_temperature

    def _apply_thinking_config(
        self,
        profile: ModelProfile,
        tier: ModelTier,
    ) -> dict[str, Any]:
        """Return extra API params for thinking/reasoning.

        Args:
            profile: Model profile.
            tier: Routing tier.

        Returns:
            Dict of extra params (empty if no thinking config).
        """
        if not profile.thinking_enabled:
            return {}

        mode = profile.thinking_mode

        if mode == "budget":
            # Claude extended thinking
            return {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": profile.thinking_budget_tokens,
                },
            }
        elif mode == "toggle":
            # Qwen/Nemotron enable_thinking
            return {"enable_thinking": True}
        elif mode == "auto":
            # DeepSeek R1 — automatic, don't interfere
            return {}

        return {}
