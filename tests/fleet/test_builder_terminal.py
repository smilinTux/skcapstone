"""Collected units require exact trusted terminal evidence and current absence."""

import json
import os
import time
from copy import deepcopy

import pytest

from skcapstone.fleet import builder_terminal as terminal
from tests.fleet.test_builder_retry import attempt as attempt


@pytest.fixture
def collected(attempt, monkeypatch):
    a = attempt
    a.status.update(pid=12345, pid_start_ticks="100")
    a.boot = "e" * 32
    a.values = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "MainPID": "0",
        "InvocationID": "",
        "SubState": "dead",
    }
    shared = {
        "_BOOT_ID": a.boot,
        "_UID": str(os.getuid()),
        "_PID": "99",
        "_EXE": "/usr/lib/systemd/systemd",
        "_SYSTEMD_CGROUP": f"/user.slice/user-{os.getuid()}.slice/"
        f"user@{os.getuid()}.service/init.scope",
        "USER_UNIT": a.status["unit"],
        "USER_INVOCATION_ID": a.status["invocation"],
    }
    a.rows = [
        {
            **shared,
            "MESSAGE_ID": terminal.START_MESSAGE,
            "JOB_TYPE": "start",
            "JOB_RESULT": "done",
            "__CURSOR": "start",
            "__MONOTONIC_TIMESTAMP": "2000000",
        },
        {
            **shared,
            "MESSAGE_ID": terminal.RESOURCE_MESSAGE,
            "CODE_FILE": "src/core/unit.c",
            "CODE_FUNC": "unit_log_resources",
            "__CURSOR": "terminal",
            "__MONOTONIC_TIMESTAMP": "3000000",
        },
    ]
    a.version = terminal.QUALIFICATION["version"]
    monkeypatch.setattr(
        terminal.custody, "prove_dead", lambda *args: (_ for _ in ()).throw(ValueError("unknown"))
    )
    monkeypatch.setattr(terminal, "boot_id", lambda: a.boot)
    monkeypatch.setattr(terminal, "absent", lambda status: None)

    def command(argv):
        if argv[0] == "systemctl":
            return "\n".join(k + "=" + v for k, v in a.values.items())
        if argv[0] == "journalctl":
            return "\n".join(json.dumps(row) for row in a.rows)
        assert argv[0] == "dpkg-query"
        return a.version

    monkeypatch.setattr(terminal, "command", command)
    return a


def test_journal_check_only_then_private_durable_exact_receipt(collected):
    a = collected
    target = terminal.path(a.home, a.status)
    terminal.prove(a.home, a.status)
    assert not target.exists()
    terminal.prove(a.home, a.status, apply=True)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    raw = target.read_bytes()
    receipt = json.loads(raw)
    assert receipt["entry"] == a.rows[-1]
    assert receipt["entry_sha256"] == terminal.custody.sha(terminal.custody.encoded(a.rows[-1]))
    assert "exit_code" not in receipt
    terminal.prove(a.home, a.status, apply=True)
    assert target.read_bytes() == raw


def test_missing_invocation_recovers_only_exact_collected_generation(collected):
    a = collected
    expected = a.status["invocation"]
    a.status["invocation"] = None
    recovered = terminal.recover_collected(a.home, a.status)
    assert a.status["invocation"] is None
    assert recovered["invocation"] == expected
    assert json.loads(terminal.path(a.home, recovered).read_bytes())["kind"] == "qualified-journal"
    terminal.prove(a.home, recovered)


@pytest.mark.parametrize(
    "change",
    ["second-invocation", "invalid-invocation", "unhashable-invocation", "malformed", "live"],
)
def test_missing_invocation_recovery_refuses_ambiguous_or_live_generation(collected, change):
    a = collected
    a.status["invocation"] = None
    if change == "second-invocation":
        a.rows[-1]["USER_INVOCATION_ID"] = "f" * 32
    elif change == "invalid-invocation":
        a.rows[-1]["USER_INVOCATION_ID"] = "invalid"
    elif change == "unhashable-invocation":
        a.rows[-1]["USER_INVOCATION_ID"] = []
    elif change == "malformed":
        a.rows.append("not-an-object")
    else:
        a.values.update(LoadState="loaded", ActiveState="active", MainPID="9")
    with pytest.raises(ValueError):
        terminal.recover_collected(a.home, a.status)
    assert a.status["invocation"] is None


