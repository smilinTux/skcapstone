"""Qualification tests for declarative host-local MCP policy."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest
import tomllib

from skcapstone.mcp_policy import (
    MCPPolicy,
    load_host_policy,
    reconcile_codex_config,
    render_codex_config,
    stdio_process_slot,
)


def test_render_is_idempotent_host_specific_and_preserves_unrelated(tmp_path: Path) -> None:
    existing = 'model = "gpt-5"\n\n[notice]\nkeep = true\n'
    policy = MCPPolicy()
    first = render_codex_config(existing, policy, home=tmp_path, agent="jarvis")
    second = render_codex_config(first, policy, home=tmp_path, agent="jarvis")

    assert second == first
    parsed = tomllib.loads(first)
    assert parsed["model"] == "gpt-5"
    assert parsed["notice"]["keep"] is True
    assert parsed["approval_policy"] == "on-request"
    for name in ("skcapstone", "skmemory"):
        entry = parsed["mcp_servers"][name]
        assert entry["command"] == str(tmp_path / ".skenv" / "bin" / f"{name}-mcp")
        assert entry["env"] == {
            "SKAGENT": "jarvis",
            "SKCAPSTONE_AGENT": "jarvis",
            "SKMEMORY_AGENT": "jarvis",
        }
        assert entry["startup_timeout_sec"] == 20
        assert entry["tool_timeout_sec"] == 120


def test_dry_run_reports_drift_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('model = "keep"\n', encoding="utf-8")
    before = target.read_bytes()

    result = reconcile_codex_config(path=target, policy=MCPPolicy(), agent="jarvis", dry_run=True)

    assert result["action"] == "would-update"
    assert target.read_bytes() == before


def test_reconcile_second_run_has_no_drift(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    first = reconcile_codex_config(path=target, policy=MCPPolicy(), agent="jarvis")
    first_bytes = target.read_bytes()
    second = reconcile_codex_config(path=target, policy=MCPPolicy(), agent="jarvis")

    assert first["action"] == "created"
    assert second["action"] == "exists"
    assert target.read_bytes() == first_bytes


def test_host_overlay_accepts_only_bounded_non_secret_policy(tmp_path: Path) -> None:
    overlay = tmp_path / "mcp-host.json"
    overlay.write_text(
        json.dumps(
            {
                "approval_policy": "on-failure",
                "startup_timeout_sec": 30,
                "tool_timeout_sec": 180,
                "max_stdio_processes": 2,
            }
        ),
        encoding="utf-8",
    )
    overlay.chmod(0o600)
    assert load_host_policy(overlay) == MCPPolicy("on-failure", 30, 180, 2)

    overlay.write_text(json.dumps({"api_token": "do-not-copy"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported host overlay fields"):
        load_host_policy(overlay)


def _hold_slot(
    runtime_dir: str, ready: multiprocessing.Queue, release: multiprocessing.Event
) -> None:
    with stdio_process_slot(
        "test", policy=MCPPolicy(max_stdio_processes=1), runtime_dir=Path(runtime_dir)
    ) as slot:
        ready.put(slot)
        release.wait(5)


def test_process_ceiling_refuses_duplicate_and_reuses_released_slot(tmp_path: Path) -> None:
    ready: multiprocessing.Queue = multiprocessing.Queue()
    release = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_slot, args=(str(tmp_path), ready, release))
    process.start()
    assert ready.get(timeout=3) == 0
    with pytest.raises(RuntimeError, match="process ceiling reached"):
        with stdio_process_slot(
            "test", policy=MCPPolicy(max_stdio_processes=1), runtime_dir=tmp_path
        ):
            pass
    release.set()
    process.join(timeout=3)
    assert process.exitcode == 0
    with stdio_process_slot(
        "test", policy=MCPPolicy(max_stdio_processes=1), runtime_dir=tmp_path
    ) as slot:
        assert slot == 0
