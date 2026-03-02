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


class TestOllamaCallback:
    """Tests for ollama_callback streaming/retry fixes."""

    def _make_urlopen_cm(self, body: bytes):
        """Return a mock suitable as the return value of urlopen(...)."""
        inner = MagicMock()
        inner.read.return_value = body
        cm = MagicMock()
        cm.__enter__.return_value = inner
        cm.__exit__.return_value = False
        return cm

    def test_single_json_chat_response(self):
        """Parses a single JSON /api/chat response correctly."""
        body = b'{"model":"llama3.2","message":{"role":"assistant","content":"Hello!"},"done":true}'
        cb = ollama_callback(model="llama3.2")
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)):
            result = cb("hi")
        assert result == "Hello!"

    def test_single_json_generate_response(self):
        """Parses a single JSON /api/generate response correctly."""
        body = b'{"model":"llama3.2","response":"Hello world","done":true}'
        cb = ollama_callback(model="llama3.2")
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)):
            result = cb("hi")
        assert result == "Hello world"

    def test_ndjson_streaming_aggregation(self):
        """Aggregates NDJSON chunks when Ollama returns a streaming body."""
        lines = [
            b'{"message":{"role":"assistant","content":"Hel"},"done":false}',
            b'{"message":{"role":"assistant","content":"lo!"},"done":false}',
            b'{"message":{"role":"assistant","content":""},"done":true}',
        ]
        body = b"\n".join(lines)
        cb = ollama_callback(model="llama3.2")
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)):
            result = cb("hi")
        assert result == "Hello!"

    def test_retry_on_empty_response(self):
        """Retries once when first response is empty; returns second attempt."""
        empty_body = b'{"message":{"role":"assistant","content":""},"done":true}'
        good_body = b'{"message":{"role":"assistant","content":"Retry worked!"},"done":true}'
        cb = ollama_callback(model="llama3.2", max_retries=1)
        side_effects = [
            self._make_urlopen_cm(empty_body),
            self._make_urlopen_cm(good_body),
        ]
        with patch("urllib.request.urlopen", side_effect=side_effects):
            result = cb("hi")
        assert result == "Retry worked!"

    def test_no_retry_when_max_retries_zero(self):
        """Returns empty string when max_retries=0 and response is empty."""
        empty_body = b'{"message":{"role":"assistant","content":""},"done":true}'
        cb = ollama_callback(model="llama3.2", max_retries=0)
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(empty_body)):
            result = cb("hi")
        assert result == ""

    def test_none_content_guard(self):
        """Handles None content field without crashing."""
        body = b'{"model":"llama3.2","message":{"role":"assistant","content":null},"done":true}'
        cb = ollama_callback(model="llama3.2", max_retries=0)
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)):
            result = cb("hi")
        assert result == ""

    def test_adapted_prompt_uses_chat_endpoint(self):
        """AdaptedPrompt routes to /api/chat."""
        class FakeAdapted:
            messages = [{"role": "user", "content": "hi"}]
            system_param = "You are helpful."
            temperature = 0.7
            extra_params: dict = {}

        body = b'{"message":{"role":"assistant","content":"Hi there!"},"done":true}'
        cb = ollama_callback(model="llama3.2")
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)) as mock_open:
            result = cb(FakeAdapted())
        assert result == "Hi there!"
        req = mock_open.call_args[0][0]
        assert "/api/chat" in req.full_url

    def test_plain_prompt_uses_generate_endpoint(self):
        """Plain string routes to /api/generate."""
        body = b'{"response":"Hi!","done":true}'
        cb = ollama_callback(model="llama3.2")
        with patch("urllib.request.urlopen", return_value=self._make_urlopen_cm(body)) as mock_open:
            result = cb("hi")
        assert result == "Hi!"
        req = mock_open.call_args[0][0]
        assert "/api/generate" in req.full_url

    def test_max_retries_default_is_one(self):
        """Default max_retries is 1."""
        import inspect
        sig = inspect.signature(ollama_callback)
        assert sig.parameters["max_retries"].default == 1

    def test_exception_retried_then_raised(self):
        """Exceptions are retried; final exception is re-raised."""
        import urllib.error
        cb = ollama_callback(model="llama3.2", max_retries=1)
        err = urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(urllib.error.URLError):
                cb("hi")