@pytest.mark.parametrize("missing_invocation", [True, False])
def test_dispatcher_preserves_recovered_terminal_and_source_custody(
    collected, monkeypatch, missing_invocation
):
    from skcapstone.fleet import builder_continue, production_exit, source_bundle
    from skcapstone.fleet import builder_dispatch as builder

    a = collected
    a.status.update(state="running", invocation=None if missing_invocation else "d" * 32)
    monkeypatch.setattr(builder, "_process_state", lambda status: (None, None))
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: a.card)
    monkeypatch.setattr(builder_continue, "original_outcome_pending", lambda *args: False)
    monkeypatch.setattr(production_exit, "release_blocked", lambda *args: None)
    monkeypatch.setattr(
        source_bundle, "publish_source", lambda *args, **kwargs: {"manifest_sha256": "f" * 64}
    )

    result = builder._reconcile_running(a.paths, a.home, "node-worker", a.request, a.status)
    assert result["state"] == "awaiting-review"
    assert result["terminal_proof"] == "qualified-terminal"
    assert result["invocation"] == "d" * 32
    assert result["exit_code"] is None
    again = builder._reconcile_running(a.paths, a.home, "node-worker", a.request, result)
    assert again["state"] == "awaiting-review"
    assert again["terminal_proof"] == "qualified-terminal"


@pytest.mark.parametrize(
    "change",
    [
        "boot",
        "uid",
        "exe",
        "cgroup",
        "unit",
        "invocation",
        "message",
        "function",
        "cursor",
        "stale",
        "later",
        "empty",
        "oversized",
        "ambiguous",
        "version",
        "live",
        "unknown",
        "birth",
    ],
)
def test_spoof_stale_mismatch_later_live_unknown_refuse(collected, change):
    a = collected
    row = a.rows[-1]
    field = {
        "boot": "_BOOT_ID",
        "uid": "_UID",
        "exe": "_EXE",
        "cgroup": "_SYSTEMD_CGROUP",
        "unit": "USER_UNIT",
        "invocation": "USER_INVOCATION_ID",
        "message": "MESSAGE_ID",
        "function": "CODE_FUNC",
        "cursor": "__CURSOR",
    }.get(change)
    if field:
        row[field] = ""
    elif change == "stale":
        row["__MONOTONIC_TIMESTAMP"] = "1"
    elif change == "birth":
        a.status["pid_start_ticks"] = "9999999999"
    elif change == "later":
        a.rows.append({**row, "USER_INVOCATION_ID": "f" * 32, "__MONOTONIC_TIMESTAMP": "4000000"})
    elif change == "empty":
        a.rows.clear()
    elif change in {"oversized", "ambiguous"}:
        a.rows.extend([deepcopy(row)] * (64 if change == "oversized" else 1))
    elif change == "version":
        a.version = "other"
    elif change == "live":
        a.values.update(LoadState="loaded", ActiveState="active", MainPID="9")
    elif change == "unknown":
        a.values.clear()
    with pytest.raises(ValueError):
        terminal.prove(a.home, a.status, apply=True)
    assert not terminal.path(a.home, a.status).exists()


@pytest.mark.parametrize("change", ["claim", "boot", "entry", "mode", "symlink", "later"])
def test_receipt_drift_rejected(collected, change):
    a = collected
    terminal.prove(a.home, a.status, apply=True)
    target = terminal.path(a.home, a.status)
    if change == "claim":
        a.status["claim_revision"] = "f" * 32
    elif change == "boot":
        a.boot = "f" * 32
    elif change == "entry":
        a.rows[-1]["MESSAGE"] = "changed bytes"
    elif change == "mode":
        target.chmod(0o644)
    elif change == "symlink":
        moved = target.with_suffix(".moved")
        target.rename(moved)
        target.symlink_to(moved)
    else:
        a.rows.append(
            {**a.rows[-1], "USER_INVOCATION_ID": "f" * 32, "__MONOTONIC_TIMESTAMP": "4000000"}
        )
    with pytest.raises((ValueError, OSError)):
        terminal.prove(a.home, a.status)


