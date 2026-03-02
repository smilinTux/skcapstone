"""Benchmark LLM response time across all available backends.

Sends a configurable prompt to each detected backend, measures wall-clock
latency, and renders a Rich table (or JSON) with results.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import click
from rich.table import Table

from ._common import AGENT_HOME, console

# Ordered list of all known backends (matches consciousness_loop.py fallback_chain)
BACKENDS: list[str] = ["ollama", "grok", "kimi", "nvidia", "anthropic", "openai", "passthrough"]


# ---------------------------------------------------------------------------
# BenchmarkRunner
# ---------------------------------------------------------------------------


class BenchmarkRunner:
    """Run latency benchmarks against LLM backends.

    Each backend is called with a simple prompt and the round-trip wall-clock
    time is recorded.  Cloud backends (anthropic, openai, grok, kimi, nvidia)
    require both an API key *and* the respective SDK to be installed.

    Args:
        prompt: The prompt string to send to every backend.
        timeout: Per-backend HTTP/API timeout in seconds.
    """

    def __init__(self, prompt: str = "Hello", timeout: float = 30.0) -> None:
        self.prompt = prompt
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def detect_backends(self) -> dict[str, bool]:
        """Return a mapping of backend name → availability.

        Ollama is probed via HTTP; cloud providers are available when the
        corresponding API key env-var is set; passthrough is always True.
        """
        return {
            "ollama": self._probe_ollama(),
            "grok": bool(os.environ.get("XAI_API_KEY")),
            "kimi": bool(os.environ.get("MOONSHOT_API_KEY")),
            "nvidia": bool(os.environ.get("NVIDIA_API_KEY")),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "passthrough": True,
        }

    def _probe_ollama(self) -> bool:
        """Return True if the Ollama API is reachable."""
        import urllib.request

        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            with urllib.request.urlopen(f"{host}/api/tags", timeout=2):
                return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Runner
    # ------------------------------------------------------------------

    def run_all(self, skip_unavailable: bool = True) -> list[dict]:
        """Benchmark all backends and return a list of result dicts.

        Result dict keys: ``backend``, ``status``, ``ms``, ``model``, ``error``.

        Args:
            skip_unavailable: When True, unavailable backends are included in
                the result list with ``status="unavailable"`` but are not
                actually called.
        """
        available = self.detect_backends()
        results: list[dict] = []
        for name in BACKENDS:
            if not available.get(name, False):
                results.append(
                    {"backend": name, "status": "unavailable", "ms": None, "model": None, "error": None}
                )
                continue
            results.append(self.run_backend(name))
        return results

    def run_backend(self, name: str) -> dict:
        """Benchmark a single backend by name.

        Returns a result dict with keys: ``backend``, ``status``, ``ms``,
        ``model``, ``error``.
        """
        method = getattr(self, f"_bench_{name}", None)
        if method is None:
            return {
                "backend": name,
                "status": "unsupported",
                "ms": None,
                "model": None,
                "error": f"No benchmark method for backend '{name}'",
            }
        try:
            return method()
        except Exception as exc:
            return {
                "backend": name,
                "status": "error",
                "ms": None,
                "model": None,
                "error": str(exc)[:120],
            }

    # ------------------------------------------------------------------
    # Per-backend implementations
    # ------------------------------------------------------------------

    def _bench_passthrough(self) -> dict:
        """Passthrough — instant in-process mock (always succeeds)."""
        t0 = time.perf_counter()
        _ = f"[passthrough] {self.prompt}"
        ms = round((time.perf_counter() - t0) * 1000, 3)
        return {"backend": "passthrough", "status": "ok", "ms": ms, "model": "mock", "error": None}

    def _bench_ollama(self) -> dict:
        """Ollama — local HTTP POST to /api/generate."""
        import urllib.request

        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
        payload = json.dumps(
            {"model": model, "prompt": self.prompt, "stream": False}
        ).encode()
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "backend": "ollama",
            "status": "ok",
            "ms": ms,
            "model": data.get("model", model),
            "error": None,
        }

    def _bench_anthropic(self) -> dict:
        """Anthropic Claude — requires ``anthropic`` SDK + ANTHROPIC_API_KEY."""
        try:
            import anthropic as _anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK not installed — run: pip install anthropic")

        model = "claude-haiku-4-5-20251001"
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        t0 = time.perf_counter()
        client.messages.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": self.prompt}],
        )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"backend": "anthropic", "status": "ok", "ms": ms, "model": model, "error": None}

    def _bench_openai(self) -> dict:
        """OpenAI — requires ``openai`` SDK + OPENAI_API_KEY."""
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai SDK not installed — run: pip install openai")

        model = "gpt-3.5-turbo"
        client = _openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        t0 = time.perf_counter()
        client.chat.completions.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": self.prompt}],
        )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"backend": "openai", "status": "ok", "ms": ms, "model": model, "error": None}

    def _bench_grok(self) -> dict:
        """xAI Grok — OpenAI-compatible API, requires ``openai`` SDK + XAI_API_KEY."""
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai SDK not installed — run: pip install openai")

        model = "grok-3-mini"
        client = _openai.OpenAI(
            api_key=os.environ["XAI_API_KEY"],
            base_url="https://api.x.ai/v1",
        )
        t0 = time.perf_counter()
        client.chat.completions.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": self.prompt}],
        )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"backend": "grok", "status": "ok", "ms": ms, "model": model, "error": None}

    def _bench_kimi(self) -> dict:
        """Moonshot Kimi — OpenAI-compatible API, requires ``openai`` SDK + MOONSHOT_API_KEY."""
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai SDK not installed — run: pip install openai")

        model = "moonshot-v1-8k"
        client = _openai.OpenAI(
            api_key=os.environ["MOONSHOT_API_KEY"],
            base_url="https://api.moonshot.cn/v1",
        )
        t0 = time.perf_counter()
        client.chat.completions.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": self.prompt}],
        )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"backend": "kimi", "status": "ok", "ms": ms, "model": model, "error": None}

    def _bench_nvidia(self) -> dict:
        """NVIDIA NIM — OpenAI-compatible API, requires ``openai`` SDK + NVIDIA_API_KEY."""
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError("openai SDK not installed — run: pip install openai")

        model = "meta/llama-3.1-8b-instruct"
        client = _openai.OpenAI(
            api_key=os.environ["NVIDIA_API_KEY"],
            base_url="https://integrate.api.nvidia.com/v1",
        )
        t0 = time.perf_counter()
        client.chat.completions.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": self.prompt}],
        )
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"backend": "nvidia", "status": "ok", "ms": ms, "model": model, "error": None}


# ---------------------------------------------------------------------------
# Rich table renderer
# ---------------------------------------------------------------------------


def _render_table(results: list[dict]) -> None:
    """Render benchmark results as a Rich table with a fastest-backend summary."""
    table = Table(title="LLM Backend Benchmark", show_header=True, header_style="bold magenta")
    table.add_column("Backend", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Model", style="dim")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Note")

    for r in results:
        status = r["status"]
        if status == "ok":
            status_str = "[green]ok[/]"
            ms_str = f"[green]{r['ms']}[/]"
        elif status == "unavailable":
            status_str = "[dim]unavailable[/]"
            ms_str = "[dim]—[/]"
        else:
            status_str = "[red]error[/]"
            ms_str = "[red]—[/]"

        table.add_row(
            r["backend"],
            status_str,
            r.get("model") or "—",
            ms_str,
            r.get("error") or "",
        )

    console.print(table)

    ok_results = [r for r in results if r["status"] == "ok"]
    if ok_results:
        fastest = min(ok_results, key=lambda r: r["ms"])
        console.print(
            f"\n[bold]Fastest:[/] [green]{fastest['backend']}[/] — {fastest['ms']} ms"
            f"  ([dim]{fastest['model']}[/])"
        )
    elif all(r["status"] == "unavailable" for r in results):
        console.print("[yellow]No backends available. Set API keys or start Ollama.[/]")


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def register_benchmark_commands(main: click.Group) -> None:
    """Register the ``skcapstone benchmark`` command."""

    @main.command("benchmark")
    @click.option(
        "--prompt", default="Hello", show_default=True,
        help="Prompt to send to each backend.",
    )
    @click.option(
        "--timeout", default=30.0, show_default=True, type=float,
        help="Per-backend timeout in seconds.",
    )
    @click.option(
        "--include-unavailable", is_flag=True,
        help="Include unavailable backends in output (they will show as 'unavailable').",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON instead of a table.")
    def benchmark_cmd(
        prompt: str,
        timeout: float,
        include_unavailable: bool,
        json_out: bool,
    ) -> None:
        """Benchmark LLM response time across all available backends.

        Sends PROMPT to each detected backend, measures latency, and
        reports results in a table.  Cloud backends require API key env vars
        (ANTHROPIC_API_KEY, OPENAI_API_KEY, XAI_API_KEY, MOONSHOT_API_KEY,
        NVIDIA_API_KEY).  Ollama is probed via HTTP on OLLAMA_HOST.
        """
        runner = BenchmarkRunner(prompt=prompt, timeout=timeout)

        if not json_out:
            console.print(
                f"[bold]Benchmarking LLM backends[/] — "
                f"prompt: [cyan]{prompt!r}[/]  timeout: {timeout}s"
            )
            console.print()

        results = runner.run_all(skip_unavailable=not include_unavailable)

        if json_out:
            click.echo(json.dumps(results, indent=2))
            return

        _render_table(results)
