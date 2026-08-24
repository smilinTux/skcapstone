"""Tests for the deterministic package build entry point (card e1d24370)."""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "build_reproducible.py"
SPEC = importlib.util.spec_from_file_location("build_reproducible", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
build_reproducible = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_reproducible)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Build Test")
    _git(repo, "config", "user.email", "build@example.invalid")
    (repo / "tracked.txt").write_text("source\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = "1700000000 +0000"
    env["GIT_COMMITTER_DATE"] = "1700000000 +0000"
    _git(repo, "commit", "-q", "-m", "source", env=env)
    return repo


def _write_sdist(path: Path, mtime: int) -> None:
    payload = b"same source bytes\n"
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="source.tar", mtime=mtime) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("package/file.txt")
                member.size = len(payload)
                member.mtime = mtime
                archive.addfile(member, io.BytesIO(payload))


def test_source_metadata_is_derived_from_clean_commit(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    monkeypatch.setattr(build_reproducible, "_source_version", lambda _root: "1.2.3")

    epoch, version = build_reproducible.source_metadata(repo)

    assert epoch == 1700000000
    assert version == "1.2.3"


def test_source_metadata_rejects_tracked_changes(tmp_path, monkeypatch):
    repo = _committed_repo(tmp_path)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(build_reproducible, "_source_version", lambda _root: "1.2.3")

    with pytest.raises(build_reproducible.BuildError, match="tracked changes"):
        build_reproducible.source_metadata(repo)


def test_build_environment_overrides_only_deterministic_inputs(monkeypatch):
    monkeypatch.setenv("UNRELATED", "preserved")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "wrong")

    env = build_reproducible.build_environment(1700000000, "1.2.3")

    assert env["UNRELATED"] == "preserved"
    assert env["SOURCE_DATE_EPOCH"] == "1700000000"
    assert env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"] == "1.2.3"


def test_sdist_normalization_is_byte_identical(tmp_path):
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _write_sdist(first, 1700000100)
    _write_sdist(second, 1700000200)

    build_reproducible.normalize_sdist(first, 1700000000)
    build_reproducible.normalize_sdist(second, 1700000000)

    assert first.read_bytes() == second.read_bytes()


def test_source_version_rejects_empty_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(build_reproducible, "_command", lambda *_args, **_kwargs: "")

    with pytest.raises(build_reproducible.BuildError, match="version"):
        build_reproducible._source_version(tmp_path)


def test_run_build_propagates_build_failure(tmp_path, monkeypatch):
    failure = subprocess.CalledProcessError(2, ["python", "-m", "build"])

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(build_reproducible.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError) as caught:
        build_reproducible._run_build(tmp_path, tmp_path / "dist", {})

    assert caught.value is failure
