"""Deterministic tests for the workspace-runtime orchestration seam."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skcapstone.fleet.workspace_runtime import (
    AdmissionDecision,
    WorkspaceRuntimeError,
    activate_successor,
    admit_capacity,
    advertise_runtime,
    create_isolated_workspace,
    exact_claim_unit_mapping,
    herdr_generation,
    list_bindings,
    plan_isolated_workspace,
    retire_binding,
    retire_workspace,
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


def _advert(tmp_path: Path, **buckets: int):
    root = tmp_path / "workspaces"
    root.mkdir(exist_ok=True)
    return advertise_runtime(
        workspaces_root=root,
        buckets=buckets or {"M": 2},
        mem_reserve_kb=1_000,
        swap_reserve_kb=100,
    )


def _materialize(binding):
    target = Path(binding.workspace)
    target.mkdir(parents=True, exist_ok=False)
    return target.resolve()


def _retire(binding):
    target = Path(binding.workspace)
    if target.exists():
        target.rmdir()


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


def test_bootstrap_refuses_unsafe_headroom_before_registration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    advert = _advert(tmp_path, M=2)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(_meminfo(available=10), encoding="utf-8")
    with pytest.raises(WorkspaceRuntimeError, match="unsafe-memory"):
        create_isolated_workspace(
            home,
            advertisement=advert,
            card_id="e058c2c8",
            claim_revision="rev-aaaaaaaaaaaa",
            owner="worker-a",
            lane="codex",
            bucket="M",
            base_revision="a" * 40,
            meminfo_path=meminfo,
            materialize=_materialize,
            retire=_retire,
        )
    assert list_bindings(home) == []


def test_concurrent_same_bucket_bootstrap_cannot_exceed_capacity(tmp_path: Path) -> None:
    home = tmp_path / "home"
    advert = _advert(tmp_path, M=1)
    safe = _meminfo()

    def attempt(card: str, rev: str) -> object:
        return create_isolated_workspace(
            home,
            advertisement=advert,
            card_id=card,
            claim_revision=rev,
            owner=f"owner-{card}",
            lane="codex",
            bucket="M",
            base_revision="a" * 40,
            meminfo_reader=lambda: safe,
            materialize=_materialize,
            retire=_retire,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, "aaaaaaa1", "rev-oneeeeeeee"),
            pool.submit(attempt, "aaaaaaa2", "rev-twooooooooo"),
        ]
        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=30))
            except WorkspaceRuntimeError as exc:
                results.append(exc)
    successes = [row for row in results if not isinstance(row, Exception)]
    failures = [row for row in results if isinstance(row, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "admission refused" in str(failures[0])
    assert list_bindings(home) == successes


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
    home = tmp_path / "home"
    advert = _advert(tmp_path, M=2, S=2)
    first = create_isolated_workspace(
        home,
        advertisement=advert,
        card_id="e058c2c8",
        claim_revision="rev-aaaaaaaaaaaa",
        owner="worker-a",
        lane="codex",
        bucket="M",
        base_revision="a" * 40,
        meminfo_reader=lambda: _meminfo(),
        materialize=_materialize,
        retire=_retire,
    )
    second = create_isolated_workspace(
        home,
        advertisement=advert,
        card_id="feedbeef",
        claim_revision="rev-oneeeeeeee",
        owner="owner-1",
        lane="codex",
        bucket="S",
        base_revision="c" * 40,
        meminfo_reader=lambda: _meminfo(),
        materialize=_materialize,
        retire=_retire,
    )
    with pytest.raises(WorkspaceRuntimeError, match="successor blocked"):
        activate_successor(previous=first, next_binding=first, occupied=list_bindings(home))
    retire_workspace(home, first, retire=_retire)
    cleared = retire_binding(first, [first, second])
    assert cleared == [second]
    successor = create_isolated_workspace(
        home,
        advertisement=advert,
        card_id="e058c2c8",
        claim_revision="rev-newnewnewnew",
        owner="owner-3",
        lane="codex",
        bucket="M",
        base_revision="a" * 40,
        meminfo_reader=lambda: _meminfo(),
        materialize=_materialize,
        retire=_retire,
    )
    activate_successor(
        previous=first,
        next_binding=successor,
        occupied=list_bindings(home),
    )


def test_source_module_has_no_literal_host_or_model_bindings() -> None:
    from skcapstone.fleet import workspace_runtime as runtime

    source = Path(runtime.__file__).read_text(encoding="utf-8").lower()
    for token in ("ziowk01", "chiap08", "lumina", "sk-codex", "openai", "anthropic"):
        assert token not in source, token
