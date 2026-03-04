"""Benchmark SKComm transport throughput and latency.

Sends N messages of a configurable size through each available
SKComm transport. Reports p50/p95/p99 latency, throughput (msg/s),
and error rate.

The file transport is benchmarked via a local temp-dir loopback
(always available). Network transports (syncthing, nostr, websocket,
tailscale, webrtc) are probed with repeated health_check() calls
when available.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
import time
from typing import Optional

import click
from rich.table import Table

from ._common import console

# Mirrors skcomm.core.BUILTIN_TRANSPORTS — kept local to avoid hard dep
_BUILTIN_TRANSPORTS: dict[str, str] = {
    "file": "skcomm.transports.file",
    "syncthing": "skcomm.transports.syncthing",
    "nostr": "skcomm.transports.nostr",
    "websocket": "skcomm.transports.websocket",
    "tailscale": "skcomm.transports.tailscale",
    "webrtc": "skcomm.transports.webrtc",
}


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


def _percentile(data: list[float], p: float) -> Optional[float]:
    """Return the p-th percentile of *data*, or None if data is empty."""
    if not data:
        return None
    s = sorted(data)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return round(s[idx], 3)


# ---------------------------------------------------------------------------
# Per-transport benchmark runners
# ---------------------------------------------------------------------------


def _bench_file_loopback(count: int, size: int) -> dict:
    """Benchmark the file transport via a temp-dir send-loop loopback.

    Creates a temporary directory, configures the file transport to use
    it as both outbox and inbox, sends *count* messages of *size* bytes,
    and measures per-send latency from the returned SendResult.

    Args:
        count: Number of messages to send.
        size: Payload size in bytes.

    Returns:
        Result dict with status, mode, latency percentiles, throughput,
        and error counts.
    """
    tmp = tempfile.mkdtemp(prefix="skcomm_bench_")
    try:
        mod = importlib.import_module("skcomm.transports.file")
        factory = getattr(mod, "create_transport", None)
        if factory is None:
            return {"status": "error", "error": "no create_transport() in file transport"}

        transport = factory(outbox_path=tmp, inbox_path=tmp, archive=False)
        payload = os.urandom(size)
        latencies: list[float] = []
        errors = 0

        t_start = time.monotonic()
        for _ in range(count):
            try:
                result = transport.send(payload, "bench-self")
                latencies.append(result.latency_ms)
                if not result.success:
                    errors += 1
            except Exception:
                errors += 1

        total_s = time.monotonic() - t_start
        throughput = count / total_s if total_s > 0 else 0.0

        return {
            "status": "ok",
            "mode": "send-loop",
            "count": count,
            "errors": errors,
            "error_rate": errors / count,
            "throughput_msg_s": round(throughput, 1),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "p99_ms": _percentile(latencies, 99),
        }

    except ImportError:
        return {"status": "unavailable", "error": "skcomm not installed"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bench_health_checks(transport, count: int) -> dict:
    """Benchmark a transport by calling health_check() *count* times.

    Used for network transports where a local loopback send is not
    possible without a real peer. The health_check() latency_ms is
    collected as a proxy for round-trip time.

    Args:
        transport: A configured Transport instance with is_available()==True.
        count: Number of health_check iterations.

    Returns:
        Result dict with status, mode, latency percentiles, throughput,
        and error counts.
    """
    latencies: list[float] = []
    errors = 0

    t_start = time.monotonic()
    for _ in range(count):
        try:
            status = transport.health_check()
            if status.latency_ms is not None:
                latencies.append(float(status.latency_ms))
            if str(status.status) != "available":
                errors += 1
        except Exception:
            errors += 1

    total_s = time.monotonic() - t_start
    throughput = count / total_s if total_s > 0 else 0.0

    return {
        "status": "ok",
        "mode": "health-check",
        "count": count,
        "errors": errors,
        "error_rate": errors / count,
        "throughput_msg_s": round(throughput, 1),
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
    }


# ---------------------------------------------------------------------------
# Main benchmark orchestrator
# ---------------------------------------------------------------------------


def run_bench(
    transports: list[str],
    count: int,
    size: int,
    health_count: int,
) -> list[dict]:
    """Run benchmarks for all requested transports.

    Args:
        transports: Transport names to benchmark. Empty list = all.
        count: Number of messages for the file send-loop.
        size: Message payload size in bytes.
        health_count: Number of health_check() iterations for network transports.

    Returns:
        List of result dicts, one per transport.
    """
    selected = {
        name: path
        for name, path in _BUILTIN_TRANSPORTS.items()
        if not transports or name in transports
    }

    results: list[dict] = []

    for name, module_path in selected.items():
        r: dict = {"transport": name}

        # File transport — always benchmarked via send-loop loopback
        if name == "file":
            r.update(_bench_file_loopback(count, size))
            results.append(r)
            continue

        # Network transports — load module, check availability, health-check bench
        try:
            mod = importlib.import_module(module_path)
            factory = getattr(mod, "create_transport", None)
            if factory is None:
                r.update({"status": "unavailable", "error": f"no create_transport() in {module_path}"})
                results.append(r)
                continue
        except ImportError as exc:
            r.update({"status": "unavailable", "error": f"ImportError: {exc}"})
            results.append(r)
            continue
        except Exception as exc:
            r.update({"status": "unavailable", "error": str(exc)[:80]})
            results.append(r)
            continue

        try:
            transport = factory()
        except Exception as exc:
            r.update({"status": "unavailable", "error": f"init failed: {exc}"})
            results.append(r)
            continue

        try:
            available = transport.is_available()
        except Exception:
            available = False

        if not available:
            r.update({"status": "unavailable", "error": "not available"})
            results.append(r)
            continue

        r.update(_bench_health_checks(transport, health_count))
        results.append(r)

    return results


# ---------------------------------------------------------------------------
# Rich table renderer
# ---------------------------------------------------------------------------


def _render_table(results: list[dict], count: int, size: int, health_count: int) -> None:
    """Render benchmark results as a Rich table with a fastest-transport summary."""
    table = Table(
        title=f"SKComm Transport Benchmark  [{count} msgs × {size}B | health×{health_count}]",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Transport", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Mode", style="dim", no_wrap=True)
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("p99 (ms)", justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("Errors", justify="right")

    for r in results:
        status = r.get("status", "unknown")

        if status == "ok":
            status_str = "[green]ok[/]"
            mode_str = r.get("mode", "")
            p50 = str(r.get("p50_ms") or "—")
            p95 = str(r.get("p95_ms") or "—")
            p99 = str(r.get("p99_ms") or "—")
            tput = f"{r.get('throughput_msg_s', 0):.1f} msg/s"
            err_count = r.get("errors", 0)
            err_rate = r.get("error_rate", 0.0)
            err_str = "[green]0[/]" if err_count == 0 else f"[red]{err_count} ({err_rate:.0%})[/]"

        elif status == "unavailable":
            status_str = "[dim]unavailable[/]"
            err = r.get("error", "")
            mode_str = err[:40] if err else ""
            p50 = p95 = p99 = tput = "—"
            err_str = ""

        else:
            status_str = "[red]error[/]"
            mode_str = (r.get("error") or "")[:40]
            p50 = p95 = p99 = tput = "—"
            err_str = ""

        table.add_row(r["transport"], status_str, mode_str, p50, p95, p99, tput, err_str)

    console.print(table)

    ok_results = [r for r in results if r.get("status") == "ok" and r.get("p50_ms") is not None]
    if ok_results:
        fastest = min(ok_results, key=lambda r: r.get("p50_ms") or float("inf"))
        console.print(
            f"\n[bold]Fastest (p50):[/] [green]{fastest['transport']}[/] "
            f"— {fastest['p50_ms']} ms  ([dim]{fastest.get('mode')}[/])"
        )


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def register_bench_commands(main: click.Group) -> None:
    """Register the ``skcapstone bench`` command."""

    @main.command("bench")
    @click.option(
        "--count", "-n", default=100, show_default=True, type=int,
        help="Number of messages to send via the file transport loopback.",
    )
    @click.option(
        "--size", default=1024, show_default=True, type=int,
        help="Message payload size in bytes.",
    )
    @click.option(
        "--health-count", default=5, show_default=True, type=int,
        help="Number of health_check() iterations for network transports.",
    )
    @click.option(
        "--transport", "-t", "transports", multiple=True,
        help="Benchmark only this transport. Repeat to select multiple. Default: all.",
    )
    @click.option("--json-out", is_flag=True, help="Output raw JSON instead of a table.")
    def bench_cmd(
        count: int,
        size: int,
        health_count: int,
        transports: tuple,
        json_out: bool,
    ) -> None:
        """Benchmark SKComm transport throughput and latency.

        Sends COUNT messages of SIZE bytes through each available transport
        and reports p50/p95/p99 latency, throughput (msg/s), and error rate.

        \b
        Transport modes:
          file            — send-loop via temp-dir loopback (COUNT msgs × SIZE B)
          syncthing/nostr
          websocket       — health_check() × HEALTH_COUNT (no real peer needed)
          tailscale/webrtc

        \b
        Examples:
          skcapstone bench
          skcapstone bench -n 500 --size 4096
          skcapstone bench -t file -t syncthing
          skcapstone bench --json-out | jq '.[] | select(.status=="ok")'
        """
        selected = list(transports)

        if not json_out:
            scope = ", ".join(selected) if selected else "all transports"
            console.print(
                f"[bold]SKComm Transport Benchmark[/]  "
                f"scope={scope}  count={count}  size={size}B  "
                f"health-count={health_count}"
            )
            console.print()

        results = run_bench(
            transports=selected,
            count=count,
            size=size,
            health_count=health_count,
        )

        if json_out:
            click.echo(json.dumps(results, indent=2))
            return

        _render_table(results, count=count, size=size, health_count=health_count)
