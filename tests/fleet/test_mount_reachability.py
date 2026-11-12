"""Unreachable network mounts read as missing paths, never as a hang."""

import subprocess
from types import SimpleNamespace

import pytest

from skcapstone.fleet import mount_reachability as mr


@pytest.fixture(autouse=True)
def fresh_cache():
    mr._PROBED.clear()
    yield
    mr._PROBED.clear()


@pytest.fixture
def table(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "10.0.0.111:/share/cloud/onedrive /mnt/cloud/onedrive nfs4 rw,hard 0 0\n"
        "10.0.0.111:/share/common /mnt/common nfs4 rw,hard 0 0\n"
    )
    return str(path)


def test_local_path_never_probes(table):
    calls = []
    assert mr.path_reachable("/home/u/x", table=table, run=lambda *a, **k: calls.append(a))
    assert calls == []


def test_hung_nfs_mount_is_unreachable_and_probed_once(table):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    bundle = "/mnt/cloud/onedrive/projects/x/FE4DB8C5-SCANNER-CANDIDATE.bundle"
    assert not mr.path_reachable(bundle, table=table, run=run)
    assert not mr.path_reachable("/mnt/cloud/onedrive/other", table=table, run=run)
    assert calls == [["stat", "-t", "/mnt/cloud/onedrive"]]


def test_answering_nfs_mount_is_reachable(table):
    ok = lambda *a, **k: SimpleNamespace(returncode=0)  # noqa: E731
    assert mr.path_reachable("/mnt/common/f", table=table, run=ok)


def test_prefix_is_not_a_false_match(table):
    calls = []
    assert mr.path_reachable(
        "/mnt/cloud/onedrive-local/f", table=table, run=lambda *a, **k: calls.append(a)
    )
    assert calls == []


def test_guard_raises_oserror_for_callers_that_skip_missing(table, monkeypatch):
    monkeypatch.setattr(mr, "path_reachable", lambda path: False)
    with pytest.raises(OSError, match="unreachable"):
        mr.guard("/mnt/cloud/onedrive/f")


def test_rotate_guards_both_candidate_open_sites():
    from pathlib import Path

    rotate = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    source = rotate.read_text(encoding="utf-8")
    start = source.index("def _provisional_candidate(")
    body = source[start : source.index("\ndef ", start + 10)]
    assert body.count("open(guard(") == 2
    assert "open(evidence_path" not in body and 'open(path, "rb")' not in body
