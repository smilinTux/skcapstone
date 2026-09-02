from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli import main
from skcapstone.source_worktree import (
    SourceWorktreeError,
    abandoned_worktrees,
    prepare_worktree,
    source_spec,
    validate_source_completion,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    git(checkout, "config", "user.name", "test")
    git(checkout, "config", "user.email", "test@example.invalid")
    (checkout / "README").write_text("base\n", encoding="utf-8")
    git(checkout, "add", "README")
    git(checkout, "commit", "-m", "base")
    git(checkout, "push", "-u", "origin", "HEAD:main")
    return checkout, remote, git(checkout, "rev-parse", "HEAD")


def core(card_id: str = "abc12345") -> dict[str, object]:
    return {
        "id": card_id,
        "kind": "task",
        "title": "source card",
        "initial_labels": ["source-only"],
        "meta": {"source": {"repository": "demo", "base_ref": "refs/remotes/origin/main"}},
    }


def enrollment(checkout: Path, remote: Path) -> list[dict[str, str]]:
    return [{"name": "demo", "checkout": str(checkout), "remote": str(remote)}]


def create_card(home: Path, card_id: str = "abc12345") -> None:
    home.mkdir(parents=True, exist_ok=True)
    CardStore(home).create(
        CardCore(
            id=card_id,
            kind="task",
            title="source card",
            created_by="test",
            initial_labels=["source-only"],
            meta=core(card_id)["meta"],
        )
    )


def point_verdict(home: Path, card_id: str, path: Path) -> None:
    events = home / "coordination/card_events"
    events.mkdir(parents=True, exist_ok=True)
    event = {
        "card_id": card_id,
        "action": "link",
        "link_key": "worktree",
        "link_value": str(path),
    }
    (events / "test.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


def test_source_card_requires_exact_metadata_and_unambiguous_enrollment(tmp_path: Path) -> None:
    checkout, remote, _ = repository(tmp_path)
    missing = core()
    missing["meta"] = {}
    with pytest.raises(SourceWorktreeError, match="meta.source"):
        source_spec(missing, tmp_path, enrollment(checkout, remote))
    with pytest.raises(SourceWorktreeError, match="ambiguous"):
        source_spec(core(), tmp_path, enrollment(checkout, remote) * 2)


def test_shared_checkout_dirt_fails_closed(tmp_path: Path) -> None:
    checkout, remote, _ = repository(tmp_path)
    (checkout / "README").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(SourceWorktreeError, match="dirty"):
        source_spec(core(), tmp_path, enrollment(checkout, remote))


def test_launcher_registers_clean_card_worktree_inside_absolute_workspace(
    tmp_path: Path,
) -> None:
    checkout, remote, head = repository(tmp_path)
    home = tmp_path / "home"
    workspace = home / "fleet/workspaces/agent"
    create_card(home)
    path = prepare_worktree(
        home,
        workspace,
        "abc12345",
        core(),
        "agent",
        enrollments=enrollment(checkout, remote),
    )
    assert path.parent == workspace.resolve()
    assert git(path, "status", "--porcelain") == ""
    event = CardStore(home)._read_events("abc12345")[-1]
    assert event["repository"] == "demo"
    assert event["base"] == event["head"] == head
    assert event["branch"] == "feat/abc12345-source-worktree"
    assert event["clean"] is True


def test_completion_rejects_unpointed_and_non_durable_change(tmp_path: Path) -> None:
    checkout, remote, _ = repository(tmp_path)
    home = tmp_path / "home"
    workspace = home / "fleet/workspaces/agent"
    create_card(home)
    path = prepare_worktree(
        home,
        workspace,
        "abc12345",
        core(),
        "agent",
        enrollments=enrollment(checkout, remote),
    )
    with pytest.raises(SourceWorktreeError, match="does not point"):
        validate_source_completion(home, "abc12345", core())
    point_verdict(home, "abc12345", path)
    (path / "README").write_text("candidate\n", encoding="utf-8")
    git(path, "add", "README")
    git(
        path,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "candidate",
    )
    with pytest.raises(SourceWorktreeError, match="remotely durable"):
        validate_source_completion(home, "abc12345", core())
    git(path, "push", "-u", "origin", "HEAD")
    validate_source_completion(home, "abc12345", core())


def test_inventory_preserves_registered_worktree(tmp_path: Path) -> None:
    checkout, remote, _ = repository(tmp_path)
    home = tmp_path / "home"
    create_card(home)
    path = prepare_worktree(
        home,
        home / "fleet/workspaces/agent",
        "abc12345",
        core(),
        "agent",
        enrollments=enrollment(checkout, remote),
    )
    assert abandoned_worktrees(home)[0]["worktree"] == str(path)
    assert path.exists()


def test_coord_create_requires_source_declaration(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--title",
            "source",
            "--tag",
            "source-only",
        ],
    )
    assert result.exit_code != 0
    assert "require --repository and --base-ref" in result.output
