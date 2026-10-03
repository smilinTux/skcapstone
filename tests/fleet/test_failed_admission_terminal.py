"""Unknown pending launches remain charged; only exact failed custody releases."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_admission as admission
from tests.fleet.test_production_admission import (
    BINDING,
    HOST,
    LIMITS,
    POLICY,
    UNIT,
    command,
    reserve,
)
from tests.fleet.test_production_admission import capacity as capacity

REVISION = "b" * 64
INVOCATION = "c" * 32


@pytest.fixture
def failed(capacity, monkeypatch):
    """Provide an exact retained failed invocation with no real OS mutations."""
    argv = reserve(capacity)
    identity = argv[1].split("=", 2)[2]
    card = SimpleNamespace(
        archived=False,
        meta={"_claim_revision": BINDING["claim_revision"]},
        status=SimpleNamespace(value="doing"),
        owner=BINDING["owner"],
    )
    monkeypatch.setattr(
        admission, "CardStore", lambda home: SimpleNamespace(fold=lambda card_id: card)
    )
    monkeypatch.setattr(admission, "card_mutation_lock", lambda *args: nullcontext())
    monkeypatch.setattr(admission, "card_revision", lambda card: REVISION)
    state = dict(
        Id=UNIT,
        LoadState="loaded",
        ActiveState="failed",
        SubState="failed",
        InvocationID=INVOCATION,
        SKFLEET_ADMISSION_ID=identity,
        MemoryMax=str(LIMITS["memory_max_bytes"]),
        MemoryCurrent="[not set]",
        MainPID="0",
        ControlPID="0",
        ExecMainPID="999999999",
        ExecMainCode="1",
        ExecMainStatus="203",
        ControlGroup="",
        TasksCurrent="[not set]",
        Result="exit-code",
    )
    monkeypatch.setattr(admission, "unit_state", lambda *args, **kwargs: dict(state))
    return SimpleNamespace(
        home=capacity,
        state=state,
        card=card,
        identity=identity,
        directory=capacity / "fleet/resource-admission" / HOST / identity,
    )


def finish(failed, **changes):
    """Call the public finalizer with the complete immutable launch contract."""
    args = dict(
        home=failed.home,
        policy=POLICY,
        host=HOST,
        unit=UNIT,
        binding=BINDING,
        argv=command(),
        invocation=INVOCATION,
        expected_card_revision=REVISION,
    )
    return admission.finalize_failed_launch(**(args | changes))


def rows(failed):
    """Read accounting through the same locked observation used by new callers."""
    with admission._transaction(failed.home, HOST) as root:
        return admission._occupancy(root, failed.home)


def test_exact_failed_spawn_finalizes_without_replay_or_claim_release(failed):
    assert rows(failed) == [{"unit": UNIT, "reserved_memory_max": LIMITS["memory_max_bytes"]}]
    proof = finish(failed)
    assert proof["state"]["ExecMainStatus"] == "203"
    assert rows(failed) == []
    assert finish(failed) == proof
    assert failed.card.owner == BINDING["owner"]
    assert (failed.directory / "intent.json").exists()
    assert not (failed.directory / "observed.json").exists()
    with pytest.raises(admission.AdmissionError, match="already reserved"):
        reserve(failed.home)
    failed.card.meta["_claim_revision"] = "new"
    reserve(failed.home, dict(BINDING, claim_revision="new"))


@pytest.mark.parametrize(
    "change",
    [
        {"LoadState": "not-found", "ActiveState": "inactive"},
        {"ActiveState": "active", "SubState": "running"},
        {"ActiveState": "activating"},
        {"InvocationID": "d" * 32},
        {"SKFLEET_ADMISSION_ID": "e" * 64},
        {"MemoryMax": "2048"},
        {"MainPID": "123"},
        {"ControlPID": "456"},
        {"ExecMainPID": "0"},
        {"ExecMainStatus": "0"},
        {"ExecMainCode": "0"},
        {"Result": "success"},
        {"TasksCurrent": "1"},
        {"ControlGroup": "/../../escape", "TasksCurrent": "0"},
        {"Id": "skfleet-worker-other.service"},
    ],
)
def test_unknown_or_wrong_terminal_state_preserves_pending(failed, change):
    failed.state.update(change)
    with pytest.raises(admission.AdmissionError):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()
    assert rows(failed)[0]["reserved_memory_max"] == LIMITS["memory_max_bytes"]


@pytest.mark.parametrize(
    "change",
    [
        {"binding": dict(BINDING, owner="other")},
        {"binding": dict(BINDING, source_head="changed")},
        {"argv": command() + ["changed"]},
        {"expected_card_revision": "e" * 64},
    ],
)
def test_changed_exact_binding_or_revision_refuses(failed, change):
    with pytest.raises((admission.AdmissionError, FileNotFoundError)):
        finish(failed, **change)
    assert not (failed.directory / "failed-terminal.json").exists()


@pytest.mark.parametrize("change", ["owner", "claim", "status", "archived", "conflict"])
def test_changed_native_custody_refuses(failed, change):
    if change == "owner":
        failed.card.owner = "other"
    elif change == "claim":
        failed.card.meta["_claim_revision"] = "other"
    elif change == "status":
        failed.card.status.value = "done"
    elif change == "archived":
        failed.card.archived = True
    else:
        failed.card.meta["claim_conflicts"] = ["other"]
    with pytest.raises(admission.AdmissionError, match="custody"):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()


def test_process_or_cgroup_population_refuses(failed, monkeypatch):
    original = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: True if str(path) == "/proc/999999999" else original(path)
    )
    with pytest.raises(admission.AdmissionError, match="process"):
        finish(failed)
    monkeypatch.setattr(Path, "exists", original)
    failed.state.update(ControlGroup="/user.slice/" + UNIT, TasksCurrent="0")
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if str(path).startswith("/sys/fs/cgroup/") else original(path),
    )
    monkeypatch.setattr(Path, "is_file", lambda path: path.name == "cgroup.events")
    monkeypatch.setattr(Path, "read_text", lambda path: "populated 1\n")
    with pytest.raises(admission.AdmissionError, match="cgroup"):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()


def test_existing_cgroup_without_readable_events_is_not_terminal(failed, monkeypatch):
    original = Path.exists
    failed.state.update(ControlGroup="/user.slice/" + UNIT, TasksCurrent="0")
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if str(path).startswith("/sys/fs/cgroup/") else original(path),
    )
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    with pytest.raises(admission.AdmissionError, match="cgroup"):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()


def test_reused_invocation_during_observation_refuses(failed, monkeypatch):
    calls = iter([dict(failed.state), dict(failed.state, InvocationID="d" * 32)])
    monkeypatch.setattr(admission, "unit_state", lambda *args, **kwargs: next(calls))
    with pytest.raises(admission.AdmissionError, match="during observation"):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()


def test_crash_before_receipt_does_not_drop_reservation(failed, monkeypatch):
    original = admission.write_once
    monkeypatch.setattr(
        admission, "write_once", lambda *args: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(OSError):
        finish(failed)
    assert rows(failed)[0]["reserved_memory_max"] == LIMITS["memory_max_bytes"]
    monkeypatch.setattr(admission, "write_once", original)
    finish(failed)
    assert rows(failed) == []


def test_corrupted_terminal_proof_never_releases(failed):
    intent = admission.read_json(failed.directory / "intent.json")
    admission.write_once(
        failed.directory / "failed-terminal.json",
        {
            "schema": "skfleet.failed-admission-terminal/v1",
            "intent_sha256": admission._digest(intent),
            "reservation_id": failed.identity,
            "state": failed.state,
            "card_revision": REVISION,
            "process_absent": False,
            "cgroup_empty": True,
        },
    )
    with pytest.raises(admission.AdmissionError, match="inconsistent"):
        rows(failed)


def test_later_live_service_stays_charged_after_prior_failed_proof(failed, monkeypatch):
    finish(failed)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    assert rows(failed) == [{"unit": UNIT}]


def test_concurrent_finalizers_share_one_immutable_terminal_receipt(failed):
    with ThreadPoolExecutor(max_workers=2) as pool:
        proofs = list(pool.map(lambda _: finish(failed), range(2)))
    assert proofs[0] == proofs[1]
    assert len(list(failed.directory.glob("failed-terminal.json"))) == 1


def test_already_observed_launch_keeps_normal_lifecycle(failed):
    admission.write_once(
        failed.directory / "observed.json",
        {
            "reservation_id": failed.identity,
            "unit": UNIT,
            "invocation": INVOCATION,
            "memory_max_bytes": LIMITS["memory_max_bytes"],
        },
    )
    with pytest.raises(admission.AdmissionError, match="already observed"):
        finish(failed)
    assert not (failed.directory / "failed-terminal.json").exists()
