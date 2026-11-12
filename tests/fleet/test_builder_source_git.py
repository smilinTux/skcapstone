"""Exact source materialization must not inherit host Git transport or source settings."""

import subprocess

import pytest

from skcapstone.fleet.builder_dispatch import BuilderDispatchError, materialize_source


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    environment = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@invalid",
        "GIT_COMMITTER_EMAIL": "fixture@invalid",
    }

    def git(*args):
        return subprocess.check_output(["/usr/bin/git", *args], env=environment, text=True).strip()

    git("init", "--quiet", str(root))
    (root / "candidate.txt").write_text("exact synthetic source\n")
    git("-C", str(root), "add", "candidate.txt")
    git("-C", str(root), "commit", "--quiet", "-m", "synthetic fixture")
    head = git("-C", str(root), "rev-parse", "HEAD")
    return root, head, git


def test_preseed_ignores_host_url_rewrite_and_git_injection(source, tmp_path, monkeypatch):
    root, head, git = source
    repository = "https://github.com/example/synthetic.git"
    git("-C", str(root), "remote", "add", "origin", repository)
    global_config = tmp_path / "host-gitconfig"
    global_config.write_text('[url "ssh://invalid/"]\n insteadOf = https://github.com/\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.ssh://also-invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    assert materialize_source(dict(repository=repository, base_revision=head), root) == root
    assert (root / "candidate.txt").read_text() == "exact synthetic source\n"


@pytest.mark.parametrize("wrong", ["head", "origin"])
def test_actual_mismatch_still_refuses_without_touching_source(source, wrong):
    root, head, git = source
    repository = "https://github.com/example/synthetic.git"
    git("-C", str(root), "remote", "add", "origin", repository)
    request = dict(repository=repository, base_revision=head)
    request["base_revision" if wrong == "head" else "repository"] = (
        "f" * 40 if wrong == "head" else repository + "-wrong"
    )
    with pytest.raises(BuilderDispatchError, match="existing workspace"):
        materialize_source(request, root)
    assert git("-C", str(root), "rev-parse", "HEAD") == head
    assert (root / "candidate.txt").read_text() == "exact synthetic source\n"


def test_reconstruction_ignores_injected_directory_and_transport(source, tmp_path, monkeypatch):
    root, head, _ = source
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "unrelated"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.ssh://invalid/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "file://")
    workspace = tmp_path / "destination"
    assert (
        materialize_source(dict(repository=root.as_uri(), base_revision=head), workspace)
        == workspace
    )
    assert (workspace / "candidate.txt").read_text() == "exact synthetic source\n"
    assert not (tmp_path / "unrelated").exists()


def test_verification_ignores_injected_git_dir_and_path(source, tmp_path, monkeypatch):
    root, head, git = source
    repository = "https://github.com/example/synthetic.git"
    git("-C", str(root), "remote", "add", "origin", repository)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent.git"))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert materialize_source(dict(repository=repository, base_revision=head), root) == root
