"""Source-controlled sandbox prerequisites reach only selected worker hosts."""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import qualification_tools as tools


def allowlist(path, hosts):
    """Create a private operator fixture."""
    path.write_text(json.dumps({"schema": "skfleet.qualification-tools/v1", "hosts": hosts}))
    path.chmod(0o600)
    return path


def test_sandbox_selection_does_not_select_backup_tools(tmp_path):
    config = allowlist(tmp_path / "selection", {"chiap01": ["sandbox"], "chiap03": ["sandbox"]})
    assert tools.selected_host(config, "chiap01", "sandbox")
    assert not tools.selected_host(config, "chiap01")
    assert not tools.selected_host(config, "chiap02", "sandbox")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Mock root operations around real synthetic archive and checksum validation."""
    from skcapstone.fleet import sandbox_tools as module

    binary = b"synthetic node 22"
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w:xz") as archive:
        entry = tarfile.TarInfo("node-v22.23.3-linux-x64/bin/node")
        entry.size = len(binary)
        archive.addfile(entry, io.BytesIO(binary))
    data = packed.getvalue()
    asset = {
        "version": "22.23.3",
        "url": "https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz",
        "member": entry.name,
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
    }
    monkeypatch.setattr(module, "node_asset", lambda arch: asset)
    root = tmp_path / "system"
    for relative in ("etc/apparmor.d", "usr/local/bin", "usr/bin"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    config = allowlist(tmp_path / "selection", {"chiap01": ["sandbox"]})
    calls, downloads = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] and kwargs["timeout"] <= 30
        if "/usr/bin/install" in argv:
            shutil.copyfile(argv[-2], argv[-1])
            Path(argv[-1]).chmod(int(argv[argv.index("-m") + 1], 8))
        elif "/usr/bin/mv" in argv:
            Path(argv[-2]).replace(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fetch(url):
        downloads.append(url)
        return data

    def match(path, digest, mode):
        return (
            path.is_file()
            and not path.is_symlink()
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            and path.stat().st_mode & 0o777 == mode
        )

    monkeypatch.setattr(module, "root_file_matches", match)
    monkeypatch.setattr(module, "readiness", lambda *args, **kwargs: [])
    return module, root, config, asset, binary, calls, downloads, fetch, run


@pytest.mark.parametrize("apply", [False, True])
def test_unselected_hosts_have_no_download_root_call_or_write(sandbox, apply):
    module, root, config, _, _, calls, downloads, fetch, run = sandbox
    result = module.provision(
        config, "chiap02", "unsupported", root=root, apply=apply, fetch=fetch, runner=run
    )
    assert result["state"] == "not-selected"
    assert not calls and not downloads and not list((root / "usr/local/bin").iterdir())


def test_plan_has_no_mutations(sandbox):
    module, root, config, _, _, calls, downloads, fetch, run = sandbox
    assert (
        module.provision(config, "chiap01", "x86_64", root=root, fetch=fetch, runner=run)["state"]
        == "planned"
    )
    assert not calls and not downloads


def test_install_owns_modes_loads_exact_profiles_and_preserves_apt_node(sandbox):
    module, root, config, _, binary, calls, downloads, fetch, run = sandbox
    apt = root / "usr/bin/node"
    apt.write_bytes(b"old apt node")
    result = module.provision(
        config, "chiap01", "x86_64", root=root, apply=True, fetch=fetch, runner=run
    )
    assert result["state"] == "installed"
    assert apt.read_bytes() == b"old apt node"
    assert (root / "usr/local/bin/node").read_bytes() == binary
    profile = root / "etc/apparmor.d/skfleet-bwrap"
    assert profile.read_bytes() == module.profile_bytes()
    assert any(argv[-2:] == ["-r", str(profile)] for argv in calls)
    installs = [argv for argv in calls if "/usr/bin/install" in argv]
    assert len(installs) == 2
    assert all(argv[1:3] == ["-n", "/usr/bin/install"] for argv in installs)
    assert all("root" in argv and ("0644" in argv or "0755" in argv) for argv in installs)
    stamps = {p: p.stat().st_mtime_ns for p in (profile, root / "usr/local/bin/node")}
    downloads.clear()
    result = module.provision(
        config, "chiap01", "x86_64", root=root, apply=True, fetch=fetch, runner=run
    )
    assert result["state"] == "unchanged" and not downloads
    assert all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items())


@pytest.mark.parametrize("failure", ["archive", "binary", "link", "oversize"])
def test_bad_archive_precedes_all_privileged_mutations(sandbox, monkeypatch, failure):
    module, root, config, asset, _, calls, _, fetch, run = sandbox
    if failure == "archive":
        asset["archive_sha256"] = "0" * 64
    elif failure == "binary":
        asset["binary_sha256"] = "0" * 64
    elif failure == "oversize":
        monkeypatch.setattr(module, "MAX_NODE", 4)
    else:
        packed = io.BytesIO()
        with tarfile.open(fileobj=packed, mode="w:xz") as archive:
            entry = tarfile.TarInfo(asset["member"])
            entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/shadow"
            archive.addfile(entry)
        data = packed.getvalue()
        asset["archive_sha256"] = hashlib.sha256(data).hexdigest()

        def fetch(_):
            return data

    with pytest.raises(ValueError):
        module.provision(
            config, "chiap01", "x86_64", root=root, apply=True, fetch=fetch, runner=run
        )
    assert not calls and not list((root / "etc/apparmor.d").iterdir())


def test_exact_profile_and_official_node_pins_are_packaged():
    from skcapstone.fleet import sandbox_tools as module

    data = module.profile_bytes()
    assert len(data) == 951
    assert (
        hashlib.sha256(data).hexdigest()
        == "309919f9d569a90e8e777d300727c90cfc047bc1cf207733a94467f759bb16a1"
    )
    assert b"audit deny capability," in data
    assert b"profile skfleet_bwrap" in data and b"profile skfleet_unpriv_bwrap" in data
    for arch in ("x86_64", "aarch64"):
        asset = module.node_asset(arch)
        assert asset["version"] == "22.23.3"
        assert asset["url"].startswith("https://nodejs.org/dist/v22.23.3/")
        assert len(asset["archive_sha256"]) == len(asset["binary_sha256"]) == 64


def test_redirected_or_unowned_root_target_is_not_healthy(tmp_path):
    from skcapstone.fleet import sandbox_tools as module

    target = tmp_path / "target"
    target.write_bytes(b"fixture")
    target.chmod(0o644)
    assert not module.root_file_matches(target, hashlib.sha256(b"fixture").hexdigest(), 0o644)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="redirected"):
        module.root_file_matches(link, "0" * 64, 0o644)


@pytest.mark.parametrize(
    "broken", [None, "profile", "loaded", "node", "refused", "missing", "timeout"]
)
def test_readiness_refuses_missing_profiles_wrong_node_and_sandbox_failure(
    tmp_path, monkeypatch, broken
):
    from skcapstone.fleet import sandbox_tools as module

    monkeypatch.setattr(module, "root_file_matches", lambda *args: broken != "profile")
    monkeypatch.setattr(module, "loaded_profiles", lambda *args, **kwargs: broken != "loaded")
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        assert kwargs["timeout"] == 10
        if broken == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        if broken == "missing":
            raise FileNotFoundError("bwrap")
        is_node = "--version" in argv
        return SimpleNamespace(
            returncode=int(broken == "refused"),
            stdout=(
                "v18.19.1\n" if is_node and broken == "node" else "v22.23.3\n" if is_node else ""
            ),
            stderr="refused",
        )

    failures = module.readiness(sys.executable, root=tmp_path, runner=run)
    assert bool(failures) == bool(broken)
    assert observed and all("--unshare-all" in argv for argv, _ in observed)
    assert any(sys.executable in argv for argv, _ in observed)
    assert any("--version" in argv for argv, _ in observed) or broken in {"missing", "timeout"}


def test_sealed_commands_prefer_local_node_without_changing_prefix_priority(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_node as node
    from skcapstone.fleet import production_test_worker as worker

    local = tmp_path / "usr/local/bin/node"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"node22")
    assert node.system_node(tmp_path) == local
    local.unlink()
    assert node.system_node(tmp_path) == tmp_path / "usr/bin/node"
    source = tmp_path / "source"
    source.mkdir()
    command = worker.sandbox_command(source, tmp_path / "output", ["node", "--version"])
    assert (
        command[command.index("PATH") + 1]
        == str(worker.PREFIX / "bin") + ":/usr/local/bin:/usr/bin"
    )


def test_production_readiness_and_drift_use_shared_sandbox_gate(tmp_path, monkeypatch, capsys):
    from skcapstone.fleet import rollout_drift

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/fleet"))
    import skfleet_readiness as observer

    from skcapstone.fleet import sandbox_tools as module

    monkeypatch.setattr(
        module,
        "readiness",
        lambda *args, **kwargs: ["AppArmor profile missing", "Node major must be 22"],
    )
    source = tmp_path / "dispatcher.py"
    source.write_text("SKFLEET_PRODUCTION_POLICY_V1 = True\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "fixture.service").write_text("ExecStart=/usr/bin/python3 -m json\n")
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setattr(observer, "production_environment_error", lambda *args: None)
    monkeypatch.setattr(
        observer, "check_module_imports", lambda modules, _: dict.fromkeys(modules, True)
    )
    assert observer._run(source, units, sys.executable, None) == 1
    assert "FAIL qualification sandbox: AppArmor profile missing" in capsys.readouterr().out
    home = tmp_path / "home"
    marker = home / ".skcapstone/fleet/production.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    monkeypatch.setattr(
        rollout_drift,
        "_load_readiness_module",
        lambda _: SimpleNamespace(
            unit_in_scope=lambda _: (False, None, "fixture"),
            _systemctl_show_value=lambda *args: ("inactive", None),
        ),
    )
    monkeypatch.setattr(rollout_drift, "_script_files", lambda _: [])
    findings = rollout_drift.detect_drift({"units": []}, home, tmp_path)
    assert any(d.artifact.startswith("qualification-sandbox:") for d in findings)


@pytest.mark.parametrize("failure", ["refusal", "timeout"])
def test_root_or_parser_failure_cannot_report_ready(sandbox, failure):
    module, root, config, _, _, calls, _, fetch, run = sandbox

    def refused(argv, **kwargs):
        if "apparmor_parser" in " ".join(argv):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            raise subprocess.CalledProcessError(1, argv)
        return run(argv, **kwargs)

    with pytest.raises(subprocess.SubprocessError):
        module.provision(
            config, "chiap01", "x86_64", root=root, apply=True, fetch=fetch, runner=refused
        )
    assert calls


@pytest.mark.parametrize("state", ["enforce", "complain", "missing"])
def test_loaded_profiles_require_both_enforcing_names(tmp_path, state):
    from skcapstone.fleet import sandbox_tools as module

    root = tmp_path / "sys/kernel/security/apparmor"
    root.mkdir(parents=True)
    if state != "missing":
        (root / "profiles").write_text(
            "skfleet_bwrap (enforce)\nskfleet_unpriv_bwrap (" + state + ")\n"
        )
    assert module.loaded_profiles(tmp_path, lambda *args, **kwargs: None) == (state == "enforce")


def test_selected_non_linux_and_redirected_parent_refuse_before_root_calls(sandbox, monkeypatch):
    module, root, config, _, _, calls, downloads, fetch, run = sandbox
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="Linux"):
        module.provision(
            config, "chiap01", "x86_64", root=root, apply=True, fetch=fetch, runner=run
        )
    assert not calls and not downloads


def test_exact_private_toolchain_combinations(tmp_path):
    config = allowlist(tmp_path / "selection", {"chiap01": ["skstacks", "sandbox"]})
    assert tools.selected_host(config, "chiap01")
    assert tools.selected_host(config, "chiap01", "sandbox")
    for invalid in ([], ["sandbox", "sandbox"], [None], [["sandbox"]], ["other"]):
        allowlist(config, {"chiap01": invalid})
        with pytest.raises(ValueError, match="allowlist"):
            tools.selected_host(config, "chiap01", "sandbox")


@pytest.mark.parametrize("fail", [False, True])
def test_kernel_profile_reader_is_bounded_read_only_and_fail_closed(tmp_path, monkeypatch, fail):
    from skcapstone.fleet import sandbox_tools as module

    def denied(*args, **kwargs):
        raise PermissionError("kernel profile listing")

    monkeypatch.setattr(Path, "read_text", denied)
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        assert kwargs["timeout"] == 10 and kwargs["check"]
        if fail:
            raise subprocess.TimeoutExpired(argv, 10)
        return SimpleNamespace(stdout="skfleet_bwrap (enforce)\nskfleet_unpriv_bwrap (enforce)\n")

    assert module.loaded_profiles(tmp_path, run) == (not fail)
    assert seen == [
        ["sudo", "-n", "/usr/bin/cat", str(tmp_path / "sys/kernel/security/apparmor/profiles")]
    ]
