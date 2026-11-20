"""``SK_DEPLOY_MANIFEST_URL``: one manifest repo per cluster, any git URL."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from skcapstone.deploy import ManifestUnavailableError, manifest_url, resolve_manifest

MANIFEST = {
    "schema": 1,
    "python": "3.12",
    "packages": {"skcapstone": {"source": "pypi", "version": "1.0.0", "repo": "o/skcapstone"}},
    "services": {},
    "smoke": ["true"],
}

KEYS = ("SK_DEPLOY_MANIFEST", "SK_DEPLOY_MANIFEST_URL", "SK_DEPLOY_MANIFEST_REPO")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No real ``~/.config/sk-deploy/env`` and no inherited manifest variables."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def _origin(path: Path, smoke: list[str]) -> Path:
    path.mkdir(parents=True)

    def git(*a: str) -> None:
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "manifest.yaml").write_text(yaml.safe_dump({**MANIFEST, "smoke": smoke}))
    git("add", ".")
    git("commit", "-qm", "one")
    return path


def test_manifest_url_precedence_env_over_file_url_over_repo(monkeypatch):
    conf = {"SK_DEPLOY_MANIFEST_URL": "git@alias:o/conf.git", "SK_DEPLOY_MANIFEST_REPO": "o/c"}
    assert manifest_url(conf) == "git@alias:o/conf.git"
    assert manifest_url({"SK_DEPLOY_MANIFEST_REPO": "o/c"}) == "https://github.com/o/c.git"
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_REPO", "o/env")
    assert manifest_url(conf) == "https://github.com/o/env.git"
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_URL", "git@alias:o/env.git")
    assert manifest_url(conf) == "git@alias:o/env.git"
    assert manifest_url({}) == "git@alias:o/env.git"


def test_manifest_url_none_when_nothing_is_configured():
    assert manifest_url({}) is None


def test_resolve_manifest_clones_any_git_url(tmp_path, monkeypatch):
    origin = _origin(tmp_path / "chi", ["echo chi"])
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_URL", str(origin))
    manifest, mid = resolve_manifest(tmp_path / "state")
    assert manifest.smoke == ["echo chi"] and len(mid) == 40


def test_resolve_manifest_follows_a_changed_url(tmp_path, monkeypatch):
    nor = _origin(tmp_path / "nor", ["echo nor"])
    chi = _origin(tmp_path / "chi", ["echo chi"])
    state = tmp_path / "state"
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_URL", str(nor))
    assert resolve_manifest(state)[0].smoke == ["echo nor"]
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_URL", str(chi))
    assert resolve_manifest(state)[0].smoke == ["echo chi"]
    origin = subprocess.run(
        ["git", "-C", str(state / "manifest-repo"), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == str(chi)


def test_resolve_manifest_reads_the_node_file(tmp_path):
    origin = _origin(tmp_path / "chi", ["echo chi"])
    conf = tmp_path / "home" / ".config" / "sk-deploy" / "env"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"# chi cluster\nSK_DEPLOY_MANIFEST_URL={origin}\n")
    assert resolve_manifest(tmp_path / "state")[0].smoke == ["echo chi"]


def test_resolve_manifest_without_a_source_names_both_keys(tmp_path):
    match = "SK_DEPLOY_MANIFEST_URL.*SK_DEPLOY_MANIFEST_REPO"
    with pytest.raises(ManifestUnavailableError, match=match):
        resolve_manifest(tmp_path / "state")
