"""Focused tests for host-neutral workspace runtime bootstrap."""

from __future__ import annotations

import subprocess
import threading
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
    get_binding,
    herdr_generation,
    rollback_create,
    select_herdr_reclaims,
    worker_unit_name,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, head


def _meminfo(
    *,
    available: int = 4_000_000,
    swap_total: int = 2_000_000,
    swap_free: int = 1_000_000,
) -> str:
    return (
        f"MemTotal:       8000000 kB\n"
        f"MemAvailable:   {available} kB\n"
        f"SwapTotal:      {swap_total} kB\n"
        f"SwapFree:       {swap_free} kB\n"
    )


def test_advertise_runtime_is_host_and_model_neutral(tmp_path: Path) -> None:
    advert = advertise_runtime(
        tmp_path,
        workspaces_root=tmp_path / "workspaces",
        buckets={"S": 1, "M": 2},
    )
    payload = advert.__dict__
    blob = str(payload).lower()
    assert "lumina" not in blob
    assert "ziowk" not in blob
    assert "chiap" not in blob
    assert "sk-codex" not in blob
    assert advert.buckets == {"S": 1, "M": 2}


def test_create_and_retire_isolated_workspace_avoids_shared_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, head = _repo(tmp_path)
    shared = tmp_path / "shared-checkout"
    shared.mkdir()
    workspaces = tmp_path / "workspaces"
    binding = create_isolated_workspace(
        home,
        repo=repo,
        workspaces_root=workspaces,
        card_id="e058c2c8",
        claim_revision="rev-aaaaaaaa",
        owner="worker-a",
        lane="codex",
        bucket="M",
        base_revision=head,
        shared_checkouts=[shared],
    )
    assert Path(binding.workspace).parent == workspaces.resolve()
    assert binding.unit == "skfleet-worker-codex-e058c2c8.service"
    assert get_binding(home, "e058c2c8") == binding
    with pytest.raises(WorkspaceRuntimeError, match="shared-checkout"):
        create_isolated_workspace(
            home,
            repo=repo,
            workspaces_root=shared,
            card_id="deadbeef",
            claim_revision="rev-bbbbbbbb",
            owner="worker-b",
            lane="glm",
            bucket="S",
            base_revision=head,
            shared_checkouts=[shared],
        )


def test_herdr_reclaim_preserves_nonterminal_and_changed_generations() -> None:
    done = {
        "name": "pi-done",
        "agent_status": "done",
        "workspace_id": "w1",
        "pane_id": "w1:p1",
        "cwd": "/work/skcapstone-aaaaaaa1-herdr",
        "state_change_seq": 10,
        "revision": 1,
    }
    idle = {**done, "name": "pi-idle", "agent_status": "idle"}
    working = {**done, "name": "pi-working", "agent_status": "working"}
    blocked = {**done, "name": "pi-blocked", "agent_status": "blocked"}
    unknown = {**done, "name": "pi-unknown", "agent_status": "unknown"}
    changed = {**done, "name": "pi-changed", "state_change_seq": 99}
    recorded = {
        "pi-done": herdr_generation(done),
        "pi-idle": herdr_generation(idle),
        "pi-working": herdr_generation(working),
        "pi-blocked": herdr_generation(blocked),
        "pi-unknown": herdr_generation(unknown),
        "pi-changed": herdr_generation({**changed, "state_change_seq": 10}),
    }
    selected = select_herdr_reclaims(
        [done, idle, working, blocked, unknown, changed],
        recorded,
    )
    assert [row["name"] for row in selected] == ["pi-done"]


def test_admission_fails_closed_on_unsafe_memory_swap_and_capacity() -> None:
    ok = admit_capacity(
        meminfo_text=_meminfo(),
        active_work=1,
        bucket="M",
        bucket_capacity=2,
    )
    assert ok == AdmissionDecision(
        True,
        "admitted",
        mem_available_kb=4_000_000,
        swap_free_kb=1_000_000,
        active_work=1,
        bucket_capacity=2,
    )
    assert (
        admit_capacity(
            meminfo_text=_meminfo(available=100),
            active_work=0,
            bucket="M",
            bucket_capacity=2,
        ).reason
        == "unsafe-memory"
    )
    assert (
        admit_capacity(
            meminfo_text=_meminfo(swap_free=10),
            active_work=0,
            bucket="M",
            bucket_capacity=2,
        ).reason
        == "unsafe-swap"
    )
    assert (
        admit_capacity(
            meminfo_text=_meminfo(),
            active_work=2,
            bucket="M",
            bucket_capacity=2,
        ).reason
        == "bucket-full"
    )
    assert (
        admit_capacity(
            meminfo_text="MemTotal: 1 kB\n",
            active_work=0,
            bucket="M",
            bucket_capacity=2,
        ).admitted
        is False
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


def test_concurrent_creates_serialize_to_one_binding(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, head = _repo(tmp_path)
    workspaces = tmp_path / "workspaces"
    barrier = threading.Barrier(2)
    results: list[object] = []

    def attempt(suffix: str) -> None:
        barrier.wait(timeout=5)
        try:
            results.append(
                create_isolated_workspace(
                    home,
                    repo=repo,
                    workspaces_root=workspaces,
                    card_id="feedbeef",
                    claim_revision=f"rev-{suffix}",
                    owner=f"owner-{suffix}",
                    lane="codex",
                    bucket="S",
                    base_revision=head,
                )
            )
        except WorkspaceRuntimeError as exc:
            results.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, "one"), pool.submit(attempt, "two")]
        for future in futures:
            future.result(timeout=30)
    successes = [row for row in results if not isinstance(row, Exception)]
    failures = [row for row in results if isinstance(row, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert get_binding(home, "feedbeef") == successes[0]


def test_rollback_and_successor_safety(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, head = _repo(tmp_path)
    workspaces = tmp_path / "workspaces"
    first = create_isolated_workspace(
        home,
        repo=repo,
        workspaces_root=workspaces,
        card_id="abcd1234",
        claim_revision="rev-oldoldold",
        owner="owner-1",
        lane="qwen",
        bucket="L",
        base_revision=head,
    )
    with pytest.raises(WorkspaceRuntimeError, match="successor blocked"):
        activate_successor(home, previous=first, next_binding=first)
    rollback_create(home, first, repo=repo)
    assert get_binding(home, "abcd1234") is None
    second = create_isolated_workspace(
        home,
        repo=repo,
        workspaces_root=workspaces,
        card_id="abcd1234",
        claim_revision="rev-newnewnew",
        owner="owner-2",
        lane="qwen",
        bucket="L",
        base_revision=head,
    )
    activate_successor(home, previous=first, next_binding=second)
    assert get_binding(home, "abcd1234") == second
