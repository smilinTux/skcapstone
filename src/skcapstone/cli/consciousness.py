"""Consciousness commands: status, config, test, backends, profiles."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_consciousness_commands(main: click.Group) -> None:
    """Register the consciousness command group."""

    @main.group()
    def consciousness():
        """Consciousness loop — autonomous message processing.

        Manages the LLM-powered consciousness loop that lets agents
        respond to messages autonomously, route to the right model,
        and self-heal when things break.
        """

    @consciousness.command("status")
    @click.option("--port", default=7777, help="Daemon API port.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def consciousness_status(port: int, json_out: bool):
        """Show consciousness loop status from the running daemon."""
        import urllib.request
        import urllib.error

        try:
            url = f"http://127.0.0.1:{port}/consciousness"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError):
            if json_out:
                click.echo(json.dumps({"error": "Daemon not reachable"}))
            else:
                console.print("[yellow]Daemon is not running or unreachable.[/]")
            return

        if json_out:
            click.echo(json.dumps(data, indent=2))
            return

        from rich.panel import Panel
        from rich.table import Table

        enabled = data.get("enabled", False)
        color = "green" if enabled else "yellow"

        inbox_watcher = data.get("inbox_watcher", "unknown")

        console.print()
        console.print(
            Panel(
                f"Enabled: [bold {color}]{enabled}[/]\n"
                f"Messages processed: [bold]{data.get('messages_processed', 0)}[/]\n"
                f"Responses sent: [bold]{data.get('responses_sent', 0)}[/]\n"
                f"Errors: [bold]{data.get('errors', 0)}[/]\n"
                f"Inotify active: {data.get('inotify_active', False)}\n"
                f"Inbox watcher mode: [dim]{inbox_watcher}[/]\n"
                f"Last activity: {data.get('last_activity') or '[dim]never[/]'}",
                title=f"[{color}]Consciousness Loop[/]",
                border_style=color,
            )
        )

        backends = data.get("backends", {})
        if backends:
            table = Table(title="LLM Backends")
            table.add_column("Backend", style="bold")
            table.add_column("Status")
            for name, available in sorted(backends.items()):
                status_str = "[green]available[/]" if available else "[red]unavailable[/]"
                table.add_row(name, status_str)
            console.print(table)

        console.print()

    @consciousness.command("config")
    @click.option("--init", "do_init", is_flag=True, help="Write default config YAML.")
    @click.option("--show", "do_show", is_flag=True, help="Show current config.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def consciousness_config(do_init: bool, do_show: bool, home: str):
        """Manage consciousness configuration."""
        home_path = Path(home).expanduser()

        if do_init:
            from ..consciousness_config import write_default_config
            path = write_default_config(home_path)
            console.print(f"\n  [green]Config written to[/] {path}\n")
            return

        if do_show:
            from ..consciousness_config import load_consciousness_config
            config = load_consciousness_config(home_path)
            console.print()
            for key, value in config.model_dump().items():
                console.print(f"  [bold]{key}[/]: {value}")
            console.print()
            return

        # Default: show help
        click.echo(click.get_current_context().get_help())

    @consciousness.command("test")
    @click.argument("message")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def consciousness_test(message: str, home: str):
        """Test the LLM pipeline end-to-end with a message.

        Builds the full agent system prompt, routes via the model
        router, and returns the LLM response.
        """
        home_path = Path(home).expanduser()

        from ..consciousness_config import load_consciousness_config
        from ..consciousness_loop import (
            ConsciousnessConfig,
            LLMBridge,
            SystemPromptBuilder,
            _classify_message,
        )

        console.print("\n  [cyan]Testing consciousness pipeline...[/]")

        config = load_consciousness_config(home_path)
        bridge = LLMBridge(config)
        builder = SystemPromptBuilder(home_path, config.max_context_tokens)

        # Classify
        signal = _classify_message(message)
        console.print(f"  Signal: tags={signal.tags}, tokens~{signal.estimated_tokens}")

        # Build system prompt
        system_prompt = builder.build()
        console.print(f"  System prompt: {len(system_prompt)} chars")

        # Generate
        console.print("  [dim]Generating response...[/]")
        try:
            response = bridge.generate(system_prompt, message, signal)
            console.print(f"\n  [green]Response ({len(response)} chars):[/]\n")
            console.print(f"  {response}\n")
        except Exception as exc:
            console.print(f"\n  [red]Error:[/] {exc}\n")
            sys.exit(1)

    @consciousness.command("backends")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def consciousness_backends(json_out: bool):
        """Show which LLM backends are reachable."""
        from ..consciousness_loop import ConsciousnessConfig, LLMBridge

        config = ConsciousnessConfig()
        bridge = LLMBridge(config)
        health = bridge.health_check()

        if json_out:
            click.echo(json.dumps(health, indent=2))
            return

        from rich.table import Table

        console.print()
        table = Table(title="LLM Backend Availability")
        table.add_column("Backend", style="bold")
        table.add_column("Status")
        table.add_column("Source")

        backend_sources = {
            "ollama": "localhost:11434 or OLLAMA_HOST",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "grok": "XAI_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
            "nvidia": "NVIDIA_API_KEY",
            "passthrough": "always available (echo mode)",
        }

        for name, available in sorted(health.items()):
            status_str = "[green]AVAILABLE[/]" if available else "[red]UNAVAILABLE[/]"
            source = backend_sources.get(name, "")
            table.add_row(name, status_str, source)

        console.print(table)

        try:
            import watchdog  # noqa: F401
            inotify_line = "[green]inotify: active (watchdog installed)[/]"
        except ImportError:
            inotify_line = "[yellow]inotify: degraded (polling only, install watchdog)[/]"
        console.print(f"  {inotify_line}")
        console.print()

    @consciousness.command("quality")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    @click.option("--port", default=7777, help="Daemon API port (tries live daemon first).")
    def consciousness_quality(home: str, json_out: bool, port: int):
        """Show average response quality scores for today.

        Reads quality metrics from today's daily metrics file.
        Dimensions: length appropriateness, coherence, latency, overall.
        """
        import urllib.request
        import urllib.error
        from datetime import datetime, timezone
        from pathlib import Path

        quality: dict = {}

        # Try live daemon first — it may have unsaved in-memory data
        try:
            url = f"http://127.0.0.1:{port}/consciousness"
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
                if "quality_avg" in data:
                    quality = data["quality_avg"]
        except Exception:
            pass  # Daemon unreachable — fall through to file

        # Fall back to daily metrics file
        if not quality:
            home_path = Path(home).expanduser()
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            daily = home_path / "metrics" / "daily" / f"{date_str}.json"
            if daily.exists():
                try:
                    file_data = json.loads(daily.read_text(encoding="utf-8"))
                    quality = file_data.get("quality_avg", {})
                except Exception:
                    pass

        if not quality or quality.get("count", 0) == 0:
            if json_out:
                click.echo(json.dumps({"count": 0, "message": "No quality data for today"}))
            else:
                console.print("\n  [yellow]No quality metrics recorded today.[/]\n")
            return

        if json_out:
            click.echo(json.dumps(quality, indent=2))
            return

        from rich.panel import Panel
        from rich.table import Table

        count = quality.get("count", 0)
        overall = quality.get("overall", 0.0)
        color = "green" if overall >= 0.7 else ("yellow" if overall >= 0.4 else "red")

        def _bar(score: float) -> str:
            filled = int(round(score * 10))
            return "█" * filled + "░" * (10 - filled)

        table = Table(title=f"Response Quality — {count} response(s) today", show_header=True)
        table.add_column("Dimension", style="bold")
        table.add_column("Score", justify="right")
        table.add_column("Bar")
        table.add_column("Description", style="dim")

        dims = [
            ("Length", quality.get("length", 0.0), "Response is appropriate length for question"),
            ("Coherence", quality.get("coherence", 0.0), "Keywords from question appear in response"),
            ("Latency", quality.get("latency", 0.0), "Response generated quickly"),
            ("Overall", overall, "Weighted average (coherence 40%, length 30%, latency 30%)"),
        ]

        for name, score, desc in dims:
            s_color = "green" if score >= 0.7 else ("yellow" if score >= 0.4 else "red")
            table.add_row(
                name,
                f"[{s_color}]{score:.3f}[/]",
                f"[{s_color}]{_bar(score)}[/]",
                desc,
            )

        console.print()
        console.print(table)
        console.print(
            Panel(
                f"Overall quality: [{color}]{overall:.3f}[/]  ({count} responses today)",
                border_style=color,
            )
        )
        console.print()

    @consciousness.command("profiles")
    @click.option("--show", "do_show", is_flag=True, help="List all profiles.")
    @click.option("--stale", "do_stale", is_flag=True, help="Show profiles older than 90 days.")
    @click.option("--update", "do_update", is_flag=True, help="Re-apply bundled defaults.")
    @click.option("--export", "do_export", is_flag=True, help="Export profiles as YAML.")
    def consciousness_profiles(do_show: bool, do_stale: bool, do_update: bool, do_export: bool):
        """Manage model prompt profiles."""
        from ..prompt_adapter import PromptAdapter, _BUNDLED_PROFILES

        adapter = PromptAdapter()

        if do_export:
            import yaml
            profiles_data = [p.model_dump() for p in adapter.profiles]
            click.echo(yaml.dump({"profiles": profiles_data}, default_flow_style=False))
            return

        if do_update:
            adapter.reload_profiles()
            console.print(f"\n  [green]Reloaded {len(adapter.profiles)} profiles from bundled defaults.[/]\n")
            return

        if do_stale:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            stale = []
            for p in adapter.profiles:
                if not p.last_updated:
                    stale.append((p.family, "never"))
                    continue
                try:
                    updated = datetime.fromisoformat(p.last_updated)
                    if hasattr(updated, "tzinfo") and updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    age = (now - updated).days
                    if age > 90:
                        stale.append((p.family, f"{age}d ago"))
                except (ValueError, TypeError):
                    stale.append((p.family, "invalid date"))

            if stale:
                console.print("\n  [yellow]Stale profiles (>90 days):[/]")
                for family, age in stale:
                    console.print(f"    {family}: {age}")
            else:
                console.print("\n  [green]All profiles are fresh.[/]")
            console.print()
            return

        # Default: --show
        from rich.table import Table
        table = Table(title="Model Profiles")
        table.add_column("Family", style="bold")
        table.add_column("Pattern")
        table.add_column("System Mode")
        table.add_column("Format")
        table.add_column("Thinking")
        table.add_column("Updated")

        for p in adapter.profiles:
            thinking = p.thinking_mode if p.thinking_enabled else "-"
            table.add_row(
                p.family,
                p.model_pattern,
                p.system_prompt_mode,
                p.structure_format,
                thinking,
                p.last_updated or "[dim]never[/]",
            )

        console.print()
        console.print(table)
        console.print()

    @consciousness.command("fallbacks")
    @click.option("--limit", default=20, show_default=True, help="Number of recent events to show.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    @click.option("--clear", "do_clear", is_flag=True, help="Clear all stored fallback events.")
    def consciousness_fallbacks(limit: int, json_out: bool, do_clear: bool):
        """Show LLM fallback history — when and why the agent degraded.

        Each entry records the primary model that failed, the backend that
        was tried next, whether the fallback succeeded, and the reason.
        Events are stored in ~/.skcapstone/fallbacks.json.
        """
        from ..fallback_tracker import FallbackTracker

        tracker = FallbackTracker()

        if do_clear:
            count = tracker.clear()
            console.print(f"\n  [green]Cleared {count} fallback event(s).[/]\n")
            return

        events = tracker.load_events(limit=limit)

        if json_out:
            click.echo(
                json.dumps([e.model_dump() for e in events], indent=2)
            )
            return

        if not events:
            console.print(
                f"\n  [dim]No fallback events recorded yet.[/]\n"
                f"  Events are written to: {tracker.path}\n"
            )
            return

        from rich.table import Table

        table = Table(title=f"LLM Fallback History (last {len(events)})", show_lines=True)
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Primary", style="bold")
        table.add_column("→ Fallback")
        table.add_column("OK?", justify="center")
        table.add_column("Reason")

        for evt in events:
            ts = evt.timestamp[:19].replace("T", " ")  # YYYY-MM-DD HH:MM:SS
            ok_str = "[green]✓[/]" if evt.success else "[red]✗[/]"
            primary = f"{evt.primary_model}\n[dim]{evt.primary_backend}[/]"
            fallback = f"{evt.fallback_model}\n[dim]{evt.fallback_backend}[/]"
            table.add_row(ts, primary, fallback, ok_str, evt.reason)

        console.print()
        console.print(table)
        console.print(f"  [dim]Source: {tracker.path}[/]")
        console.print()
