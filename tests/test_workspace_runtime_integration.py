"""Integration: create/retire real isolated worktrees through the runtime seam."""

from __future__ import annotations

import subprocess
from pathlib import Path

from skcapstone.fleet.workspace_runtime import (
    advertise_runtime,
    create_isolated_workspace,
    list_bindings,
    retire_workspace,
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
    return repo, _git(repo, "rev-parse", "HEAD")


def test_runtime_creates_and_retires_real_isolated_worktree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, head = _repo(tmp_path)
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    advert = advertise_runtime(
        workspaces_root=workspaces,
        buckets={"M": 2},
        mem_reserve_kb=1_000,
        swap_reserve_kb=100,
    )
    binding = create_isolated_workspace(
        home,
        advertisement=advert,
        card_id="e058c2c8",
        claim_revision="rev-aaaaaaaaaaaa",
        owner="worker-a",
        lane="codex",
        bucket="M",
        base_revision=head,
        repo=repo,
        meminfo_reader=lambda: (
            "MemAvailable: 4000000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1000000 kB\n"
        ),
    )
    workspace = Path(binding.workspace)
    assert workspace.is_dir()
    assert workspace.parent == workspaces.resolve()
    assert _git(workspace, "rev-parse", "HEAD") == head
    assert list_bindings(home) == [binding]
    retire_workspace(home, binding, repo=repo)
    assert list_bindings(home) == []
    assert not workspace.exists()
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert str(workspace) not in listed
