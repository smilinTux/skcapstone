"""The clean pinned qualification prefix: manifest, verify, fingerprint, rollout."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
import tomllib

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import qualify_env, staged_rollout
from skcapstone.fleet.qualified_runtime import REQUIRED_PACKAGES, STATE_NAME, qualify_prefix

ROOT = Path(__file__).resolve().parents[2]
HARNESS = "skharness @ git+https://github.com/smilinTux/skharness.git@" + "a" * 40


def test_committed_manifest_and_lock_are_consistent():
    manifest = qualify_env.load_manifest()
    pins = qualify_env.lock_pins(qualify_env.LOCK.read_bytes())
    assert manifest["python"]["path"] == "/usr/bin/python3.12"
    # Test tools come from sklegal's lock, not from whatever ~/.skenv holds.
    for name, version in {
        "pytest": "9.1.1",
        "pytest-asyncio": "1.3.0",
        "ruff": "0.16.3",
        "pluggy": "1.6.0",
        "iniconfig": "2.3.0",
        "packaging": "26.3",
    }.items():
        assert pins[name] == version
    assert "skcapstone" not in pins
    assert all(
        qualify_env.canonical(item.split("@")[0]) not in pins for item in manifest["direct"]
    )
    assert all(line.startswith("    --hash=sha256:") for line in _continuations())


def _continuations():
    return [
        line
        for line in qualify_env.LOCK.read_text().splitlines()
        if line.startswith(" ") and line.strip()
    ]


def test_direct_pins_match_the_fleet_qualify_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extra = project["optional-dependencies"]["fleet-qualify"]
    for item in qualify_env.load_manifest()["direct"]:
        assert item in extra


def test_lock_covers_every_declared_runtime_dependency():
    """A new skcapstone dependency must regenerate the lock, never drift past it."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pins = qualify_env.lock_pins(qualify_env.LOCK.read_bytes())
    declared = list(project["dependencies"]) + [
        item
        for item in project["optional-dependencies"]["fleet-qualify"]
        if " @ " not in item and "python_version < " not in item
    ]
    for requirement in declared:
        name = qualify_env.canonical(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
        assert name in pins, name


@pytest.mark.parametrize(
    "raw, error",
    [
        (b"pytest>=9\n", "exact pin"),
        (b"pytest==9.1.1\npytest==9.1.1\n", "twice"),
        (b"# only comments\n", "empty"),
    ],
)
def test_lock_parser_rejects_loose_or_ambiguous_pins(raw, error):
    with pytest.raises(qualify_env.QualifyEnvError, match=error):
        qualify_env.lock_pins(raw)


def test_manifest_rejects_lock_digest_mismatch(tmp_path):
    manifest = json.loads(qualify_env.MANIFEST.read_text())
    lock = tmp_path / "lock.txt"
    lock.write_bytes(qualify_env.LOCK.read_bytes() + b"# tampered\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(qualify_env.QualifyEnvError, match="digest"):
        qualify_env.load_manifest(path, lock)


def _dist(site: Path, name: str, version: str, direct: dict | None = None) -> None:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n")
    if direct is not None:
        (info / "direct_url.json").write_text(json.dumps(direct))


@pytest.fixture
def clean_prefix(tmp_path):
    """A synthetic prefix that satisfies every verify() rule."""
    prefix = tmp_path / "qualify-env"
    site = qualify_env.site_packages(prefix) / "site-packages"
    site.mkdir(parents=True)
    (prefix / "bin").mkdir()
    interpreter = tmp_path / "python3.12"
    interpreter.write_bytes(b"pinned interpreter")
    (prefix / "bin/python").symlink_to(interpreter)
    (prefix / "bin/ruff").write_bytes(b"ruff")
    (prefix / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    for package in REQUIRED_PACKAGES:
        (site / package).mkdir()
        (site / package / "__init__.py").write_text("")
    lock = tmp_path / "lock.txt"
    lock.write_text("pytest==9.1.1 \\\n    --hash=sha256:" + "0" * 64 + "\nruff==0.16.3\n")
    _dist(site, "pytest", "9.1.1")
    _dist(site, "ruff", "0.16.3")
    _dist(site, "skharness", "0.1.0", {"vcs_info": {"vcs": "git", "commit_id": "a" * 40}})
    _dist(site, "skcapstone", "0.15.1", {"dir_info": {}})
    manifest = {
        "python": {"sha256": hashlib.sha256(b"pinned interpreter").hexdigest()},
        "sklegal": {"revision": "b" * 40, "uv_lock_sha256": "c" * 64},
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "direct": [HARNESS],
    }
    (prefix / STATE_NAME).write_bytes(
        qualify_env.state_bytes(qualify_env.state_for(prefix, manifest))
    )
    assert qualify_env.verify(prefix, manifest, lock) == []
    return prefix, site, manifest, lock


def test_verify_rejects_editable_startup_file(clean_prefix):
    prefix, site, manifest, lock = clean_prefix
    (site / "__editable__.sklegal_api-0.1.0.pth").write_text("/home/x/sklegal/services/api/src\n")
    assert "editable startup file: __editable__.sklegal_api-0.1.0.pth" in qualify_env.verify(
        prefix, manifest, lock
    )


def test_verify_rejects_startup_path_and_entry_outside_prefix(clean_prefix, tmp_path):
    prefix, site, manifest, lock = clean_prefix
    outside = tmp_path / "ambient"
    outside.mkdir()
    (site / "ambient.pth").write_text(str(outside) + "\n")
    (site / "leak").symlink_to(outside)
    findings = qualify_env.verify(prefix, manifest, lock)
    assert "startup path escapes the prefix: ambient.pth" in findings
    assert "site-packages entry resolves outside the prefix: leak" in findings


def test_verify_rejects_missing_required_package(clean_prefix):
    prefix, site, manifest, lock = clean_prefix
    for path in (site / "ansible").iterdir():
        path.unlink()
    (site / "ansible").rmdir()
    assert "missing required package: ansible" in qualify_env.verify(prefix, manifest, lock)


def test_verify_rejects_version_drift_and_unexpected_distribution(clean_prefix):
    prefix, site, manifest, lock = clean_prefix
    metadata = next(site.glob("pytest-*.dist-info")) / "METADATA"
    metadata.write_text(metadata.read_text().replace("9.1.1", "9.0.2"))
    _dist(site, "rich", "13.9.4")
    findings = qualify_env.verify(prefix, manifest, lock)
    assert "locked distribution drifted: pytest 9.0.2 != 9.1.1" in findings
    assert "unexpected distribution: rich==13.9.4" in findings
    assert "qualify manifest is stale" in findings


def test_verify_rejects_editable_skcapstone_and_wrong_harness_commit(clean_prefix):
    prefix, site, manifest, lock = clean_prefix
    info = next(site.glob("skcapstone-*.dist-info"))
    (info / "direct_url.json").write_text(json.dumps({"dir_info": {"editable": True}}))
    manifest = {**manifest, "direct": [HARNESS.replace("a" * 40, "d" * 40)]}
    findings = qualify_env.verify(prefix, manifest, lock)
    assert "editable distribution: skcapstone" in findings
    assert any(
        f.startswith("direct distribution missing or not at dddddddddddd") for f in findings
    )


def test_verify_rejects_system_site_packages_and_wrong_interpreter(clean_prefix):
    prefix, _site, manifest, lock = clean_prefix
    (prefix / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
    manifest = {**manifest, "python": {"sha256": "0" * 64}}
    findings = qualify_env.verify(prefix, manifest, lock)
    assert "prefix exposes system site-packages" in findings
    assert "interpreter differs from the pinned python sha256" in findings


def test_state_record_is_host_and_skcapstone_neutral(clean_prefix):
    prefix, site, manifest, _lock = clean_prefix
    before = (prefix / STATE_NAME).read_bytes()
    info = next(site.glob("skcapstone-*.dist-info"))
    (info / "METADATA").write_text("Name: skcapstone\nVersion: 0.15.999\n\n")
    assert qualify_env.state_bytes(qualify_env.state_for(prefix, manifest)) == before
    assert b"noroc" not in before and b"chiap" not in before


def test_fingerprints_bind_the_qualify_manifest(qualified_runtime):
    runtime, toolchain = plan.runtime_fingerprint(), plan.toolchain_fingerprint()
    (qualified_runtime / STATE_NAME).write_text('{"schema":"other lock"}\n')
    assert plan.runtime_fingerprint() != runtime
    assert plan.toolchain_fingerprint() != toolchain
    (qualified_runtime / STATE_NAME).unlink()
    with pytest.raises(plan.TestEvidenceError, match="manifest is missing"):
        plan.runtime_fingerprint()
    with pytest.raises(plan.TestEvidenceError, match="manifest is missing"):
        plan.toolchain_fingerprint()


def test_legacy_toolchain_keeps_historical_rows(qualified_runtime, monkeypatch):
    """The legacy prefix is hashed without a manifest row, as before the switch."""
    monkeypatch.setattr(plan, "LEGACY_PREFIX", qualified_runtime)
    (qualified_runtime / STATE_NAME).unlink()
    assert plan.legacy_toolchain_fingerprint() is not None
    monkeypatch.setattr(plan, "LEGACY_PREFIX", qualified_runtime / "absent")
    assert plan.legacy_toolchain_fingerprint() is None


def test_sealed_execution_uses_the_clean_prefix_not_skenv():
    assert qualify_prefix(Path("/h")) == Path("/h/.local/share/skcapstone/qualify-env")
    assert ".skenv" not in str(qualify_prefix(Path("/h")))
    assert plan.PREFIX == qualify_prefix(Path.home())
    # LEGACY_PREFIX is redirected by the hermetic autouse fixture here.
    assert plan.LEGACY_PREFIX != plan.PREFIX


@pytest.mark.parametrize("steps", ["_DEPLOY_STEPS", "_ROLLBACK_STEPS"])
def test_rollout_builds_and_verifies_prefix_after_package_install(steps):
    names = [name for name, _ in getattr(staged_rollout, steps)]
    assert names.index("pip_install") < names.index("qualify_env")
    assert names.index("qualify_env") < names.index("qualification_tools")
    command = dict(getattr(staged_rollout, steps))["qualify_env"]
    assert "-m skcapstone.fleet.qualify_env build --source {repo}" in command


def test_cli_verify_reports_findings(tmp_path, capsys):
    assert qualify_env.main(["verify", "--prefix", str(tmp_path / "absent")]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["findings"] == ["qualify-env prefix is missing or redirected"]


def test_build_refuses_a_different_interpreter(tmp_path):
    manifest = json.loads(qualify_env.MANIFEST.read_text())
    manifest["python"]["sha256"] = "0" * 64
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    if not Path(manifest["python"]["path"]).exists():
        pytest.skip("pinned interpreter not present on this host")
    with pytest.raises(qualify_env.QualifyEnvError, match="interpreter"):
        qualify_env.build(tmp_path / "prefix", ROOT, manifest_path=path, uv=sys.executable)
