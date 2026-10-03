"""Retained success releases only an exactly bound, consumed admission."""

# ruff: noqa: F811

from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_admission as admission
from skcapstone.fleet import production_resources as resources
from tests.fleet.test_failed_admission_terminal import (  # noqa: F401
    INVOCATION,
    REVISION,
    failed,
    rows,
)
from tests.fleet.test_production_admission import (  # noqa: F401
    BINDING,
    HOST,
    POLICY,
    UNIT,
    capacity,
    command,
    reserve,
)


@pytest.fixture
def success(failed):
    """Model a fast successful retained systemd invocation."""
    failed.state.update(
        ActiveState="active", SubState="exited", ExecMainStatus="0", Result="success"
    )
    argv = admission.reserved_command(failed.home, POLICY, HOST, UNIT, BINDING, command())
    admission.start_reserved(failed.home, HOST, argv, lambda argv: None)
    return failed


def finish(success, **changes):
    """Supply all original source bytes and exact current custody."""
    return admission.finalize_successful_launch(
        **(
            dict(
                home=success.home,
                policy=POLICY,
                host=HOST,
                unit=UNIT,
                binding=BINDING,
                argv=command(),
                invocation=INVOCATION,
                expected_card_revision=REVISION,
            )
            | changes
        )
    )


def test_fast_success_preserves_intent_claim_and_denies_replay(success):
    proof = finish(success)
    assert proof["state"]["ExecMainStatus"] == "0"
    assert rows(success) == []
    assert finish(success) == proof
    assert (success.directory / "intent.json").exists()
    assert success.card.owner == BINDING["owner"]
    with pytest.raises(admission.AdmissionError, match="already reserved"):
        reserve(success.home)


@pytest.mark.parametrize(
    "change",
    [
        {"ActiveState": "active", "SubState": "running"},
        {"InvocationID": "d" * 32},
        {"SKFLEET_ADMISSION_ID": "e" * 64},
        {"MainPID": "123"},
        {"ControlPID": "123"},
        {"ExecMainCode": "0"},
        {"ExecMainStatus": "1"},
        {"Result": "exit-code"},
        {"TasksCurrent": "1"},
        {"MemoryMax": "999"},
    ],
)
def test_unknown_or_mismatched_success_remains_charged(success, change):
    success.state.update(change)
    with pytest.raises(admission.AdmissionError):
        finish(success)
    assert rows(success)[0]["reserved_memory_max"] == 1024


@pytest.mark.parametrize(
    "change",
    [
        {"argv": command() + ["changed"]},
        {"invocation": "d" * 32},
        {"expected_card_revision": "d" * 64},
        {"binding": dict(BINDING, owner="other")},
    ],
)
def test_source_or_custody_mismatch_denies(success, change):
    with pytest.raises((admission.AdmissionError, FileNotFoundError)):
        finish(success, **change)


def test_success_requires_consumed_start(success):
    (success.directory / "start.json").unlink()
    with pytest.raises((admission.AdmissionError, FileNotFoundError)):
        finish(success)


def test_pending_retained_success_stays_charged_without_memory_parse_crash(success, monkeypatch):
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    assert rows(success) == [{"unit": UNIT, "reserved_memory_max": 1024}]


def test_unknown_live_memory_is_denied(success, monkeypatch):
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    success.state.update(SubState="running", MainPID="123")
    with pytest.raises(admission.AdmissionError):
        rows(success)


def resource_admit(success, monkeypatch, reservation=False, changed=None):
    """Use actual resource calculation against bounded synthetic systemd reads."""
    real_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n"
            if str(path) == "/proc/meminfo"
            else real_read(path, *a, **kw)
        ),
    )
    calls = []

    def runner(*args, **kwargs):
        state = dict(success.state)
        if calls and changed:
            state.update(changed)
        calls.append(args)
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{k}={v}" for k, v in state.items())
        )

    row = {"unit": UNIT}
    if reservation:
        row["reserved_memory_max"] = 900000 * 1024
    return resources.local_worker_admission(POLICY, HOST, [row], runner=runner)


