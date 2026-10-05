"""Collected native starts need exact journal custody before RAM is discharged."""

import json
import subprocess

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_admission as admission

START = "39f53479d3a045ac8e11786248231fbf"
TERMINAL = "ae8f7b866b0347b9af31fe1c80b127c0"


@pytest.fixture
def collected(tmp_path, monkeypatch):
    """Consume one unique native start without ever observing its live unit."""
    host = "fixture"
    (tmp_path / "fleet").mkdir()
    binding = dict(
        card_id="12345678",
        owner="fixture",
        claim_revision="a" * 32,
        request_id="b" * 64,
        attempt=1,
    )
    unit = "skfleet-builder-12345678-" + binding["request_id"] + "-1.service"
    limits = dict(
        cpu_quota_percent=200, memory_max_bytes=1024, tasks_max=256, runtime_max_seconds=3600
    )
    policy = {"node_quotas": {host: limits}}
    CardStore(tmp_path).create(
        CardCore(
            id=binding["card_id"],
            title="Synthetic source",
            initial_owner=binding["owner"],
            initial_claim_revision=binding["claim_revision"],
        )
    )
    monkeypatch.setattr(admission.socket, "gethostname", lambda: host)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *a: (True, "ample RAM"))
    state = dict(
        Id=unit,
        LoadState="not-found",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        ControlPID="0",
        TasksCurrent="[not set]",
        ControlGroup="",
    )
    monkeypatch.setattr(admission, "unit_state", lambda *a, **k: dict(state))
    command = [
        "systemd-run",
        "--user",
        "--unit=" + unit,
        "--property=CPUQuota=200%",
        "--property=MemoryMax=1024",
        "--property=TasksMax=256",
        "--property=RuntimeMaxSec=3600",
        "--",
        "/bin/true",
    ]
    argv = admission.reserve_launch(tmp_path, policy, host, unit, binding, command)
    admission.start_reserved(tmp_path, host, argv, lambda argv: None)
    root = tmp_path / "fleet/resource-admission" / host
    directory = next(p for p in root.iterdir() if p.is_dir())
    entries = [
        dict(
            USER_UNIT=unit,
            USER_INVOCATION_ID="c" * 32,
            MESSAGE_ID=START,
            __REALTIME_TIMESTAMP="100",
        ),
        dict(
            USER_UNIT=unit,
            USER_INVOCATION_ID="c" * 32,
            MESSAGE_ID=TERMINAL,
            __REALTIME_TIMESTAMP="200",
        ),
    ]
    calls = []

    def journal(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "journalctl"
        return subprocess.CompletedProcess(
            argv, 0, stdout="\n".join(json.dumps(e) for e in entries)
        )

    monkeypatch.setattr(admission.subprocess, "run", journal)
    return tmp_path, root, directory, state, entries, calls, unit


@pytest.mark.parametrize("strict", [False, True])
def test_exact_unobserved_native_start_and_terminal_free_only_ram(collected, monkeypatch, strict):
    home, root, directory, state, entries, calls, unit = collected
    before = CardStore(home).fold("12345678").model_dump(mode="json")
    assert admission._occupancy(root, home, strict_terminal=strict) == []
    observed = admission.read_json(directory / "observed.json")
    assert observed["invocation"] == "c" * 32
    proof = admission.read_json(directory / "journal-terminal.json")
    assert proof["start_realtime_us"] == "100"
    assert len(proof["start_entry_sha256"]) == 64
    assert CardStore(home).fold("12345678").model_dump(mode="json") == before
    assert (directory / "intent.json").exists() and (directory / "start.json").exists()
    assert len(calls) == 1
    monkeypatch.setattr(admission.subprocess, "run", lambda *a, **k: pytest.fail("journal replay"))
    assert admission._occupancy(root, home, strict_terminal=strict) == []


@pytest.mark.parametrize(
    "defect",
    [
        "no-start",
        "wrong-start-binding",
        "missing-journal-start",
        "missing-terminal",
        "wrong-invocation",
        "ambiguous-invocation",
        "duplicate-start",
        "terminal-before-start",
        "different-unit",
        "loaded-reused-unit",
        "active-unit",
        "journal-timeout",
        "malformed-journal",
        "missing-time",
        "unit-changed-during-proof",
    ],
)
def test_uncertain_unobserved_start_retains_charge(collected, monkeypatch, defect):
    home, root, directory, state, entries, calls, unit = collected
    if defect == "no-start":
        (directory / "start.json").unlink()
    elif defect == "wrong-start-binding":
        start = admission.read_json(directory / "start.json")
        start["binding"]["claim_revision"] = "f" * 32
        (directory / "start.json").write_text(json.dumps(start))
    elif defect == "missing-journal-start":
        entries.pop(0)
    elif defect == "missing-terminal":
        entries.pop()
    elif defect == "wrong-invocation":
        entries[1]["USER_INVOCATION_ID"] = "f" * 32
    elif defect == "ambiguous-invocation":
        entries.append(entries[0] | {"USER_INVOCATION_ID": "f" * 32})
    elif defect == "duplicate-start":
        entries.append(dict(entries[0]))
    elif defect == "terminal-before-start":
        entries[1]["__REALTIME_TIMESTAMP"] = "99"
    elif defect == "different-unit":
        entries[0]["USER_UNIT"] = "another.service"
    elif defect == "loaded-reused-unit":
        state["LoadState"] = "loaded"
        state["InvocationID"] = "f" * 32
    elif defect == "active-unit":
        state["ActiveState"] = "active"
    elif defect == "journal-timeout":

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 5)

        monkeypatch.setattr(admission.subprocess, "run", timeout)
    elif defect == "malformed-journal":
        monkeypatch.setattr(
            admission.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="not json"),
        )
    elif defect == "missing-time":
        entries[1].pop("__REALTIME_TIMESTAMP")
    elif defect == "unit-changed-during-proof":
        states = iter([dict(state), state | {"LoadState": "loaded", "ActiveState": "active"}])
        monkeypatch.setattr(admission, "unit_state", lambda *a, **k: next(states))
    assert admission._occupancy(root, home) == [{"unit": unit, "reserved_memory_max": 1024}]
    assert not (directory / "observed.json").exists()
    assert not (directory / "journal-terminal.json").exists()


