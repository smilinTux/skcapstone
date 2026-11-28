"""Release retention: ``prune_releases`` and the prune after a successful apply."""

from __future__ import annotations

import os
import time
from pathlib import Path

from skcapstone.deploy import rollout

OLD = time.time() - 2 * 86400


def _paths(tmp_path: Path) -> rollout.Paths:
    return rollout.Paths(tmp_path / ".skenv", tmp_path / ".skenv-releases", tmp_path / "state")


def _release(paths: rollout.Paths, name: str, mtime: float = OLD) -> Path:
    d = paths.releases / name
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python").write_text("")
    os.utime(d, (mtime, mtime))
    return d


def _names(n: int) -> list[str]:
    return [f"202611{day:02d}-120000-{day:012x}" for day in range(1, n + 1)]


def _empty_proc(tmp_path: Path) -> Path:
    p = tmp_path / "proc"
    p.mkdir(exist_ok=True)
    return p


def test_keeps_the_newest_n_and_removes_the_rest(tmp_path):
    paths = _paths(tmp_path)
    for n in _names(6):
        _release(paths, n)
    os.symlink(paths.releases / _names(6)[-1], paths.venv_link)
    res = rollout.prune_releases(paths, keep=3, proc=_empty_proc(tmp_path))
    assert sorted(d.name for d in res.removed) == _names(6)[:3]
    assert sorted(p.name for p in paths.releases.iterdir()) == _names(6)[3:]


def test_current_previous_and_legacy_rollback_target_survive(tmp_path):
    paths = _paths(tmp_path)
    names = _names(5)
    for n in names:
        _release(paths, n)
    legacy = _release(paths, "legacy-20261101-000000")
    os.symlink(paths.releases / names[0], paths.venv_link)  # rolled back to an old one
    rollout.save_state(
        paths,
        {
            "current": {"release": str(paths.releases / names[0])},
            "previous": {"release": str(legacy)},
        },
    )
    res = rollout.prune_releases(paths, keep=1, proc=_empty_proc(tmp_path))
    assert {d.name for d in res.removed} == set(names[1:4])
    assert res.kept[names[0]].startswith("current")
    assert res.kept[legacy.name] == "previous in deploy state"
    assert res.kept[names[4]] == "newest 1"


def test_a_release_a_running_process_uses_is_kept(tmp_path):
    paths = _paths(tmp_path)
    names = _names(4)
    for n in names:
        _release(paths, n)
    proc = _empty_proc(tmp_path)
    (proc / "4242").mkdir()
    so = paths.releases / names[0] / "lib/python3.12/site-packages/_cffi.so"
    (proc / "4242" / "maps").write_text(f"7f00-7f01 r-xp 00000000 08:02 123 {so}\n")
    (proc / "4243").mkdir()
    argv = f"{paths.releases / names[1]}/bin/python\0-m\0x"
    (proc / "4243" / "cmdline").write_bytes(argv.encode())
    res = rollout.prune_releases(paths, keep=1, proc=proc)
    assert {d.name for d in res.removed} == {names[2]}
    assert res.kept[names[0]] == res.kept[names[1]] == "in use by a running process"


def test_dry_run_removes_nothing_and_young_or_odd_dirs_stay(tmp_path):
    paths = _paths(tmp_path)
    names = _names(3)
    for n in names:
        _release(paths, n)
    _release(paths, "20261031-120000-building", mtime=time.time())
    _release(paths, "scratch")
    res = rollout.prune_releases(paths, keep=1, dry_run=True, proc=_empty_proc(tmp_path))
    assert {d.name for d in res.removed} == set(names[:2])
    assert all(d.exists() for d in res.removed)
    assert res.kept["scratch"] == "not a release name"
    res = rollout.prune_releases(paths, keep=0, proc=_empty_proc(tmp_path))
    assert "20261031-120000-building" in res.kept
    assert res.kept["20261031-120000-building"] == "younger than an hour"


def test_missing_releases_dir_is_a_no_op(tmp_path):
    res = rollout.prune_releases(_paths(tmp_path), proc=_empty_proc(tmp_path))
    assert res.removed == [] and res.kept == {}


def test_keep_count_comes_from_the_environment():
    assert rollout.keep_releases_from_env({}) == rollout.KEEP_RELEASES == 3
    assert rollout.keep_releases_from_env({"SK_DEPLOY_KEEP_RELEASES": "5"}) == 5
    assert rollout.keep_releases_from_env({"SK_DEPLOY_KEEP_RELEASES": "0"}) == 0
    assert rollout.keep_releases_from_env({"SK_DEPLOY_KEEP_RELEASES": "lots"}) == 3


def test_auto_prune_is_off_at_zero_and_reports_errors_as_notes(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    for n in _names(5):
        _release(paths, n)
    monkeypatch.setenv("SK_DEPLOY_KEEP_RELEASES", "0")
    assert rollout._auto_prune(paths, lambda _m: None) == []
    assert len(list(paths.releases.iterdir())) == 5
    monkeypatch.setenv("SK_DEPLOY_KEEP_RELEASES", "2")
    monkeypatch.setattr(rollout, "releases_in_use", lambda *_a, **_k: set())
    logs: list[str] = []
    assert rollout._auto_prune(paths, logs.append) == []
    assert len(list(paths.releases.iterdir())) == 2 and "removed 3" in logs[0]


def test_deploy_prune_cli_dry_run(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from skcapstone.cli import deploy_cmd, main

    paths = _paths(tmp_path)
    for n in _names(4):
        _release(paths, n)
    monkeypatch.setattr(deploy_cmd, "_paths", lambda: paths)
    monkeypatch.setattr(rollout, "releases_in_use", lambda *_a, **_k: set())
    out = CliRunner().invoke(main, ["deploy", "prune", "--keep", "2", "--dry-run"])
    assert out.exit_code == 0, out.output
    assert "would remove 2 release(s)" in out.output
    assert len(list(paths.releases.iterdir())) == 4
