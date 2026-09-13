"""Integration coverage for workspace runtime bootstrap lifecycle."""

from __future__ import annotations

import subprocess
from pathlib import Path

from skcapstone.fleet import workspace_runtime as runtime


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
    (repo / "README").write_text("integration\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_end_to_end_admit_create_reclaim_retire_and_successor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, head = _repo(tmp_path)
    workspaces = tmp_path / "workspaces"
    shared = tmp_path / "clawd" / "skcapstone-repos" / "skcapstone"
    shared.mkdir(parents=True)
    advert = runtime.advertise_runtime(
        home,
        workspaces_root=workspaces,
        buckets={"M": 2},
        mem_reserve_kb=1_000,
        swap_reserve_kb=100,
    )
    decision = runtime.admit_capacity(
        meminfo_text=("MemAvailable: 5000000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1500000 kB\n"),
        active_work=len(runtime.list_bindings(home)),
        bucket="M",
        bucket_capacity=advert.buckets["M"],
        mem_reserve_kb=advert.mem_reserve_kb,
        swap_reserve_kb=advert.swap_reserve_kb,
    )
    assert decision.admitted is True
    binding = runtime.create_isolated_workspace(
        home,
        repo=repo,
        workspaces_root=Path(advert.workspaces_root),
        card_id="e058c2c8",
        claim_revision="claim-revision-1",
        owner="cursor-e058c2c8",
        lane="codex",
        bucket="M",
        base_revision=head,
        shared_checkouts=[shared],
    )
    assert runtime.exact_claim_unit_mapping(
        card_id=binding.card_id,
        claim_revision=binding.claim_revision,
        owner=binding.owner,
        lane=binding.lane,
        unit=binding.unit,
    )
    agents = [
        {
            "name": "pi-terminal",
            "agent_status": "done",
            "workspace_id": "w9",
            "pane_id": "w9:p1",
            "cwd": binding.workspace,
            "state_change_seq": 3,
            "revision": 1,
        },
        {
            "name": "pi-live",
            "agent_status": "working",
            "workspace_id": "w9",
            "pane_id": "w9:p2",
            "cwd": str(shared),
            "state_change_seq": 4,
            "revision": 1,
        },
    ]
    recorded = {
        "pi-terminal": runtime.herdr_generation(agents[0]),
        "pi-live": runtime.herdr_generation(agents[1]),
    }
    reclaim = runtime.select_herdr_reclaims(agents, recorded)
    assert [row["name"] for row in reclaim] == ["pi-terminal"]
    runtime.retire_workspace(home, binding, repo=repo)
    assert runtime.get_binding(home, "e058c2c8") is None
    successor = runtime.create_isolated_workspace(
        home,
        repo=repo,
        workspaces_root=Path(advert.workspaces_root),
        card_id="e058c2c8",
        claim_revision="claim-revision-2",
        owner="cursor-e058c2c8",
        lane="codex",
        bucket="M",
        base_revision=head,
        shared_checkouts=[shared],
    )
    runtime.activate_successor(home, previous=binding, next_binding=successor)
    assert runtime.list_bindings(home) == [successor]


def test_source_module_has_no_literal_host_or_model_bindings() -> None:
    source = Path(runtime.__file__).read_text(encoding="utf-8").lower()
    forbidden = (
        "ziowk01",
        "chiap08",
        "chiap04",
        "lumina",
        "sk-codex",
        "openai",
        "anthropic",
    )
    for token in forbidden:
        assert token not in source, token
