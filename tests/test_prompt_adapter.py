"""Tests for the prompt adapter — per-model formatting."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.blueprints.schema import ModelTier
from skcapstone.prompt_adapter import (
    AdaptedPrompt,
    ModelProfile,
    PromptAdapter,
    _GENERIC_PROFILE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ollama_resp(
    family: str = "llama",
    param_size: str = "7B",
    quantization: str = "Q4_0",
    param_count: int = 7_000_000_000,
) -> MagicMock:
    """Return a mock context-manager that mimics urllib.request.urlopen."""
    data = {
        "details": {
            "family": family,
            "parameter_size": param_size,
            "quantization_level": quantization,
        },
        "model_info": {
            "general.architecture": family,
            "general.parameter_count": param_count,
        },
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode("utf-8")
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


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


class TestDetectModel:
    """Tests for Ollama /api/show auto-detection."""

    @pytest.fixture
    def adapter(self):
        return PromptAdapter()

    def test_detect_llama_returns_profile(self, adapter):
        """Successful Ollama response for a llama model yields a valid profile."""
        with patch("urllib.request.urlopen", return_value=_make_ollama_resp("llama", "7B", "Q4_0")):
            profile = adapter.detect_model("custom-llama:7b")

        assert profile is not None
        assert profile.family == "ollama-llama"
        assert "Auto-detected via Ollama" in profile.notes
        assert "param_size=7B" in profile.notes
        assert "quant=Q4_0" in profile.notes
        assert profile.model_pattern == re.escape("custom-llama:7b")

    def test_detect_qwen_enables_thinking(self, adapter):
        """Qwen family auto-detection sets thinking_enabled and toggle mode."""
        with patch("urllib.request.urlopen", return_value=_make_ollama_resp("qwen", "14B", "Q8_0")):
            profile = adapter.detect_model("qwen3-custom:14b")

        assert profile is not None
        assert profile.family == "ollama-qwen"
        assert profile.thinking_enabled is True
        assert profile.thinking_mode == "toggle"

    def test_detect_nemotron_reasoning_temp(self, adapter):
        """Nemotron auto-detection sets reasoning_temperature=1.0."""
        with patch("urllib.request.urlopen", return_value=_make_ollama_resp("nemotron", "49B", "Q4_K_M")):
            profile = adapter.detect_model("nemotron-custom:49b")

        assert profile is not None
        assert profile.reasoning_temperature == 1.0
        assert profile.thinking_enabled is True

    def test_detect_model_ollama_unreachable_returns_none(self, adapter):
        """When Ollama is unreachable detect_model returns None."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            profile = adapter.detect_model("mystery-model:latest")

        assert profile is None

    def test_detect_model_missing_fields_still_works(self, adapter):
        """Partial Ollama response (no model_info) gracefully returns a profile."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"details": {"family": "phi"}}).encode("utf-8")
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            profile = adapter.detect_model("phi4-mini:latest")

        assert profile is not None
        assert profile.family == "ollama-phi"
        assert profile.structure_format == "plain"

    def test_resolve_profile_falls_back_to_detect(self, adapter):
        """Unknown model triggers detect_model via resolve_profile."""
        with patch("urllib.request.urlopen", return_value=_make_ollama_resp("llama", "3B", "Q4_0")):
            profile = adapter.resolve_profile("orca-mini:3b")

        assert profile.family == "ollama-llama"

    def test_resolve_profile_caches_detected(self, adapter):
        """A detected profile is cached — second call skips HTTP."""
        with patch("urllib.request.urlopen", return_value=_make_ollama_resp("llama")) as mock_open:
            adapter.resolve_profile("my-new-model:latest")
            adapter.resolve_profile("my-new-model:latest")

        # urlopen called exactly once — second lookup hit the cache
        assert mock_open.call_count == 1

    def test_resolve_static_profile_not_overridden(self, adapter):
        """Known model uses static profile — detect_model is never called."""
        with patch.object(adapter, "detect_model") as mock_detect:
            profile = adapter.resolve_profile("claude-opus-4-5")

        mock_detect.assert_not_called()
        assert profile.family == "claude"
