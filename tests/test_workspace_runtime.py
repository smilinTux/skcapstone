"""Deterministic tests for the workspace-runtime orchestration seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.fleet.workspace_runtime import (
    AdmissionDecision,
    WorkspaceRuntimeError,
    activate_successor,
    admit_capacity,
    advertise_runtime,
    exact_claim_unit_mapping,
    herdr_generation,
    plan_isolated_workspace,
    retire_binding,
    select_herdr_reclaims,
    worker_unit_name,
)


def _meminfo(
    *,
    available: int = 4_000_000,
    swap_total: int = 2_000_000,
    swap_free: int = 1_000_000,
) -> str:
    return (
        f"MemAvailable:   {available} kB\n"
        f"SwapTotal:      {swap_total} kB\n"
        f"SwapFree:       {swap_free} kB\n"
    )


def _binding(tmp_path: Path, card: str = "e058c2c8", rev: str = "rev-aaaaaaaaaaaa") -> object:
    return plan_isolated_workspace(
        workspaces_root=tmp_path / "workspaces",
        card_id=card,
        claim_revision=rev,
        owner="worker-a",
        lane="codex",
        bucket="M",
        base_revision="a" * 40,
        shared_checkouts=[tmp_path / "shared"],
    )


def test_advertise_runtime_is_host_and_model_neutral(tmp_path: Path) -> None:
    advert = advertise_runtime(workspaces_root=tmp_path / "workspaces", buckets={"S": 1, "M": 2})
    blob = str(advert.__dict__).lower()
    assert "lumina" not in blob and "ziowk" not in blob and "sk-codex" not in blob
    assert advert.buckets == {"S": 1, "M": 2}


def test_plan_refuses_shared_checkout_and_duplicate_occupancy(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    root = tmp_path / "workspaces"
    first = plan_isolated_workspace(
        workspaces_root=root,
        card_id="e058c2c8",
        claim_revision="rev-aaaaaaaaaaaa",
        owner="worker-a",
        lane="codex",
        bucket="M",
        base_revision="a" * 40,
        shared_checkouts=[shared],
    )
    assert Path(first.workspace).parent == root.resolve()
    assert first.unit == "skfleet-worker-codex-e058c2c8.service"
    with pytest.raises(WorkspaceRuntimeError, match="shared-checkout"):
        plan_isolated_workspace(
            workspaces_root=shared,
            card_id="deadbeef",
            claim_revision="rev-bbbbbbbbbbbb",
            owner="worker-b",
            lane="glm",
            bucket="S",
            base_revision="b" * 40,
            shared_checkouts=[shared],
        )
    with pytest.raises(WorkspaceRuntimeError, match="card already"):
        plan_isolated_workspace(
            workspaces_root=root,
            card_id="e058c2c8",
            claim_revision="rev-cccccccccccc",
            owner="worker-c",
            lane="codex",
            bucket="M",
            base_revision="a" * 40,
            occupied=[first],
        )


def test_herdr_reclaim_preserves_nonterminal_and_changed() -> None:
    done = {
        "name": "pi-done",
        "agent_status": "done",
        "workspace_id": "w1",
        "pane_id": "w1:p1",
        "cwd": "/work/skcapstone-aaaaaaa1-herdr",
        "state_change_seq": 10,
        "revision": 1,
    }
    agents = [
        done,
        {**done, "name": "pi-idle", "agent_status": "idle"},
        {**done, "name": "pi-working", "agent_status": "working"},
        {**done, "name": "pi-blocked", "agent_status": "blocked"},
        {**done, "name": "pi-unknown", "agent_status": "unknown"},
        {**done, "name": "pi-changed", "state_change_seq": 99},
    ]
    recorded = {row["name"]: herdr_generation({**done, "name": row["name"]}) for row in agents}
    recorded["pi-changed"] = herdr_generation(
        {**done, "name": "pi-changed", "state_change_seq": 10}
    )
    assert [row["name"] for row in select_herdr_reclaims(agents, recorded)] == ["pi-done"]


def test_admission_fails_closed_on_unsafe_memory_swap_and_capacity() -> None:
    assert admit_capacity(
        meminfo_text=_meminfo(), active_work=1, bucket="M", bucket_capacity=2
    ) == AdmissionDecision(True, "admitted", 4_000_000, 1_000_000, 1, 2)
    assert (
        admit_capacity(
            meminfo_text=_meminfo(available=100), active_work=0, bucket="M", bucket_capacity=2
        ).reason
        == "unsafe-memory"
    )
    assert (
        admit_capacity(
            meminfo_text=_meminfo(swap_free=10), active_work=0, bucket="M", bucket_capacity=2
        ).reason
        == "unsafe-swap"
    )
    assert (
        admit_capacity(
            meminfo_text=_meminfo(), active_work=2, bucket="M", bucket_capacity=2
        ).reason
        == "bucket-full"
    )


def test_exact_claim_unit_mapping() -> None:
    assert exact_claim_unit_mapping(
        card_id="e058c2c8",
        claim_revision="rev-1",
        owner="worker",
        lane="codex",
        unit=worker_unit_name("codex", "e058c2c8"),
    )
    assert not exact_claim_unit_mapping(
        card_id="e058c2c8",
        claim_revision="rev-1",
        owner="worker",
        lane="codex",
        unit="skfleet-worker-glm-e058c2c8.service",
    )


def test_concurrency_occupancy_and_rollback_successor(tmp_path: Path) -> None:
    first = _binding(tmp_path)
    second_plan = plan_isolated_workspace(
        workspaces_root=tmp_path / "workspaces",
        card_id="feedbeef",
        claim_revision="rev-oneeeeeeee",
        owner="owner-1",
        lane="codex",
        bucket="S",
        base_revision="c" * 40,
        occupied=[first],
    )
    # Only one binding per card: a racing second plan for the same card fails.
    with pytest.raises(WorkspaceRuntimeError, match="card already"):
        plan_isolated_workspace(
            workspaces_root=tmp_path / "workspaces",
            card_id="e058c2c8",
            claim_revision="rev-twooooooooo",
            owner="owner-2",
            lane="codex",
            bucket="S",
            base_revision="c" * 40,
            occupied=[first, second_plan],
        )
    with pytest.raises(WorkspaceRuntimeError, match="successor blocked"):
        activate_successor(previous=first, next_binding=first, occupied=[first])
    cleared = retire_binding(first, [first, second_plan])
    assert cleared == [second_plan]
    successor = plan_isolated_workspace(
        workspaces_root=tmp_path / "workspaces",
        card_id="e058c2c8",
        claim_revision="rev-newnewnewnew",
        owner="owner-3",
        lane="codex",
        bucket="M",
        base_revision="a" * 40,
        occupied=cleared,
    )
    activate_successor(previous=first, next_binding=successor, occupied=[*cleared, successor])


def test_source_module_has_no_literal_host_or_model_bindings() -> None:
    from skcapstone.fleet import workspace_runtime as runtime

    source = Path(runtime.__file__).read_text(encoding="utf-8").lower()
    for token in ("ziowk01", "chiap08", "lumina", "sk-codex", "openai", "anthropic"):
        assert token not in source, token
    assert "import subprocess" not in source
    assert "worktree add" not in source
