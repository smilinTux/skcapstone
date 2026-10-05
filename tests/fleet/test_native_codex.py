"""Codex may disable its own sandbox only behind the enforced fleet boundary."""

import importlib
import os
import subprocess
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore


@pytest.fixture
def module():
    return importlib.import_module("skcapstone.fleet.native_codex")


@pytest.fixture
def inputs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    executable = tmp_path / "codex-runtime/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.parent.parent.chmod(0o700)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    auth.chmod(0o600)
    return workspace, executable, auth


def test_inner_sandbox_off_is_always_wrapped_and_environment_is_closed(
    module, inputs, monkeypatch
):
    monkeypatch.setenv("BASH_ENV", "/private/execute-me")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    command = module.sandbox_command(*inputs, model="gpt-6.1-sol", owner="retained-owner")
    assert command[:4] == ["/usr/bin/bwrap", "--unshare-all", "--share-net", "--die-with-parent"]
    assert ["--cap-drop", "ALL"] == command[command.index("--cap-drop") :][:2]
    assert ["--remount-ro", "/"] == command[command.index("--remount-ro") :][:2]
    assert "--clearenv" in command
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "danger-full-access"
    assert 'approval_policy="never"' in command
    assert "--ignore-user-config" in command
    assert "--disable-userns" not in command
    assert "--unshare-user" not in command
    assert "execute-me" not in " ".join(command)
    assert "must-not-cross" not in " ".join(command)
    assert "DBUS_SESSION_BUS_ADDRESS" not in command
    assert "BASH_ENV" not in command
    assert ["--bind", str(inputs[0]), "/work"] == command[command.index(str(inputs[0])) - 1 :][:3]
    assert ["--ro-bind", str(inputs[2]), "/tmp/codex/auth.json"] == command[
        command.index(str(inputs[2])) - 1 :
    ][:3]
    assert "--bind" not in command[command.index(str(inputs[2])) - 1 :][:3]


@pytest.mark.parametrize("target", [0, 1, 2])
def test_redirected_or_unsafe_inputs_refuse(module, inputs, tmp_path, target):
    paths = list(inputs)
    alias = tmp_path / ("alias" + str(target))
    alias.symlink_to(paths[target], target_is_directory=target == 0)
    paths[target] = alias
    with pytest.raises(ValueError, match="redirected"):
        module.sandbox_command(*paths, model="gpt-6.1-sol", owner="retained-owner")


def test_auth_must_be_private_and_is_never_copied(module, inputs):
    inputs[2].chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        module.sandbox_command(*inputs, model="gpt-6.1-sol", owner="retained-owner")


def test_kernel_guard_refuses_direct_host_execution(module):
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            module.GUARD,
            str(os.getuid()),
            str(os.getgid()),
            "/usr/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "fleet outer sandbox" in result.stderr


def test_native_gate_refuses_missing_reservation_before_any_spawn(module, tmp_path, monkeypatch):
    monkeypatch.delenv("SKFLEET_ADMISSION_ID", raising=False)
    with pytest.raises(ValueError, match="native admitted unit"):
        module.require_native_unit(tmp_path, {}, "skfleet-worker-codex-deadbeef.service", {})


def test_native_gate_refuses_another_process_or_changed_claim(module, tmp_path, monkeypatch):
    from skcapstone.fleet import production_admission as admission

    monkeypatch.setenv("SKFLEET_ADMISSION_ID", "a" * 64)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    monkeypatch.setattr(
        admission,
        "unit_state",
        lambda *_a, **_k: {
            "MainPID": str(os.getpid() + 1),
            "InvocationID": "b" * 32,
            admission.MARKER: "a" * 64,
            "ActiveState": "active",
            "SubState": "running",
        },
    )
    with pytest.raises(ValueError, match="native admitted unit"):
        module.require_native_unit(tmp_path, {}, "skfleet-worker-codex-deadbeef.service", {})


@pytest.fixture
def native(module, tmp_path, monkeypatch):
    admission = module.admission
    binding = dict(card_id="deadbeef", owner="retained-owner", claim_revision="exact-claim")
    unit = "skfleet-worker-codex-deadbeef.service"
    host = os.uname().nodename.split(".")[0]
    limits = dict(
        cpu_quota_percent=200, memory_max_bytes=1024, tasks_max=256, runtime_max_seconds=3600
    )
    policy = {"node_quotas": {host: limits}}
    (tmp_path / "agents" / binding["owner"]).mkdir(parents=True, mode=0o700)
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="deadbeef",
            title="[S] Public synthetic source",
            initial_owner=binding["owner"],
            initial_claim_revision=binding["claim_revision"],
            initial_labels=["source-only", "codex-only", "dispatch-approved"],
        )
    )
    intent = admission._intent(policy, host, unit, binding, ["systemd-run", "--", "/fixture"])
    marker = admission._reservation_id(intent)
    directory = tmp_path / "fleet/resource-admission" / host / marker
    directory.mkdir(parents=True, mode=0o700)
    admission.write_once(directory / "intent.json", intent)
    admission.write_once(
        directory / "start.json",
        dict(
            schema="skfleet.resource-start/v1",
            reservation_id=marker,
            binding=binding,
            argv_sha256=intent["argv_sha256"],
        ),
    )
    state = dict(
        MainPID=str(os.getpid()),
        InvocationID="b" * 32,
        SKFLEET_ADMISSION_ID=marker,
        ActiveState="active",
        SubState="running",
        MemoryMax="1024",
    )
    monkeypatch.setenv("SKFLEET_ADMISSION_ID", marker)
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    monkeypatch.setattr(admission, "unit_state", lambda *_a, **_k: state)
    checks = []
    monkeypatch.setattr(module.profile, "preflight", lambda *args: checks.append(args))
    return tmp_path, policy, unit, binding, state, store, directory, checks


