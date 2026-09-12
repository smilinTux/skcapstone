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

    preflight(
        "https://github.com/smilinTux/sklegal",
        "refs/heads/main",
        "a" * 40,
        "a" * 40,
        runner=present,
    )
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
        preflight(
            "https://github.com/smilinTux/sklegal",
            "missing",
            "a" * 40,
            "a" * 40,
            runner=absent,
        )


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


def test_legacy_sha_base_ref_normalizes_to_default_ref_and_exact_revision() -> None:
    spec = _helpers()["_source_workspace_spec"]
    revision = "a" * 40

    assert spec(
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": revision,
            },
            "meta": {"base_ref": "main", "base_revision": revision},
        },
        ["source-only"],
    ) == ("https://github.com/smilinTux/sklegal", "main", revision, revision)


def test_review_source_binding_checks_out_reviewed_head_not_producer_base() -> None:
    spec = _helpers()["_source_workspace_spec"]
    base = "a" * 40
    reviewed = "b" * 40

    assert spec(
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": base,
                "link_head_revision": reviewed,
            }
        },
        ["source-only", "review"],
    ) == ("https://github.com/smilinTux/sklegal", "main", base, reviewed)


def test_legacy_sha_base_ref_rejects_conflicting_exact_revision() -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="conflicts"):
        spec(
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "a" * 40,
                },
                "meta": {"base_revision": "b" * 40},
            },
            ["source-only"],
        )


def test_exact_revision_is_checked_out_after_named_ref_clone(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    revision = "c" * 40
    calls: list[list[str]] = []

    def clone(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/smilinTux/sklegal\n"
        elif "status" in command or "fetch" in command or "checkout" in command:
            output = ""
        elif "rev-parse" in command:
            output = revision + "\n"
        else:
            output = "f" * 40 + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    assert materialize(
        str(target),
        {
            "meta": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": revision,
            }
        },
        ["source-only"],
        runner=clone,
    ) == str(target)
    checkout_call = [
        "git",
        "-C",
        str(target.with_name(".worker.materializing-" + str(os.getpid()))),
        "checkout",
        "--quiet",
        "--detach",
        revision,
    ]
    assert checkout_call in calls
    fetch_at = next(index for index, command in enumerate(calls) if "fetch" in command)
    assert fetch_at < calls.index(checkout_call)


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
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": "a" * 40,
            }
        },
        ["source-only"],
        runner=clone,
    )
    assert result == str(target)
    assert (target / ".git").is_dir()
    assert calls[0][1:8] == [
        "clone",
        "--quiet",
        "--no-checkout",
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
                    "base_revision": "a" * 40,
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
            "base_revision": "a" * 40,
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
                    "base_revision": "a" * 40,
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
                "base_revision": "a" * 40,
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
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=wrong_origin,
        )


def test_configured_empty_review_workspace_materializes_reviewed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "configured"
    target.mkdir()
    monkeypatch.setenv("SKFLEET_WORKSPACE", str(target))
    base = "a" * 40
    reviewed = "b" * 40
    calls: list[list[str]] = []

    def clone(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-2:] == ["--get", "remote.origin.url"]:
            output = "https://github.com/smilinTux/sklegal\n"
        elif "status" in command or "fetch" in command or "checkout" in command:
            output = ""
        elif "rev-parse" in command:
            output = reviewed + "\n"
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    result = materialize(
        str(tmp_path / "unused"),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": base,
                "link_head_revision": reviewed,
            }
        },
        ["source-only", "review"],
        runner=clone,
    )

    assert result == str(target)
    assert ["git", "-C", str(target.with_name(
        ".configured.materializing-" + str(os.getpid())
    )), "checkout", "--quiet", "--detach", reviewed] in calls
    assert target.is_dir()


def test_configured_unknown_workspace_is_preserved_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "configured"
    target.mkdir()
    marker = target / "preserved.txt"
    marker.write_text("custody", encoding="utf-8")
    monkeypatch.setenv("SKFLEET_WORKSPACE", str(target))

    with pytest.raises(ValueError, match="exactly one Git checkout"):
        materialize(
            str(tmp_path / "unused"),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
        )
    assert marker.read_text(encoding="utf-8") == "custody"


def test_materialization_precedes_claim_in_scheduler_source() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    preflight_at = source.index("_preclaim_source_ref(*_source_spec)")
    materialize_at = source.index("_materialize_worker_workspace(", preflight_at)
    claim_at = source.index("claim=subprocess.run(", materialize_at)
    assert preflight_at < materialize_at < claim_at
    assert "os.makedirs(workspace,exist_ok=True)" not in source