def test_terminal_has_no_quota_double_charge_but_pending_intent_stays_reserved(
    success, monkeypatch
):
    assert resource_admit(success, monkeypatch)[0]
    assert not resource_admit(success, monkeypatch, reservation=True)[0]


@pytest.mark.parametrize(
    "change",
    [
        {"SubState": "running"},
        {"MainPID": "123"},
        {"TasksCurrent": "1"},
        {"ControlGroup": "/wrong/group", "TasksCurrent": "0"},
        {"InvocationID": ""},
    ],
)
def test_uncertain_terminal_or_live_memory_never_becomes_free(success, monkeypatch, change):
    success.state.update(change)
    assert resource_admit(success, monkeypatch) == (False, "node-resource-evidence-unavailable")


def test_terminal_invocation_race_denies_resource_admission(success, monkeypatch):
    assert not resource_admit(success, monkeypatch, changed={"InvocationID": "d" * 32})[0]


def test_success_process_and_populated_cgroup_denied(success, monkeypatch):
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p) == "/proc/999999999" else real_exists(p)
    )
    with pytest.raises(admission.AdmissionError):
        finish(success)
    assert not resource_admit(success, monkeypatch)[0]
    monkeypatch.setattr(Path, "exists", real_exists)
    success.state.update(ControlGroup="/user.slice/" + UNIT, TasksCurrent="0")
    monkeypatch.setattr(
        Path, "exists", lambda p: True if str(p).startswith("/sys/fs/cgroup/") else real_exists(p)
    )
    monkeypatch.setattr(Path, "is_file", lambda p: True)
    real_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **k: "populated 1\n" if p.name == "cgroup.events" else real_read(p, *a, **k),
    )
    with pytest.raises(admission.AdmissionError):
        finish(success)


def test_success_observed_invocation_must_match(success):
    admission.write_once(
        success.directory / "observed.json",
        {
            "reservation_id": success.identity,
            "unit": UNIT,
            "invocation": "d" * 32,
            "memory_max_bytes": 1024,
        },
    )
    with pytest.raises(admission.AdmissionError, match="invocation changed"):
        finish(success)


def test_success_recheck_and_write_failure_keep_intent(success, monkeypatch):
    calls = iter([dict(success.state), dict(success.state, InvocationID="d" * 32)])
    monkeypatch.setattr(admission, "unit_state", lambda *a, **k: next(calls))
    with pytest.raises(admission.AdmissionError, match="during observation"):
        finish(success)
    assert not (success.directory / "success-terminal.json").exists()


@pytest.mark.parametrize("damage", ["process", "intent", "marker", "status", "group"])
def test_forged_success_receipt_never_discharges_intent(success, damage):
    """Historical proof must retain exact source and terminal attribution."""
    intent = admission.read_json(success.directory / "intent.json")
    proof = dict(
        schema="skfleet.success-admission-terminal/v1",
        reservation_id=success.identity,
        intent_sha256=admission._digest(intent),
        card_revision=REVISION,
        process_absent=True,
        cgroup_empty=True,
        state=dict(success.state),
    )
    if damage == "process":
        proof["process_absent"] = False
    elif damage == "intent":
        proof["intent_sha256"] = "d" * 64
    elif damage == "marker":
        proof["state"]["SKFLEET_ADMISSION_ID"] = "d" * 64
    elif damage == "status":
        proof["state"]["ExecMainStatus"] = "1"
    else:
        proof["state"].update(ControlGroup="/another.service", TasksCurrent="0")
    admission.write_once(success.directory / "success-terminal.json", proof)
    with pytest.raises(admission.AdmissionError, match="inconsistent"):
        rows(success)


def test_success_receipt_write_failure_keeps_reservation(success, monkeypatch):
    """A crash before immutable acknowledgment cannot free a pending intent."""

    def fail(*args):
        raise OSError("fixture receipt write failure")

    monkeypatch.setattr(admission, "write_once", fail)
    with pytest.raises(OSError):
        finish(success)
    assert rows(success) == [{"unit": UNIT, "reserved_memory_max": 1024}]