def test_native_first_terminal_snapshot_reused_after_collection(collected):
    a = collected
    a.values.update(LoadState="loaded", InvocationID=a.status["invocation"], ExecMainStatus="128")
    terminal.observe(a.home, a.status)
    target = terminal.path(a.home, a.status)
    raw = target.read_bytes()
    terminal.observe(a.home, a.status)
    assert raw == target.read_bytes()
    a.values.update(LoadState="not-found", InvocationID="")
    a.rows = a.rows[:1]  # The durable snapshot does not require a resource event.
    a.version = "unqualified-new-version"
    terminal.prove(a.home, a.status)
    receipt = json.loads(raw)
    assert receipt["kind"] == "native-loaded"
    assert receipt["snapshot"]["ExecMainStatus"] == "128"


def test_existing_loaded_guard_is_preserved(collected, monkeypatch):
    a = collected
    monkeypatch.setattr(terminal.custody, "prove_dead", lambda status: None)
    monkeypatch.setattr(terminal, "snapshot", lambda status: pytest.fail("unexpected fallback"))
    terminal.prove(a.home, a.status)


def test_actual_absence_checks_refuse_live_denied_and_cgroup(attempt, monkeypatch):
    a = attempt
    a.status.update(pid=os.getpid(), pid_start_ticks="100")
    with pytest.raises(ValueError, match="PID exists"):
        terminal.absent(a.status)
    monkeypatch.setattr(os, "kill", lambda *args: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(ValueError, match="unavailable"):
        terminal.absent(a.status)
    monkeypatch.setattr(os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(terminal.Path, "lstat", lambda path: object())
    with pytest.raises(ValueError, match="cgroup"):
        terminal.absent(a.status)


def test_future_timestamp_in_native_receipt_refused(collected):
    a = collected
    a.values.update(LoadState="loaded", InvocationID=a.status["invocation"])
    terminal.observe(a.home, a.status)
    target = terminal.path(a.home, a.status)
    receipt = json.loads(target.read_bytes())
    receipt["monotonic_usec"] = time.monotonic_ns() // 1000 + 10**12
    target.write_text(json.dumps(receipt))
    a.values.update(LoadState="not-found", InvocationID="")
    with pytest.raises(ValueError):
        terminal.prove(a.home, a.status)


def test_dispatcher_persists_first_loaded_terminal_observation(collected, monkeypatch):
    from skcapstone.fleet import builder_dispatch as builder

    a = collected
    a.values.update(LoadState="loaded", InvocationID=a.status["invocation"], ExecMainStatus="128")
    a.card.status.value = "done"
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: a.card)
    result = builder._reconcile_running(a.paths, a.home, "node-worker", a.request, a.status)
    assert result["state"] == "completed"
    assert json.loads(terminal.path(a.home, a.status).read_bytes())["kind"] == "native-loaded"


@pytest.mark.parametrize("later", [False, True])
def test_continuation_checks_terminal_custody_before_source_preservation(
    collected, monkeypatch, later
):
    from skcapstone.fleet import builder_continue as continuation

    a = collected
    workspace = a.paths.root / "workspaces" / a.status["owner"]
    workspace.mkdir(parents=True)
    (workspace / "staged.txt").write_text("preserved source")
    monkeypatch.setattr(continuation.source_bundle, "_inspect", lambda *args: {"head": "b" * 40})
    if later:
        a.rows.append(
            {**a.rows[-1], "USER_INVOCATION_ID": "f" * 32, "__MONOTONIC_TIMESTAMP": "4000000"}
        )
        with pytest.raises(ValueError):
            continuation.source_proof(a.paths, a.request, a.status, a.home / "archive", apply=True)
        assert not (a.home / "archive").exists()
    else:
        proof = continuation.source_proof(a.paths, a.request, a.status, a.home / "archive")
        assert proof["source"]["head"] == "b" * 40
        assert not terminal.path(a.home, a.status).exists()
