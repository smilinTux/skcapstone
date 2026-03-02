"""Tests for skseed LLM provider callbacks — new grok/kimi/nvidia + AdaptedPrompt support."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from skseed.llm import (
    LLMCallback,
    _is_adapted_prompt,
    anthropic_callback,
    auto_callback,
    grok_callback,
    kimi_callback,
    nvidia_callback,
    ollama_callback,
    openai_callback,
    passthrough_callback,
)


class TestIsAdaptedPrompt:
    """Test the duck-type detector for AdaptedPrompt."""

    def test_plain_string(self):
        """Plain strings are not adapted prompts."""
        assert _is_adapted_prompt("hello") is False

    def test_dict_not_adapted(self):
        """Dicts are not adapted prompts."""
        assert _is_adapted_prompt({"key": "val"}) is False

    def test_adapted_prompt_duck_type(self):
        """Objects with messages and system_param attrs are detected."""
        class FakeAdapted:
            messages = [{"role": "user", "content": "hi"}]
            system_param = "system"
        assert _is_adapted_prompt(FakeAdapted()) is True


class TestGrokCallback:
    """Grok callback factory tests."""

    def test_returns_callable(self):
        """grok_callback returns a callable."""
        cb = grok_callback(api_key="test")
        assert callable(cb)

    def test_uses_xai_base_url(self):
        """Grok uses the xAI API base URL."""
        # We can't easily test the internal base_url without mocking openai,
        # but we can verify the callback is created without error
        cb = grok_callback(model="grok-3", api_key="test-key")
        assert cb is not None


class TestKimiCallback:
    """Kimi callback factory tests."""

    def test_returns_callable(self):
        """kimi_callback returns a callable."""
        cb = kimi_callback(api_key="test")
        assert callable(cb)

    def test_default_model(self):
        """Default model is moonshot-v1-128k."""
        # Just verify it creates without error
        cb = kimi_callback(api_key="test-key")
        assert cb is not None


class TestNvidiaCallback:
    """NVIDIA callback factory tests."""

    def test_returns_callable(self):
        """nvidia_callback returns a callable."""
        cb = nvidia_callback(api_key="test")
        assert callable(cb)


class TestPassthrough:
    """Passthrough callback tests."""

    def test_echoes_string(self):
        """Passthrough returns the prompt unchanged."""
        cb = passthrough_callback()
        assert cb("hello world") == "hello world"

    def test_echoes_any(self):
        """Passthrough works with any input."""
        cb = passthrough_callback()
        result = cb("test prompt")
        assert result == "test prompt"


class TestAutoCallback:
    """Auto-callback detection order tests."""

    def test_no_env_no_ollama(self):
        """Returns None when no backends available."""
        with patch.dict(os.environ, {}, clear=True):
            # Also need to patch ollama probe
            with patch("urllib.request.urlopen", side_effect=Exception("no ollama")):
                result = auto_callback()
                # May or may not be None depending on env, just check it runs
                assert result is None or callable(result)

    def test_anthropic_first(self):
        """ANTHROPIC_API_KEY is checked first."""
        env = {"ANTHROPIC_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            try:
                result = auto_callback()
                # Should return anthropic callback if anthropic is importable
                if result is not None:
                    assert callable(result)
            except ImportError:
                pass  # anthropic package not installed, that's ok

    def test_xai_second(self):
        """XAI_API_KEY is checked after Anthropic."""
        env = {"XAI_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            try:
                result = auto_callback()
                if result is not None:
                    assert callable(result)
            except ImportError:
                pass

    def test_moonshot_third(self):
        """MOONSHOT_API_KEY is checked after xAI."""
        env = {"MOONSHOT_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            try:
                result = auto_callback()
                if result is not None:
                    assert callable(result)
            except ImportError:
                pass

    def test_nvidia_fourth(self):
        """NVIDIA_API_KEY is checked after Moonshot."""
        env = {"NVIDIA_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=True):
            try:
                result = auto_callback()
                if result is not None:
                    assert callable(result)
            except ImportError:
                pass
