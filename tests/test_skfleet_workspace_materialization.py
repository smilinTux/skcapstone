"""Tests for source workspace materialization before fleet claims."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(nodes) == set(names)
    namespace: dict[str, object] = {
        "Path": Path,
        "os": os,
        "re": re,
        "shutil": shutil,
        "subprocess": subprocess,
        "urlsplit": urlsplit,
    }
    exec(
        compile(ast.Module([nodes[name] for name in names], []), str(ROTATE), "exec"),
        namespace,
    )
    return namespace


def _helpers() -> dict[str, object]:
    return _load(
        "_resolve_workspace_root",
        "_source_workspace_spec",
        "_verify_source_workspace",
        "_materialize_worker_workspace",
    )


def test_preclaim_source_ref_accepts_only_remote_exact_ref() -> None:
    preflight = _load("_preclaim_source_ref")["_preclaim_source_ref"]
    calls: list[list[str]] = []

    def present(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "abc123\trefs/heads/main\n", "")

    preflight("https://github.com/smilinTux/sklegal", "refs/heads/main", present)
    assert calls == [
        [
            "git",
            "ls-remote",
            "--exit-code",
            "https://github.com/smilinTux/sklegal",
            "refs/heads/main",
        ]
    ]


def test_preclaim_source_ref_blocks_before_workspace_creation() -> None:
    preflight = _load("_preclaim_source_ref")["_preclaim_source_ref"]

    def absent(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "not found")

    with pytest.raises(ValueError, match="reconstructability_blocked"):
        preflight("https://github.com/smilinTux/sklegal", "missing", absent)


def test_source_card_requires_exact_repository_and_base() -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="repository"):
        spec({"links": {}}, ["source-only"])
    with pytest.raises(ValueError, match="base_ref"):
        spec(
            {"links": {"repository": "https://github.com/smilinTux/sklegal"}},
            ["source-only"],
        )


@pytest.mark.parametrize(
    "repository",
    [
        "http://github.com/smilinTux/sklegal",
        "https://token@github.com/smilinTux/sklegal",
        "https://github.com/smilinTux/sklegal?token=secret",
    ],
)
def test_source_card_rejects_unsafe_repository_links(repository: str) -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="credential-free https"):
        spec(
            {"links": {"repository": repository, "base_ref": "main"}},
            ["source-only"],
        )


def test_non_source_card_keeps_empty_working_directory(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    assert materialize(str(target), {}, []) == str(target)
    assert target.is_dir()


def test_source_checkout_is_cloned_atomically(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    target.mkdir()
    calls: list[list[str]] = []

    def clone(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ["--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/smilinTux/sklegal\n", ""
            )
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if command[-2:] == ["rev-parse", "FETCH_HEAD"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
            }
        },
        ["source-only"],
        runner=clone,
    )
    assert result == str(target)
    assert (target / ".git").is_dir()
    assert calls[0][1:7] == [
        "clone",
        "--quiet",
        "--single-branch",
        "--branch",
        "main",
        "--",
    ]


def test_failed_clone_leaves_no_partial_workspace(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"

    def fail(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        checkout = Path(command[-1])
        checkout.mkdir()
        (checkout / "partial").write_text("partial", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, "", "denied")

    with pytest.raises(ValueError, match="denied"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                }
            },
            ["source-only"],
            runner=fail,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".*.materializing-*"))


def test_interrupted_clone_cleans_up_and_can_retry(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    core = {
        "links": {
            "repository": "https://github.com/smilinTux/sklegal",
            "base_ref": "main",
        }
    }

    def interrupt(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).mkdir()
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        materialize(str(target), core, ["source-only"], runner=interrupt)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.materializing-*"))

    def retry(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/smilinTux/sklegal\n"
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    assert materialize(str(target), core, ["source-only"], runner=retry) == str(target)
    assert (target / ".git").is_dir()


def test_existing_dirty_workspace_is_preserved_and_rejected(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)

    def dirty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/smilinTux/sklegal\n"
        elif "status" in command:
            output = " M preserved.py\n"
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ValueError, match="custody state"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                }
            },
            ["source-only"],
            runner=dirty,
        )
    assert target.is_dir()


def test_existing_clean_source_workspace_is_reused(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)

    def clean(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/smilinTux/sklegal\n"
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
            }
        },
        ["source-only"],
        runner=clean,
    )
    assert result == str(target)


def test_configured_source_workspace_is_still_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "configured"
    (target / ".git").mkdir(parents=True)
    monkeypatch.setenv("SKFLEET_WORKSPACE", str(target))

    def wrong_origin(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/example/wrong\n"
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ValueError, match="does not match"):
        materialize(
            str(tmp_path / "unused"),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                }
            },
            ["source-only"],
            runner=wrong_origin,
        )


def test_materialization_precedes_claim_in_scheduler_source() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    preflight_at = source.index("_preclaim_source_ref(*_source_spec)")
    materialize_at = source.index("workspace=_materialize_worker_workspace(")
    claim_at = source.index(
        'claim=subprocess.run([SKC,"coord","claim",cid,"--agent",name]',
        materialize_at,
    )
    assert preflight_at < materialize_at < claim_at
    assert "os.makedirs(workspace,exist_ok=True)" not in source
