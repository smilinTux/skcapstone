"""Tests for ``skcapstone benchmark`` — LLM backend latency benchmarking.

Covers:
- BenchmarkRunner.detect_backends() availability detection
- BenchmarkRunner.run_backend() per-backend call and error handling
- BenchmarkRunner.run_all() result aggregation (skip_unavailable logic)
- CLI rendering: table output and JSON output modes
- Passthrough always succeeds with zero external dependencies
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(prompt: str = "Hello", timeout: float = 5.0):
    from skcapstone.cli.benchmark import BenchmarkRunner

    return BenchmarkRunner(prompt=prompt, timeout=timeout)


def _ok(backend: str, ms: float = 42.0, model: str = "test-model") -> dict:
    return {"backend": backend, "status": "ok", "ms": ms, "model": model, "error": None}


def _unavail(backend: str) -> dict:
    return {"backend": backend, "status": "unavailable", "ms": None, "model": None, "error": None}


# ---------------------------------------------------------------------------
# detect_backends
# ---------------------------------------------------------------------------


class TestDetectBackends:
    """BenchmarkRunner.detect_backends() tests."""

    def test_passthrough_always_available(self):
        """passthrough must always be True regardless of env."""
        runner = _make_runner()
        with patch.object(runner, "_probe_ollama", return_value=False):
            detected = runner.detect_backends()
        assert detected["passthrough"] is True

    def test_ollama_available_when_probe_succeeds(self):
        """ollama is True when _probe_ollama returns True."""
        runner = _make_runner()
        with patch.object(runner, "_probe_ollama", return_value=True):
            detected = runner.detect_backends()
        assert detected["ollama"] is True

    def test_ollama_unavailable_when_probe_fails(self):
        """ollama is False when _probe_ollama returns False."""
        runner = _make_runner()
        with patch.object(runner, "_probe_ollama", return_value=False):
            detected = runner.detect_backends()
        assert detected["ollama"] is False

    def test_cloud_backend_requires_env_var(self):
        """Cloud backends are True only when the right env var is set."""
        runner = _make_runner()
        env_cases = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "grok": "XAI_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
            "nvidia": "NVIDIA_API_KEY",
        }
        with patch.object(runner, "_probe_ollama", return_value=False):
            # No keys set — all cloud backends False
            clean_env = {k: "" for k in env_cases.values()}
            with patch.dict(os.environ, clean_env, clear=False):
                # Temporarily remove the keys
                for var in env_cases.values():
                    os.environ.pop(var, None)
                detected = runner.detect_backends()
            for name in env_cases:
                assert detected[name] is False, f"{name} should be unavailable without key"

    def test_anthropic_available_with_env_var(self):
        """anthropic becomes available when ANTHROPIC_API_KEY is set."""
        runner = _make_runner()
        with patch.object(runner, "_probe_ollama", return_value=False), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            detected = runner.detect_backends()
        assert detected["anthropic"] is True

    def test_all_backends_listed(self):
        """detect_backends() covers all known backends."""
        from skcapstone.cli.benchmark import BACKENDS

        runner = _make_runner()
        with patch.object(runner, "_probe_ollama", return_value=False):
            detected = runner.detect_backends()
        for name in BACKENDS:
            assert name in detected, f"Backend '{name}' missing from detect_backends() result"


# ---------------------------------------------------------------------------
# run_backend — passthrough
# ---------------------------------------------------------------------------


class TestRunBackendPassthrough:
    """Passthrough backend — always ok, no external calls."""

    def test_passthrough_returns_ok(self):
        """_bench_passthrough returns status=ok with a float ms value."""
        runner = _make_runner()
        result = runner._bench_passthrough()
        assert result["status"] == "ok"
        assert isinstance(result["ms"], float)
        assert result["model"] == "mock"
        assert result["error"] is None

    def test_passthrough_ms_is_non_negative(self):
        """Passthrough latency must be >= 0."""
        runner = _make_runner()
        result = runner._bench_passthrough()
        assert result["ms"] >= 0.0

    def test_run_backend_passthrough_via_dispatch(self):
        """run_backend('passthrough') dispatches to _bench_passthrough."""
        runner = _make_runner()
        result = runner.run_backend("passthrough")
        assert result["backend"] == "passthrough"
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# run_backend — error handling
# ---------------------------------------------------------------------------


class TestRunBackendErrors:
    """run_backend() error-handling for failed or unsupported backends."""

    def test_run_backend_catches_exception(self):
        """Exceptions from _bench_* are caught and returned as status=error."""
        runner = _make_runner()
        with patch.object(runner, "_bench_ollama", side_effect=RuntimeError("connection refused")):
            result = runner.run_backend("ollama")
        assert result["status"] == "error"
        assert result["ms"] is None
        assert "connection refused" in result["error"]

    def test_run_backend_unsupported_name(self):
        """Unknown backend name returns status=unsupported."""
        runner = _make_runner()
        result = runner.run_backend("nonexistent_backend_xyz")
        assert result["status"] == "unsupported"
        assert result["ms"] is None

    def test_run_backend_error_truncates_long_message(self):
        """Long exception messages are truncated to 120 chars."""
        runner = _make_runner()
        long_msg = "x" * 200
        with patch.object(runner, "_bench_ollama", side_effect=RuntimeError(long_msg)):
            result = runner.run_backend("ollama")
        assert len(result["error"]) <= 120


# ---------------------------------------------------------------------------
# run_all — aggregation
# ---------------------------------------------------------------------------


class TestRunAll:
    """BenchmarkRunner.run_all() aggregation logic."""

    def test_run_all_skips_unavailable_by_default(self):
        """Unavailable backends appear with status=unavailable, not called."""
        runner = _make_runner()
        # All unavailable except passthrough
        unavail = {name: False for name in runner.detect_backends()}
        unavail["passthrough"] = True

        with patch.object(runner, "detect_backends", return_value=unavail), \
             patch.object(runner, "_bench_passthrough", return_value=_ok("passthrough")):
            results = runner.run_all()

        by_name = {r["backend"]: r for r in results}
        assert by_name["passthrough"]["status"] == "ok"
        for name, avail in unavail.items():
            if not avail:
                assert by_name[name]["status"] == "unavailable"

    def test_run_all_returns_one_result_per_backend(self):
        """run_all() always returns exactly len(BACKENDS) results."""
        from skcapstone.cli.benchmark import BACKENDS

        runner = _make_runner()
        with patch.object(runner, "detect_backends", return_value={n: False for n in BACKENDS}):
            # passthrough would still be run — override detect_backends to all-False
            # except passthrough
            avail = {n: False for n in BACKENDS}
            avail["passthrough"] = True
            with patch.object(runner, "detect_backends", return_value=avail), \
                 patch.object(runner, "_bench_passthrough", return_value=_ok("passthrough")):
                results = runner.run_all()

        assert len(results) == len(BACKENDS)

    def test_run_all_collects_errors(self):
        """Failing backends are included with status=error, not raised."""
        runner = _make_runner()
        avail = {n: False for n in runner.detect_backends()}
        avail["ollama"] = True

        with patch.object(runner, "detect_backends", return_value=avail), \
             patch.object(runner, "_bench_ollama", side_effect=OSError("network down")):
            results = runner.run_all()

        ollama_result = next(r for r in results if r["backend"] == "ollama")
        assert ollama_result["status"] == "error"
        assert "network down" in ollama_result["error"]

    def test_run_all_includes_ok_results(self):
        """Successful backends appear with status=ok and a float ms."""
        runner = _make_runner()
        avail = {n: False for n in runner.detect_backends()}
        avail["passthrough"] = True

        with patch.object(runner, "detect_backends", return_value=avail), \
             patch.object(runner, "_bench_passthrough", return_value=_ok("passthrough", ms=7.5)):
            results = runner.run_all()

        pt = next(r for r in results if r["backend"] == "passthrough")
        assert pt["status"] == "ok"
        assert pt["ms"] == 7.5


# ---------------------------------------------------------------------------
# Ollama benchmark (mocked HTTP)
# ---------------------------------------------------------------------------


class TestBenchOllama:
    """_bench_ollama with mocked urllib."""

    def test_bench_ollama_ok(self):
        """Successful Ollama call returns status=ok with ms and model."""
        runner = _make_runner()
        fake_response = json.dumps({"model": "llama3.2", "response": "Hi!"}).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = runner._bench_ollama()

        assert result["status"] == "ok"
        assert result["model"] == "llama3.2"
        assert isinstance(result["ms"], float)
        assert result["ms"] >= 0

    def test_bench_ollama_network_error_propagates(self):
        """Network errors from urlopen are re-raised (caught by run_backend)."""
        runner = _make_runner()

        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with pytest.raises(OSError):
                runner._bench_ollama()


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


class TestBenchmarkCLI:
    """CLI integration tests using CliRunner."""

    def _invoke(self, *args):
        from skcapstone.cli import main
        runner = CliRunner()
        return runner.invoke(main, ["benchmark", *args])

    def test_help(self):
        """benchmark --help exits 0 and shows key options."""
        result = self._invoke("--help")
        assert result.exit_code == 0
        assert "--prompt" in result.output
        assert "--timeout" in result.output
        assert "--json-out" in result.output

    def test_json_output(self):
        """--json-out emits valid JSON list."""
        from skcapstone.cli.benchmark import BenchmarkRunner

        fake_results = [_ok("passthrough"), _unavail("ollama")]
        with patch.object(BenchmarkRunner, "run_all", return_value=fake_results):
            result = self._invoke("--json-out")

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["backend"] == "passthrough"
        assert data[0]["status"] == "ok"

    def test_table_output_contains_backend_names(self):
        """Default table output contains backend names."""
        from skcapstone.cli.benchmark import BenchmarkRunner

        fake_results = [
            _ok("passthrough", ms=1.2),
            _unavail("ollama"),
        ]
        with patch.object(BenchmarkRunner, "run_all", return_value=fake_results):
            result = self._invoke()

        assert result.exit_code == 0
        assert "passthrough" in result.output
        assert "ollama" in result.output

    def test_custom_prompt_passed_to_runner(self):
        """--prompt value is forwarded to BenchmarkRunner."""
        from skcapstone.cli.benchmark import BenchmarkRunner

        with patch.object(BenchmarkRunner, "__init__", return_value=None) as mock_init, \
             patch.object(BenchmarkRunner, "run_all", return_value=[]):
            self._invoke("--prompt", "Ping", "--json-out")

        call_kwargs = mock_init.call_args
        assert call_kwargs is not None
        # prompt is passed as keyword or positional arg
        all_args = list(call_kwargs.args) + list(call_kwargs.kwargs.values())
        assert "Ping" in all_args

    def test_fastest_summary_shown_in_table(self):
        """When at least one backend succeeds, fastest backend is shown."""
        from skcapstone.cli.benchmark import BenchmarkRunner

        fake_results = [
            _ok("passthrough", ms=3.0),
            _ok("ollama", ms=200.0),
        ]
        with patch.object(BenchmarkRunner, "run_all", return_value=fake_results):
            result = self._invoke()

        assert result.exit_code == 0
        assert "Fastest" in result.output
        assert "passthrough" in result.output

    def test_no_backends_available_message(self):
        """When all backends are unavailable, a friendly message is shown."""
        from skcapstone.cli.benchmark import BenchmarkRunner, BACKENDS

        all_unavail = [_unavail(name) for name in BACKENDS]
        with patch.object(BenchmarkRunner, "run_all", return_value=all_unavail):
            result = self._invoke()

        assert result.exit_code == 0
        assert "No backends available" in result.output or "unavailable" in result.output