def test_native_admission_and_current_profile_are_required(module, native):
    home, policy, unit, binding, state, store, directory, checks = native
    module.require_native_unit(home, policy, unit, binding)
    assert len(checks) == 1
    assert checks[0][1]["id"] == binding["card_id"]


@pytest.mark.parametrize(
    "change", ["memory", "invocation", "claim", "start", "private", "profile", "identity"]
)
def test_changed_native_custody_or_authorization_refuses(module, native, monkeypatch, change):
    home, policy, unit, binding, state, store, directory, checks = native
    if change == "memory":
        state["MemoryMax"] = "9999"
    elif change == "invocation":
        state["InvocationID"] = "c" * 32
    elif change == "claim":
        store.append_event(
            "deadbeef",
            "release_claim",
            "fixture",
            released_owner=binding["owner"],
            expected_claim_revision=binding["claim_revision"],
            transition_id="fixture-release",
        )
    elif change == "start":
        (directory / "start.json").unlink()
        module.admission.write_once(directory / "start.json", {"forged": True})
    elif change == "private":
        store.append_event("deadbeef", "add_label", "fixture", label="private")
    elif change == "identity":
        (home / "agents" / binding["owner"]).rmdir()
    else:

        def stale(*args):
            raise ValueError("qualified test execution environment changed")

        monkeypatch.setattr(module.profile, "preflight", stale)
    with pytest.raises(ValueError):
        module.require_native_unit(home, policy, unit, binding)


@pytest.fixture
def source_binding(inputs):
    workspace = inputs[0]
    environment = dict(
        PATH="/usr/bin:/bin",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_AUTHOR_NAME="fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )

    def git(*args):
        return subprocess.check_output(
            ["/usr/bin/git", "-C", str(workspace), *args], text=True, env=environment
        ).strip()

    git("init", "--quiet")
    git("remote", "add", "origin", "https://github.com/synthetic/example")
    git("commit", "--allow-empty", "--quiet", "-m", "synthetic source")
    core = dict(
        id="deadbeef",
        acceptance_criteria=["Exact public synthetic source"],
        meta=dict(
            repository="https://github.com/synthetic/example",
            base_revision=git("rev-parse", "HEAD"),
        ),
        links={},
    )
    return workspace, core


def test_source_binding_ignores_inherited_git_injection(module, source_binding, monkeypatch):
    workspace, core = source_binding
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://wrong.example/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    module.require_source(workspace, core)


@pytest.mark.parametrize("change", ["repository", "base", "missing-base"])
def test_source_binding_refuses_wrong_repository_or_unreachable_base(
    module, source_binding, change
):
    workspace, core = source_binding
    if change == "repository":
        core["meta"]["repository"] = "https://github.com/wrong/repository"
    elif change == "base":
        core["meta"]["base_revision"] = "a" * 40
    else:
        del core["meta"]["base_revision"]
    with pytest.raises(ValueError, match="source"):
        module.require_source(workspace, core)


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="bubblewrap unavailable")
def test_outer_boundary_executes_and_denies_unmounted_host_files(module, inputs, tmp_path):
    outside = tmp_path / "host-private"
    outside.write_text("private-canary")
    executable = inputs[1]
    executable.write_text(
        "#!/usr/bin/python3\nimport os,sys\n"
        "assert sys.argv[sys.argv.index('--sandbox')+1]=='danger-full-access'\n"
        f"assert not os.path.exists({str(outside)!r})\n"
        "assert os.statvfs('/').f_flag & os.ST_RDONLY\n"
        "open('/work/result','x').write('outer-enforced')\n"
        "assert not os.path.exists('/run/user')\n"
        "print('outer-enforced')\n"
    )
    command = module.sandbox_command(*inputs, model="gpt-6.1-sol", owner="retained-owner")
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode and any(
        message in result.stderr
        for message in (
            "No permissions to create new namespace",
            "Creating new namespace failed",
            "Operation not permitted",
        )
    ):
        pytest.skip("test host does not admit user namespaces")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "outer-enforced"
    assert (inputs[0] / "result").read_text() == "outer-enforced"
    assert inputs[2].read_text() == "{}"
