"""Explicit machine identity cannot inherit or impersonate a human author."""

import subprocess

import pytest

from skcapstone.fleet.worker_git import identity, preflight

OWNER = "pi-codex-builder-node-worker-1234abcd"


def test_real_git_preflight_overrides_human_globals_without_writing_config(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    config = tmp_path / ".gitconfig"
    config.write_text("[user]\nname = Human Owner\nemail = human@example.org\n")
    before = config.read_bytes()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Another Human")
    command = [
        "/usr/bin/env",
        "-i",
        f"HOME={tmp_path}",
        "PATH=/usr/bin:/bin",
        *(f"{k}={v}" for k, v in identity(OWNER).items()),
        "/test/pi",
    ]
    preflight(command, tmp_path / "repo", OWNER)
    assert config.read_bytes() == before
    command.remove(f"GIT_COMMITTER_EMAIL={OWNER}@noreply.invalid")
    with pytest.raises(ValueError, match="differs"):
        preflight(command, tmp_path / "repo", OWNER)


@pytest.mark.parametrize("owner", ["human", "jarvis", "pi-x;echo-bad-1234abcd", "pi-x\n-1234abcd"])
def test_only_assigned_worker_identity_is_accepted(owner):
    with pytest.raises(ValueError):
        identity(owner)