def test_reusable_worker_unit_is_not_recovered_from_inferred_invocation(collected):
    home, root, directory, state, entries, calls, unit = collected
    intent = admission.read_json(directory / "intent.json")
    intent["unit"] = "skfleet-worker-glm-12345678.service"
    assert admission._recover_unobserved_builder(directory, intent) is False
    assert calls == []


def test_next_native_offer_is_admitted_without_releasing_source_claim(collected, monkeypatch):
    home, root, directory, state, entries, calls, unit = collected
    monkeypatch.setattr(
        admission,
        "local_worker_admission",
        lambda policy, host, rows: (not rows, "memory_available=0 required=1024"),
    )
    binding = dict(
        card_id="87654321", owner="next", claim_revision="d" * 32, request_id="e" * 64, attempt=1
    )
    CardStore(home).create(
        CardCore(
            id=binding["card_id"],
            title="Next source",
            initial_owner=binding["owner"],
            initial_claim_revision=binding["claim_revision"],
        )
    )
    intent = admission.read_json(directory / "intent.json")
    unit = "skfleet-builder-87654321-" + binding["request_id"] + "-1.service"
    command = [
        "systemd-run",
        "--user",
        "--unit=" + unit,
        "--property=CPUQuota=200%",
        "--property=MemoryMax=1024",
        "--property=TasksMax=256",
        "--property=RuntimeMaxSec=3600",
        "--",
        "/bin/true",
    ]
    argv = admission.reserve_launch(
        home, {"node_quotas": {"fixture": intent["resources"]}}, "fixture", unit, binding, command
    )
    assert any(arg.startswith("--setenv=SKFLEET_ADMISSION_ID=") for arg in argv)
    assert CardStore(home).fold("12345678").owner == "fixture"
    assert len(list(root.glob("*/intent.json"))) == 2


def test_cached_journal_terminal_skips_per_unit_systemctl(collected, monkeypatch):
    home, root, directory, state, entries, calls, unit = collected
    assert admission._occupancy(root, home, strict_terminal=True) == []
    monkeypatch.setattr(
        admission, "unit_state", lambda *a, **k: pytest.fail("cached terminal queried systemctl")
    )
    assert admission._occupancy(root, home, strict_terminal=True) == []
    assert len(calls) == 1


def test_cached_journal_terminal_binding_still_checked(collected):
    home, root, directory, state, entries, calls, unit = collected
    assert admission._occupancy(root, home, strict_terminal=True) == []
    proof = admission.read_json(directory / "journal-terminal.json")
    proof["invocation"] = "f" * 32
    (directory / "journal-terminal.json").write_text(json.dumps(proof))
    with pytest.raises(admission.AdmissionError, match="journal terminal proof differs"):
        admission._occupancy(root, home, strict_terminal=True)


def test_cached_terminal_never_removes_reused_live_unit(collected, monkeypatch):
    home, root, directory, state, entries, calls, unit = collected
    assert admission._occupancy(root, home, strict_terminal=True) == []
    live = dict(unit=unit, reserved_memory_max=2048)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [live])
    assert admission._occupancy(root, home, strict_terminal=True) == [live]
