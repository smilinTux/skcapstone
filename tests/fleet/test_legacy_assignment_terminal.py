"""Legacy assignment death must preserve claims and require generation evidence."""

import json
import subprocess
from datetime import datetime, timezone

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_admission as admission


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Reserve a historical assignment which bypassed start_reserved."""
    host = "fixture"
    (tmp_path / "fleet").mkdir()
    binding = dict(card_id="12345678", owner="pi-glm-fixture-12345678", claim_revision="a" * 32)
    unit = "skfleet-worker-glm-12345678.service"
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="12345678",
            title="Synthetic source",
            initial_owner=binding["owner"],
            initial_claim_revision=binding["claim_revision"],
        )
    )
    monkeypatch.setattr(admission.socket, "gethostname", lambda: host)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "ample RAM"))
    limits = dict(
        cpu_quota_percent=200, memory_max_bytes=1024, tasks_max=256, runtime_max_seconds=3600
    )
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
    admission.reserve_launch(
        tmp_path, {"node_quotas": {host: limits}}, host, unit, binding, command
    )
    root = tmp_path / "fleet/resource-admission" / host
    directory = next(p for p in root.iterdir() if p.is_dir())
    state = dict(
        Id=unit,
        LoadState="not-found",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        ControlPID="0",
        ControlGroup="",
        TasksCurrent="[not set]",
        InvocationID="",
    )
    monkeypatch.setattr(admission, "unit_state", lambda *args, **kw: dict(state))
    now = datetime.now(timezone.utc)
    event = dict(
        action="production_assignment_launch",
        schema="skfleet.production-assignment-launch/v1",
        event_id="d" * 32,
        ts=now.isoformat(),
        node=host,
        writer=binding["owner"],
        worker=binding["owner"],
        claim_revision=binding["claim_revision"],
        launched=True,
    )
    events = [event]
    monkeypatch.setattr(CardStore, "_read_events", lambda self, cid: list(events))
    stamp = int(now.timestamp() * 1_000_000)
    entries = [
        dict(
            USER_UNIT=unit,
            USER_INVOCATION_ID="c" * 32,
            MESSAGE_ID="39f53479d3a045ac8e11786248231fbf",
            __REALTIME_TIMESTAMP=str(stamp - 500_000),
        ),
        dict(
            USER_UNIT=unit,
            USER_INVOCATION_ID="c" * 32,
            MESSAGE_ID="ae8f7b866b0347b9af31fe1c80b127c0",
            __REALTIME_TIMESTAMP=str(stamp + 30_000_000),
        ),
    ]
    monkeypatch.setattr(
        admission.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="\n".join(json.dumps(e) for e in entries)
        ),
    )
    return tmp_path, root, directory, state, events, entries


def test_terminal_assignment_releases_only_capacity(legacy, monkeypatch):
    home, root, directory, state, events, entries = legacy
    before = CardStore(home).fold("12345678").model_dump(mode="json")
    assert admission._occupancy(root, home, strict_terminal=True) == []
    assert (directory / "legacy-assignment-terminal.json").is_file()
    assert not (directory / "start.json").exists()
    assert not (directory / "observed.json").exists()
    assert not (directory / "released-prestart.json").exists()
    assert CardStore(home).fold("12345678").model_dump(mode="json") == before
    monkeypatch.setattr(
        admission.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("cached evidence queried journal again"),
    )
    assert admission._occupancy(root, home, strict_terminal=True) == []


@pytest.mark.parametrize(
    "defect",
    [
        "missing-launch",
        "wrong-owner",
        "wrong-claim",
        "wrong-host",
        "duplicate-launch",
        "missing-start",
        "missing-terminal",
        "multiple-invocations",
        "duplicate-start",
        "terminal-before-start",
        "start-outside-launch",
        "unit-loaded",
        "process-present",
        "journal-malformed",
        "journal-timeout",
        "state-changed",
    ],
)
def test_uncertain_assignment_stays_charged(legacy, monkeypatch, defect):
    home, root, directory, state, events, entries = legacy
    if defect == "missing-launch":
        events.clear()
    elif defect == "wrong-owner":
        events[0]["worker"] = "someone-else"
    elif defect == "wrong-claim":
        events[0]["claim_revision"] = "f" * 32
    elif defect == "wrong-host":
        events[0]["node"] = "other"
    elif defect == "duplicate-launch":
        events.append(dict(events[0]))
    elif defect == "missing-start":
        entries.pop(0)
    elif defect == "missing-terminal":
        entries.pop()
    elif defect == "multiple-invocations":
        entries[1]["USER_INVOCATION_ID"] = "f" * 32
    elif defect == "duplicate-start":
        entries.insert(0, dict(entries[0]))
    elif defect == "terminal-before-start":
        entries[1]["__REALTIME_TIMESTAMP"] = "1"
    elif defect == "start-outside-launch":
        entries[0]["__REALTIME_TIMESTAMP"] = "1"
    elif defect == "unit-loaded":
        state["LoadState"] = "loaded"
    elif defect == "process-present":
        state["MainPID"] = "42"
    elif defect == "journal-malformed":
        monkeypatch.setattr(
            admission.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="{"),
        )
    elif defect == "journal-timeout":

        def timeout(*args, **kw):
            raise subprocess.TimeoutExpired("journalctl", 5)

        monkeypatch.setattr(admission.subprocess, "run", timeout)
    elif defect == "state-changed":
        calls = []

        def changing(*args, **kw):
            calls.append(1)
            return dict(state) if len(calls) == 1 else dict(state, MainPID="42")

        monkeypatch.setattr(admission, "unit_state", changing)
    assert len(admission._occupancy(root, home, strict_terminal=True)) == 1
    assert not (directory / "legacy-assignment-terminal.json").exists()


def test_changed_native_launch_invalidates_cached_proof(legacy):
    home, root, directory, state, events, entries = legacy
    assert admission._occupancy(root, home, strict_terminal=True) == []
    events[0]["ts"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(admission.AdmissionError, match="legacy assignment terminal"):
        admission._occupancy(root, home, strict_terminal=True)


def test_launched_without_terminal_never_uses_prestart_reconciliation(legacy, monkeypatch):
    home, root, directory, state, events, entries = legacy
    entries.pop()
    monkeypatch.setattr(
        admission,
        "_released_prestart_proof",
        lambda *a: pytest.fail("launched assignment misclassified as prestart"),
    )
    assert len(admission._occupancy(root, home, strict_terminal=True)) == 1


@pytest.mark.parametrize("production,fenced", [(True, True), (False, True), (True, False)])
def test_dispatcher_consumes_reservation_before_spawn(production, fenced):
    import ast
    from pathlib import Path
    from types import SimpleNamespace

    source = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(source.read_text())
    branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "PRODUCTION_POLICY"
        and node.body
        and isinstance(node.body[0], ast.Try)
        and isinstance(node.body[0].body[0], ast.Assign)
        and isinstance(node.body[0].body[0].value, ast.Call)
        and isinstance(node.body[0].body[0].value.func, ast.Name)
        and node.body[0].body[0].value.func.id == "start_reserved"
    )
    calls = []
    logs = []
    argv = ["systemd-run", "reserved-marker"]

    def run(command, **kwargs):
        calls.append(("spawn", command))
        return subprocess.CompletedProcess(command, 0)

    def starter(home, host, command, spawn):
        calls.append(("fence", command))
        if not fenced:
            raise admission.AdmissionError("claim changed")
        return spawn(command)

    loop = ast.For(
        target=ast.Name(id="once", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()),
        body=[branch],
        orelse=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[]))
    namespace = dict(
        PRODUCTION_POLICY=production,
        Path=Path,
        HOME="/synthetic",
        HOST="fixture",
        _launch_argv=argv,
        start_reserved=starter,
        AdmissionError=admission.AdmissionError,
        subprocess=SimpleNamespace(run=run),
        log=lambda *args: logs.append(args),
        d="synthetic",
        cid="12345678",
    )
    exec(compile(module, str(source), "exec"), namespace)
    assert [kind for kind, command in calls] == (
        ["fence", "spawn"] if production and fenced else ["fence"] if production else ["spawn"]
    )
    assert all(command is argv for kind, command in calls)
    if production and not fenced:
        assert "r" not in namespace
        assert logs[0][1] == "NODE_ADMISSION_CUSTODY_REQUIRED|fixture|12345678"
    else:
        assert namespace["r"].returncode == 0


@pytest.fixture
def fenced_without_launch_receipt(legacy):
    home, root, directory, state, events, entries = legacy
    intent = admission.read_json(directory / "intent.json")
    binding = intent["binding"]
    events[:] = [dict(
        action="claim", event_id="e" * 32, ts=events[0]["ts"],
        writer=binding["owner"], node=intent["host"], owner=binding["owner"],
        claim_revision=binding["claim_revision"],
    )]
    entries[0]["__REALTIME_TIMESTAMP"] = str(int(entries[0]["__REALTIME_TIMESTAMP"]) + 1_000_000)
    entries[0]["MESSAGE"] = (
        f"Started {intent['unit']} - /synthetic/bin/python3 "
        "/synthetic/bin/skfleet-worker-wrapper.py --card 12345678 "
        f"--owner {binding['owner']} --claim-revision {binding['claim_revision']} "
        "--host fixture --lane glm --model sk-glm-m -- bash -lc 'synthetic payload'"
    )
    admission.write_once(directory / "start.json", dict(
        schema="skfleet.resource-start/v1", reservation_id=directory.name,
        binding=binding, argv_sha256=intent["argv_sha256"],
    ))
    return legacy


def test_consumed_assignment_without_launch_receipt_recovers_only_capacity(fenced_without_launch_receipt):
    home, root, directory, state, events, entries = fenced_without_launch_receipt
    before = CardStore(home).fold("12345678").model_dump(mode="json")
    assert admission._occupancy(root, home, strict_terminal=True) == []
    proof = admission.read_json(directory / "fenced-assignment-terminal.json")
    assert proof["schema"] == "skfleet.fenced-assignment-terminal/v1"
    assert proof["claim_event_sha256"] == admission._digest(events[0])
    assert proof["start_receipt_sha256"] == admission._digest(admission.read_json(directory / "start.json"))
    assert proof["invocation"] == "c" * 32
    assert not (directory / "legacy-assignment-terminal.json").exists()
    assert not (directory / "observed.json").exists()
    assert not (directory / "released-prestart.json").exists()
    assert CardStore(home).fold("12345678").model_dump(mode="json") == before
    assert [e["action"] for e in events] == ["claim"]
    assert admission._occupancy(root, home, strict_terminal=True) == []


@pytest.mark.parametrize("defect", [
    "no-start", "wrong-start", "missing-claim", "wrong-claim", "duplicate-claim",
    "wrong-wrapper", "wrong-card", "wrong-owner", "wrong-host", "wrong-lane",
    "duplicate-option", "missing-description", "start-before-claim", "missing-terminal",
    "second-invocation", "reused-unit", "claim-changed-during-observation",
])
def test_uncertain_fenced_assignment_retains_capacity(fenced_without_launch_receipt, monkeypatch, defect):
    home, root, directory, state, events, entries = fenced_without_launch_receipt
    if defect == "no-start":
        (directory / "start.json").unlink()
    elif defect == "wrong-start":
        start = admission.read_json(directory / "start.json")
        start["argv_sha256"] = "f" * 64
        (directory / "start.json").write_text(json.dumps(start))
    elif defect == "missing-claim":
        events.clear()
    elif defect == "wrong-claim":
        events[0]["claim_revision"] = "f" * 32
    elif defect == "duplicate-claim":
        events.append(dict(events[0]))
    elif defect.startswith("wrong-"):
        values = {"wrapper": ("skfleet-worker-wrapper.py", "other.py"),
                  "card": ("12345678", "87654321"),
                  "owner": ("pi-glm-fixture-12345678", "someone-else"),
                  "host": ("--host fixture", "--host other"),
                  "lane": ("--lane glm", "--lane codex")}
        old, new = values[defect[6:]]
        entries[0]["MESSAGE"] = entries[0]["MESSAGE"].replace(old, new)
    elif defect == "duplicate-option":
        entries[0]["MESSAGE"] = entries[0]["MESSAGE"].replace(" -- bash", " --card 12345678 -- bash")
    elif defect == "missing-description":
        entries[0].pop("MESSAGE")
    elif defect == "start-before-claim":
        entries[0]["__REALTIME_TIMESTAMP"] = "1"
    elif defect == "missing-terminal":
        entries.pop()
    elif defect == "second-invocation":
        entries.append(dict(entries[0], USER_INVOCATION_ID="f" * 32))
    elif defect == "reused-unit":
        state.update(LoadState="loaded", InvocationID="f" * 32)
    elif defect == "claim-changed-during-observation":
        original = CardStore._read_events
        calls = []
        def changing(self, cid):
            calls.append(1)
            rows = original(self, cid)
            return rows if len(calls) < 4 else [dict(rows[0], claim_revision="f" * 32)]
        monkeypatch.setattr(CardStore, "_read_events", changing)
    assert len(admission._occupancy(root, home, strict_terminal=True)) == 1
    assert not (directory / "fenced-assignment-terminal.json").exists()
