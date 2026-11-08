"""Pinned binaries items: catalog, probe, install, drift and plan (worktrunk `wt`)."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import binaries, install_backends, installer, profile_doctor
from skcapstone.fleet.profiles import ProfileSpecError, normalize_profile_spec

BASE = {"stateTier": "none", "capauthIdentityClass": "worker"}


def _archive(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"worktrunk-x86_64-unknown-linux-musl/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_wt(monkeypatch):
    """Swap the wt catalog entry's pin for a locally built archive."""
    blob = _archive({"wt": b"#!/bin/sh\necho wt v0.81.0\n", "git-wt": b"#!/bin/sh\n"})
    entry = binaries.CATALOG["wt"]
    monkeypatch.setitem(
        binaries.CATALOG,
        "wt",
        binaries.Binary(
            name="wt",
            version=entry.version,
            url=entry.url,
            sha256={"x86_64": hashlib.sha256(blob).hexdigest()},
            members=entry.members,
            config=entry.config,
        ),
    )

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(blob)

    return fetch


# --- catalog / spec -------------------------------------------------------


def test_catalog_pins_both_arches_with_full_hashes():
    entry = binaries.CATALOG["wt"]
    assert set(entry.sha256) == {"x86_64", "aarch64"}
    assert all(len(h) == 64 for h in entry.sha256.values())
    assert f"v{entry.version}/" in entry.url


def test_spec_binaries_default_empty():
    assert normalize_profile_spec(BASE)["binaries"] == {
        "required": [],
        "allowed": [],
        "mustNot": [],
    }


def test_spec_rejects_uncatalogued_required_binary():
    spec = dict(BASE, binaries={"required": ["nope"], "allowed": ["nope"]})
    with pytest.raises(ProfileSpecError, match="not in the pinned binaries catalog"):
        normalize_profile_spec(spec)


def test_spec_rejects_required_but_not_allowed():
    with pytest.raises(ProfileSpecError):
        normalize_profile_spec(dict(BASE, binaries={"required": ["wt"]}))


# --- probe ----------------------------------------------------------------


def test_probe_reports_version_without_v_prefix(tmp_path):
    (tmp_path / "wt").write_text("x")
    run = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="wt v0.81.0\n")  # noqa: E731
    assert binaries.probe(["wt"], runner=run, directory=tmp_path) == {"wt": "0.81.0"}


def test_probe_skips_absent_and_failing(tmp_path):
    (tmp_path / "wt").write_text("x")
    boom = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="")  # noqa: E731
    assert binaries.probe(["wt"], runner=boom, directory=tmp_path) == {}
    assert binaries.probe(["wt"], runner=boom, directory=tmp_path / "missing") == {}


# --- install --------------------------------------------------------------


def test_install_dry_run_fetches_nothing(tmp_path):
    def fetch(url, dest):  # pragma: no cover - must not run
        raise AssertionError("dry run fetched")

    status, detail = binaries.install(
        "wt", dry_run=True, fetch=fetch, arch="x86_64", directory=tmp_path
    )
    assert status == "would-write" and "sha256" in detail
    assert not any(tmp_path.iterdir())


def test_install_verifies_and_lays_down_members_and_config(tmp_path, fake_wt):
    bindir, home = tmp_path / "bin", tmp_path / "home"
    status, detail = binaries.install(
        "wt", fetch=fake_wt, arch="x86_64", directory=bindir, home=home
    )
    assert (status, detail) == ("ok", "")
    assert (bindir / "wt").stat().st_mode & 0o111
    assert (bindir / "git-wt").is_file()
    cfg = (home / ".config/worktrunk/config.toml").read_text()
    assert binaries.SK_MANAGED in cfg
    assert 'worktree-path = "~/skworld-worktrees/{{ repo }}.{{ branch | sanitize }}"' in cfg
    assert "pre-merge" in cfg


def test_install_refuses_hash_mismatch(tmp_path):
    status, detail = binaries.install(
        "wt",
        fetch=lambda url, dest: dest.write_bytes(b"tampered"),
        arch="x86_64",
        directory=tmp_path / "bin",
    )
    assert status == "failed" and "sha256 mismatch" in detail
    assert not (tmp_path / "bin").exists()


def test_install_keeps_operator_owned_config(tmp_path, fake_wt):
    home = tmp_path / "home"
    cfg = home / ".config/worktrunk/config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('worktree-path = "mine"\n')
    binaries.install("wt", fetch=fake_wt, arch="x86_64", directory=tmp_path / "b", home=home)
    assert cfg.read_text() == 'worktree-path = "mine"\n'


def test_install_unknown_arch_fails_cleanly(tmp_path):
    status, detail = binaries.install("wt", arch="riscv64", directory=tmp_path)
    assert status == "failed" and "riscv64" in detail


# --- drift + plan ---------------------------------------------------------


def _profile(**binaries_block):
    return normalize_profile_spec(dict(BASE, binaries=binaries_block))


def test_drift_missing_when_absent_or_wrong_version():
    prof = _profile(required=["wt"], allowed=["wt"])
    assert profile_doctor.diff({}, prof).missing_required_binaries == ["wt"]
    old = profile_doctor.diff({"binaries": {"wt": "0.1.0"}}, prof)
    assert old.missing_required_binaries == ["wt"]
    assert old.severity == "warn"
    pin = binaries.pinned("wt")
    assert profile_doctor.diff({"binaries": {"wt": pin}}, prof).clean


def test_drift_forbidden_binary_is_error():
    report = profile_doctor.diff({"binaries": {"wt": "0.1.0"}}, _profile(mustNot=["wt"]))
    assert report.forbidden_binaries == ["wt"] and report.severity == "error"


def test_plan_routes_binary_first_to_binaries_backend():
    drift = profile_doctor.DriftReport(
        missing_required_binaries=["wt"], missing_required_packages=["skcapstone"]
    )
    steps = installer.plan(drift).steps
    assert [(s.name, s.kind, s.backend_id) for s in steps][0] == ("wt", "binary", "binaries")
    assert install_backends.resolve("wt", "binary") == "binaries"


def test_backend_registered_and_dry_run_safe():
    fn = install_backends.default_backends()["binaries"]
    status, _ = fn(["wt"], dry_run=True, enable=False, start=False)
    assert status == "would-write"
