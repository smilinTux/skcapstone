"""Tests for the prompt adapter — per-model formatting."""

from __future__ import annotations

import pytest
from pathlib import Path

from skcapstone.prompt_adapter import (
    AdaptedPrompt,
    ModelProfile,
    PromptAdapter,
    _GENERIC_PROFILE,
)
from skcapstone.blueprints.schema import ModelTier


class TestModelProfile:
    """ModelProfile Pydantic model tests."""

    def test_defaults(self):
        """Default profile values are sensible."""
        p = ModelProfile(model_pattern="test-.*", family="test")
        assert p.system_prompt_mode == "standard"
        assert p.structure_format == "markdown"
        assert p.thinking_enabled is False
        assert p.thinking_mode == "none"
        assert p.max_system_tokens == 4000

    def test_claude_profile(self):
        """Claude profile has separate_param system mode and xml format."""
        p = ModelProfile(
            model_pattern="claude-.*",
            family="claude",
            system_prompt_mode="separate_param",
            structure_format="xml",
            thinking_enabled=True,
            thinking_mode="budget",
        )
        assert p.system_prompt_mode == "separate_param"
        assert p.structure_format == "xml"
        assert p.thinking_mode == "budget"

    def test_deepseek_r1_profile(self):
        """DeepSeek R1 omits system prompt and disables few-shot."""
        p = ModelProfile(
            model_pattern="deepseek-r1.*",
            family="deepseek-r1",
            system_prompt_mode="omit",
            no_few_shot=True,
            no_cot_instructions=True,
        )
        assert p.system_prompt_mode == "omit"
        assert p.no_few_shot is True
        assert p.no_cot_instructions is True


class TestPromptAdapter:
    """PromptAdapter profile matching and adaptation tests."""

    @pytest.fixture
    def adapter(self):
        """Create an adapter with bundled profiles."""
        return PromptAdapter()

    def test_loads_bundled_profiles(self, adapter):
        """Bundled YAML profiles are loaded."""
        assert len(adapter.profiles) > 0
        families = {p.family for p in adapter.profiles}
        assert "claude" in families
        assert "grok" in families

    def test_resolve_claude(self, adapter):
        """Claude model resolves to claude profile."""
        profile = adapter.resolve_profile("claude-opus-4-5")
        assert profile.family == "claude"
        assert profile.system_prompt_mode == "separate_param"

    def test_resolve_grok(self, adapter):
        """Grok model resolves to grok profile."""
        profile = adapter.resolve_profile("grok-3")
        assert profile.family == "grok"

    def test_resolve_deepseek_r1(self, adapter):
        """DeepSeek R1 resolves correctly — omit system prompt."""
        profile = adapter.resolve_profile("deepseek-r1-70b")
        assert profile.family == "deepseek-r1"
        assert profile.system_prompt_mode == "omit"

    def test_resolve_unknown_falls_back(self, adapter):
        """Unknown model falls back to generic profile."""
        profile = adapter.resolve_profile("totally-unknown-model-xyz")
        assert profile.family == "generic"

    def test_resolve_devstral(self, adapter):
        """Devstral resolves with low code temperature."""
        profile = adapter.resolve_profile("devstral-2506")
        assert profile.family == "devstral"
        assert profile.code_temperature == 0.15

    def test_resolve_nemotron(self, adapter):
        """Nemotron resolves with reasoning temperature 1.0."""
        profile = adapter.resolve_profile("nemotron-49b")
        assert profile.family == "nemotron"
        assert profile.reasoning_temperature == 1.0


class TestAdapt:
    """Test prompt adaptation for different models."""

    @pytest.fixture
    def adapter(self):
        return PromptAdapter()

    def test_claude_separate_system(self, adapter):
        """Claude gets system as separate_param, not in messages."""
        result = adapter.adapt(
            "You are an agent.",
            "Hello, who are you?",
            "claude-opus-4-5",
            ModelTier.NUANCE,
        )
        assert result.system_param is not None
        assert "<instructions>" in result.system_param
        assert len(result.messages) == 1
        assert result.messages[0]["role"] == "user"
        assert "system_as_separate_param" in result.adaptations_applied

    def test_deepseek_r1_no_system(self, adapter):
        """DeepSeek R1 gets no system prompt — merged into user message."""
        result = adapter.adapt(
            "You are an agent.",
            "Explain quantum computing.",
            "deepseek-r1-70b",
            ModelTier.REASON,
        )
        assert result.system_param is None
        assert len(result.messages) == 1
        assert result.messages[0]["role"] == "user"
        # System prompt content is merged into user message
        assert "You are an agent." in result.messages[0]["content"]
        assert "Explain quantum computing." in result.messages[0]["content"]
        assert "omitted_system_prompt" in result.adaptations_applied

    def test_grok_standard_system(self, adapter):
        """Grok gets standard system message as first in array."""
        result = adapter.adapt(
            "You are an agent.",
            "Hello!",
            "grok-3",
            ModelTier.FAST,
        )
        assert result.system_param is None
        assert len(result.messages) == 2
        assert result.messages[0]["role"] == "system"
        assert result.messages[1]["role"] == "user"

    def test_devstral_code_temperature(self, adapter):
        """Devstral gets 0.15 temperature for CODE tier."""
        result = adapter.adapt(
            "System", "Write a function",
            "devstral-2506", ModelTier.CODE,
        )
        assert result.temperature == 0.15
        assert "set_temp_0.15" in result.adaptations_applied

    def test_nemotron_reasoning_temperature(self, adapter):
        """Nemotron gets 1.0 temperature for REASON tier."""
        result = adapter.adapt(
            "System", "Analyze this",
            "nemotron-49b", ModelTier.REASON,
        )
        assert result.temperature == 1.0

    def test_claude_thinking_config(self, adapter):
        """Claude gets thinking budget in extra_params."""
        result = adapter.adapt(
            "System", "Think deeply",
            "claude-opus-4-5", ModelTier.REASON,
        )
        assert "thinking" in result.extra_params
        assert result.extra_params["thinking"]["type"] == "enabled"
        assert result.extra_params["thinking"]["budget_tokens"] > 0

    def test_empty_system_prompt(self, adapter):
        """Empty system prompt works without errors."""
        result = adapter.adapt("", "Hello", "gpt-4o", ModelTier.FAST)
        # Should have user message only (no empty system)
        user_msgs = [m for m in result.messages if m["role"] == "user"]
        assert len(user_msgs) == 1

    def test_profile_used_recorded(self, adapter):
        """Profile used is recorded in the result."""
        result = adapter.adapt("S", "U", "claude-opus-4-5", ModelTier.FAST)
        assert result.profile_used == "claude"


class TestReload:
    """Test hot-reload functionality."""

    def test_reload_preserves_bundled(self):
        """Reloading re-reads bundled profiles."""
        adapter = PromptAdapter()
        count_before = len(adapter.profiles)
        adapter.reload_profiles()
        assert len(adapter.profiles) == count_before

    def test_update_profile(self):
        """Updating a profile changes its values."""
        adapter = PromptAdapter()
        adapter.update_profile("claude-.*", {"max_system_tokens": 8000})
        profile = adapter.resolve_profile("claude-opus-4-5")
        assert profile.max_system_tokens == 8000
