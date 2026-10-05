"""Pinned source-suite tools only reach explicitly selected worker hosts."""

import bz2
import hashlib
import io
import json
import tarfile

import pytest

from skcapstone.fleet.staged_rollout import _DEPLOY_STEPS, _ROLLBACK_STEPS


def write_allowlist(path, hosts):
    """Write a private operator fixture, not a live fleet policy."""
    path.write_text(json.dumps({"schema": "skfleet.qualification-tools/v1", "hosts": hosts}))
    path.chmod(0o600)
    return path


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Provide tiny synthetic archives with independently computed pins."""
    from skcapstone.fleet import qualification_tools as module

    binaries = {"restic": b"synthetic restic", "shellcheck": b"synthetic shellcheck"}
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as archive:
        entry = tarfile.TarInfo("shellcheck-v0.11.0/shellcheck")
        entry.size = len(binaries["shellcheck"])
        archive.addfile(entry, io.BytesIO(binaries["shellcheck"]))
    downloads = {"restic": bz2.compress(binaries["restic"]), "shellcheck": packed.getvalue()}
    assets = {
        name: {
            "url": "https://example.invalid/" + name,
            "version": "0.19.1" if name == "restic" else "0.11.0",
            "format": "bz2" if name == "restic" else "tar.gz",
            "member": None if name == "restic" else "shellcheck-v0.11.0/shellcheck",
            "archive_sha256": hashlib.sha256(downloads[name]).hexdigest(),
            "binary_sha256": hashlib.sha256(binaries[name]).hexdigest(),
        }
        for name in binaries
    }
    monkeypatch.setattr(module, "load_artifacts", lambda arch: assets)
    prefix = tmp_path / "runtime"
    (prefix / "bin").mkdir(parents=True)
    config = write_allowlist(tmp_path / "allowlist.json", {"worker": ["skstacks"]})
    calls = []

    def fetch(url):
        calls.append(url)
        return downloads[url.rsplit("/", 1)[1]]

    return module, prefix, config, assets, downloads, binaries, calls, fetch


def test_staged_deploy_and_rollback_use_opt_in_provisioning():
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        names = [name for name, _ in steps]
        command = dict(steps)["qualification_tools"]
        assert "-m skcapstone.fleet.qualification_tools --apply" in command
        assert "test -f {repo}/src/skcapstone/fleet/qualification_tools.py" in command
        assert names.index("pip_install") < names.index("qualification_tools")
        assert names.index("qualification_tools") < names.index("converge")


@pytest.mark.parametrize("apply", [False, True])
def test_unselected_and_missing_allowlists_do_nothing(tools, apply):
    module, prefix, config, _, _, _, calls, fetch = tools
    for path in [config, config.with_name("missing.json")]:
        result = module.provision(
            prefix, path, "other-worker", "unsupported", apply=apply, fetch=fetch
        )
        assert result["state"] == "not-selected"
    assert calls == [] and list((prefix / "bin").iterdir()) == []


def test_default_dry_run_performs_no_download_or_write(tools):
    module, prefix, config, _, _, _, calls, fetch = tools
    result = module.provision(prefix, config, "worker", "x86_64", fetch=fetch)
    assert result["state"] == "planned" and all(row["changed"] for row in result["tools"])
    assert calls == [] and list((prefix / "bin").iterdir()) == []


def test_verified_install_is_atomic_per_tool_and_idempotent(tools):
    module, prefix, config, _, _, binaries, calls, fetch = tools
    result = module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert result["state"] == "installed" and len(calls) == 2
    for name, data in binaries.items():
        target = prefix / "bin" / name
        assert target.read_bytes() == data and target.stat().st_mode & 0o111
    stamps = {name: (prefix / "bin" / name).stat().st_mtime_ns for name in binaries}
    calls.clear()
    result = module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert result["state"] == "unchanged" and calls == []
    assert stamps == {name: (prefix / "bin" / name).stat().st_mtime_ns for name in binaries}


@pytest.mark.parametrize("wrong_binary", [False, True])
def test_second_tool_checksum_failure_precedes_all_replacements(tools, wrong_binary):
    module, prefix, config, assets, downloads, _, _, fetch = tools
    for name in assets:
        (prefix / "bin" / name).write_bytes(b"previous approved tool")
    if wrong_binary:
        assets["shellcheck"]["binary_sha256"] = "0" * 64
    else:
        downloads["shellcheck"] += b"tampered"
    with pytest.raises(ValueError, match="checksum"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert all(
        (prefix / "bin" / name).read_bytes() == b"previous approved tool" for name in assets
    )


def test_archive_link_is_refused_without_extracting(tools):
    module, prefix, config, assets, downloads, _, _, fetch = tools
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:gz") as archive:
        entry = tarfile.TarInfo("shellcheck-v0.11.0/shellcheck")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/outside"
        archive.addfile(entry)
    downloads["shellcheck"] = packed.getvalue()
    assets["shellcheck"]["archive_sha256"] = hashlib.sha256(packed.getvalue()).hexdigest()
    with pytest.raises(ValueError, match="regular"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert list((prefix / "bin").iterdir()) == []


def test_redirected_binary_is_refused_before_download(tools, tmp_path):
    module, prefix, config, _, _, _, calls, fetch = tools
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    (prefix / "bin/restic").symlink_to(outside)
    with pytest.raises(ValueError, match="redirected"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert calls == [] and outside.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "hosts",
    [{"../escape": ["skstacks"]}, {"worker": ["unknown"]}, {"worker": ["skstacks", "skstacks"]}],
)
def test_allowlist_is_exact_bounded_and_private(tools, hosts):
    module, prefix, config, _, _, _, calls, fetch = tools
    write_allowlist(config, hosts)
    with pytest.raises(ValueError, match="allowlist"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert calls == []


def test_public_or_symlink_allowlist_cannot_enable_provisioning(tools, tmp_path):
    module, prefix, config, _, _, _, calls, fetch = tools
    config.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    config.chmod(0o600)
    link = tmp_path / "redirected.json"
    link.symlink_to(config)
    with pytest.raises((OSError, ValueError)):
        module.provision(prefix, link, "worker", "x86_64", apply=True, fetch=fetch)
    assert calls == []


def test_packaged_manifest_has_exact_versions_and_architecture_pins():
    from skcapstone.fleet.qualification_tools import load_artifacts

    for arch in ["x86_64", "aarch64"]:
        assets = load_artifacts(arch)
        assert assets["restic"]["version"] == "0.19.1"
        assert assets["shellcheck"]["version"] == "0.11.0"
        for asset in assets.values():
            assert asset["url"].startswith("https://github.com/")
            assert len(asset["archive_sha256"]) == len(asset["binary_sha256"]) == 64
    with pytest.raises(ValueError, match="architecture"):
        load_artifacts("unsupported")


def test_selected_non_linux_host_cannot_install_linux_tools(tools, monkeypatch):
    """Architecture alone must not admit Linux binaries on another operating system."""
    module, prefix, config, _, _, _, calls, fetch = tools
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Linux"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert calls == [] and list((prefix / "bin").iterdir()) == []


def test_expansion_bound_is_enforced_before_any_replacement(tools, monkeypatch):
    """Even a pinned compressed member cannot expand beyond the executable bound."""
    module, prefix, config, _, _, _, _, fetch = tools
    monkeypatch.setattr(module, "MAX_BINARY", 4)
    with pytest.raises(ValueError, match="bound"):
        module.provision(prefix, config, "worker", "x86_64", apply=True, fetch=fetch)
    assert list((prefix / "bin").iterdir()) == []
