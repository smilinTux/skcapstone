"""Shared admission uses durable reservations across independent launchers."""

import multiprocessing

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_admission as admission

HOST = "fixture"
UNIT = "skfleet-worker-fixture.service"
LIMITS = dict(
    cpu_quota_percent=200, memory_max_bytes=1024, tasks_max=256, runtime_max_seconds=3600
)
POLICY = {"node_quotas": {HOST: LIMITS}}
BINDING = dict(card_id="12345678", owner="fixture", claim_revision="claim")


def command(unit=UNIT):
    """Keep all existing per-unit quotas in the synthetic command."""
    return [
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


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    """One pending allowance fits under a real native claim."""
    (tmp_path / "fleet").mkdir()
    CardStore(tmp_path).create(
        CardCore(
            id=BINDING["card_id"],
            title="Synthetic resource test",
            initial_owner=BINDING["owner"],
            initial_claim_revision=BINDING["claim_revision"],
        )
    )
    monkeypatch.setattr(admission.socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(
        admission,
        "local_worker_admission",
        lambda policy, host, rows: (len(rows) == 0, "fixture capacity"),
    )
    monkeypatch.setattr(
        admission,
        "unit_state",
        lambda unit: {"Id": unit, "ActiveState": "inactive", "LoadState": "not-found"},
    )
    return tmp_path


def reserve(home, binding=BINDING, unit=UNIT):
    """Use the operator-facing API also used by native launchers."""
    return admission.reserve_launch(home, POLICY, HOST, unit, binding, command(unit))


def test_crash_before_spawn_keeps_capacity_and_denies_replay(capacity):
    argv = reserve(capacity)
    assert any(arg.startswith("--setenv=SKFLEET_ADMISSION_ID=") for arg in argv)
    with pytest.raises(admission.AdmissionError, match="already reserved"):
        reserve(capacity)
    with pytest.raises(admission.AdmissionError, match="capacity"):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")


def test_released_claim_retires_only_an_unstarted_reservation(capacity, monkeypatch):
    monkeypatch.setitem(POLICY, "node_admission", {HOST: {"max_concurrent_workers": 1}})
    original = reserve(capacity)
    identity = original[1].split("=", 2)[2]
    directory = capacity / "fleet/resource-admission" / HOST / identity
    CardStore(capacity).append_event(
        BINDING["card_id"],
        "release_claim",
        "operator",
        released_owner=BINDING["owner"],
        expected_claim_revision=BINDING["claim_revision"],
        transition_id="released",
    )
    monkeypatch.setattr(
        admission,
        "unit_state",
        lambda unit, terminal=False: {
            "Id": unit,
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
            "ControlPID": "0",
            "ControlGroup": "",
            "InvocationID": "",
        },
    )
    next_binding = dict(card_id="87654321", owner="next", claim_revision="next-claim")
    CardStore(capacity).create(
        CardCore(
            id=next_binding["card_id"],
            title="Next synthetic work",
            initial_owner=next_binding["owner"],
            initial_claim_revision=next_binding["claim_revision"],
        )
    )
    reserve(capacity, next_binding, "skfleet-builder-next.service")
    assert (directory / "released-prestart.json").is_file()
    with pytest.raises(admission.AdmissionError, match="launch native claim changed"):
        reserve(capacity)
    with pytest.raises(admission.AdmissionError, match="launch native claim changed"):
        admission.start_reserved(capacity, HOST, original, lambda argv: None)


def test_released_claim_does_not_retire_a_consumed_start(capacity, monkeypatch):
    monkeypatch.setitem(POLICY, "node_admission", {HOST: {"max_concurrent_workers": 1}})
    original = reserve(capacity)
    admission.start_reserved(capacity, HOST, original, lambda argv: None)
    CardStore(capacity).append_event(
        BINDING["card_id"],
        "release_claim",
        "operator",
        released_owner=BINDING["owner"],
        expected_claim_revision=BINDING["claim_revision"],
        transition_id="released",
    )
    monkeypatch.setattr(
        admission,
        "unit_state",
        lambda unit, terminal=False: {
            "Id": unit,
            "LoadState": "not-found",
            "ActiveState": "inactive",
        },
    )
    next_binding = dict(card_id="87654321", owner="next", claim_revision="next-claim")
    CardStore(capacity).create(
        CardCore(
            id=next_binding["card_id"],
            title="Next synthetic work",
            initial_owner=next_binding["owner"],
            initial_claim_revision=next_binding["claim_revision"],
        )
    )
    with pytest.raises(admission.AdmissionError, match="worker occupancy cap"):
        reserve(capacity, next_binding, "skfleet-builder-next.service")


def _contend(home, barrier, queue, number):
    """Race distinct launch paths against one shared node allowance."""
    barrier.wait(timeout=5)
    try:
        reserve(home, dict(BINDING, attempt=str(number)), f"skfleet-builder-{number}.service")
        queue.put("reserved")
    except admission.AdmissionError:
        queue.put("denied")


def test_concurrent_paths_cannot_over_admit(capacity):
    context = multiprocessing.get_context("fork")
    barrier, queue = context.Barrier(4), context.Queue()
    children = [
        context.Process(target=_contend, args=(capacity, barrier, queue, i)) for i in range(4)
    ]
    try:
        for child in children:
            child.start()
        answers = [queue.get(timeout=5) for _ in children]
        for child in children:
            child.join(5)
            assert child.exitcode == 0
        assert answers.count("reserved") == 1
    finally:
        # A failed child must fail the test without trapping pytest at shutdown.
        for child in children:
            if child.is_alive():
                child.kill()
        for child in children:
            if child.pid is not None:
                child.join(5)
        queue.close()


def test_generation_lifetime_lock_does_not_block_admission(capacity):
    import fcntl

    lock = capacity / "generation.lock"
    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        reserve(capacity)
        with lock.open("w") as competitor:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_unknown_capacity_and_changed_quota_deny_without_intent(capacity, monkeypatch):
    monkeypatch.setattr(
        admission,
        "local_worker_admission",
        lambda *args: (False, "node-resource-evidence-unavailable"),
    )
    with pytest.raises(admission.AdmissionError, match="unavailable"):
        reserve(capacity)
    assert not list((capacity / "fleet/resource-admission").glob("*/*/intent.json"))
    argv = command()
    argv.remove("--property=TasksMax=256")
    with pytest.raises(admission.AdmissionError, match="quota"):
        admission.reserve_launch(capacity, POLICY, HOST, UNIT, BINDING, argv)


def live_state(unit, identity):
    """Model exact systemd attribution, without collecting real environment data."""
    return dict(
        Id=unit,
        LoadState="loaded",
        ActiveState="active",
        MemoryMax="1024",
        MemoryCurrent="300",
        InvocationID="a" * 32,
        SKFLEET_ADMISSION_ID=identity,
    )


def test_observed_service_transfers_charge_without_double_counting(capacity, monkeypatch):
    argv = reserve(capacity)
    identity = argv[1].split("=", 2)[2]
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    monkeypatch.setattr(admission, "unit_state", lambda unit: live_state(unit, identity))
    snapshots = []
    monkeypatch.setattr(
        admission,
        "local_worker_admission",
        lambda policy, host, rows: (snapshots.append(rows) or False, "capacity"),
    )
    with pytest.raises(admission.AdmissionError, match="capacity"):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")
    assert snapshots == [[{"unit": UNIT}]]
    directory = capacity / "fleet/resource-admission" / HOST / identity
    assert admission.read_json(directory / "observed.json")["invocation"] == "a" * 32
    # A crash after durable acknowledgment leaves the existing live cgroup charged.
    with pytest.raises(admission.AdmissionError, match="capacity"):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")
    assert snapshots[-1] == [{"unit": UNIT}]
    # Normal collected exit is no longer pending, but the same generation cannot replay.
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "capacity"))
    with pytest.raises(admission.AdmissionError, match="already reserved"):
        reserve(capacity)
    reserve(capacity, dict(BINDING, attempt="new-generation"))


@pytest.mark.parametrize(
    "change",
    [
        {"SKFLEET_ADMISSION_ID": "wrong"},
        {"MemoryMax": "512"},
        {"MemoryCurrent": "[not set]"},
        {"InvocationID": ""},
        {"ActiveState": "deactivating"},
        {"Id": "another-unit"},
    ],
)
def test_unknown_or_reused_service_never_releases_pending_charge(capacity, monkeypatch, change):
    identity = reserve(capacity)[1].split("=", 2)[2]
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    monkeypatch.setattr(admission, "unit_state", lambda unit: live_state(unit, identity) | change)
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")
    assert not list((capacity / "fleet/resource-admission").glob("*/*/observed.json"))


def test_crash_before_ack_is_reconciled_by_exact_live_observation(capacity, monkeypatch):
    identity = reserve(capacity)[1].split("=", 2)[2]
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": UNIT}])
    monkeypatch.setattr(admission, "unit_state", lambda unit: live_state(unit, identity))
    original = admission.write_once
    monkeypatch.setattr(
        admission, "write_once", lambda *args: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")
    monkeypatch.setattr(admission, "write_once", original)
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "capacity"))
    reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")
    assert (capacity / "fleet/resource-admission" / HOST / identity / "observed.json").exists()


def test_changed_command_cannot_replay_same_generation(capacity):
    reserve(capacity)
    with pytest.raises(admission.AdmissionError, match="already reserved"):
        admission.reserve_launch(capacity, POLICY, HOST, UNIT, BINDING, command() + ["changed"])


def test_interrupted_intent_publication_denies_new_launches(capacity, monkeypatch):
    monkeypatch.setattr(
        admission, "write_once", lambda *args: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(admission.AdmissionError):
        reserve(capacity)
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-builder-other.service")


@pytest.mark.parametrize(
    "option",
    [
        "--unit=other.service",
        "-uother.service",
        "--property=MemoryMax=2048",
        "-pMemoryMax=2048",
        "--property=Restart=always",
        "--host=other",
        "--scope",
    ],
)
def test_command_overrides_cannot_bypass_admission(capacity, option):
    argv = command()
    argv.insert(1, option)
    with pytest.raises(admission.AdmissionError):
        admission.reserve_launch(capacity, POLICY, HOST, UNIT, BINDING, argv)


def test_private_lock_and_intents_are_owned_regular_files(capacity):
    import os
    import stat

    reserve(capacity)
    for path in (capacity / "fleet/resource-admission").rglob("*"):
        info = path.lstat()
        assert info.st_uid == os.getuid()
        assert not stat.S_ISLNK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)


def test_service_environment_is_reduced_to_marker(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        admission.subprocess,
        "run",
        lambda *args, **kw: SimpleNamespace(
            stdout='Id=fixture\nEnvironment="SECRET=synthetic value" SKFLEET_ADMISSION_ID=abc\n'
        ),
    )
    state = admission.unit_state("fixture")
    assert state == {"Id": "fixture", "SKFLEET_ADMISSION_ID": "abc"}


def test_measured_capacity_deferral_proves_no_intent_and_is_retryable(capacity, monkeypatch):
    monkeypatch.setattr(
        admission,
        "local_worker_admission",
        lambda *args: (False, "memory_available=0 required=1024"),
    )
    with pytest.raises(admission.AdmissionDeferredError):
        reserve(capacity)
    assert not list((capacity / "fleet/resource-admission").glob("*/*/intent.json"))
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "available"))
    reserve(capacity)
    with pytest.raises(admission.AdmissionError) as raised:
        reserve(capacity)
    assert not isinstance(raised.value, admission.AdmissionDeferredError)


def _started(home, age_seconds):
    """Mark the single reservation started age_seconds ago, as start_reserved does."""
    import os
    import time

    (directory,) = [d for d in (home / "fleet/resource-admission" / HOST).iterdir() if d.is_dir()]
    start = directory / "start.json"
    start.write_text("{}")
    stamp = time.time() - age_seconds
    os.utime(start, (stamp, stamp))


def test_absent_unit_past_its_runtime_limit_holds_no_capacity(capacity):
    """2026-10-10: dead, never-observed review units pinned every builder at 0 MiB."""
    reserve(capacity)
    _started(capacity, LIMITS["runtime_max_seconds"] + admission.RUNTIME_EXPIRY_GRACE_SECONDS + 60)
    other = dict(card_id="87654321", owner="next", claim_revision="next-claim")
    CardStore(capacity).create(
        CardCore(
            id=other["card_id"],
            title="Next synthetic work",
            initial_owner=other["owner"],
            initial_claim_revision=other["claim_revision"],
        )
    )
    reserve(capacity, other, "skfleet-worker-next.service")


def test_recent_unobserved_start_stays_charged(capacity):
    reserve(capacity)
    _started(capacity, 60)
    with pytest.raises(admission.AdmissionError, match="capacity"):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-worker-other.service")


def test_pre_claim_check_sees_ledger_reservations(capacity):
    """The review pre-check must agree with reserve_launch, or it claims then releases."""
    assert admission.admission_ready(capacity, POLICY, HOST)[0]
    reserve(capacity)
    assert admission.active_resource_units(capacity) == []
    assert not admission.admission_ready(capacity, POLICY, HOST)[0]


def test_ended_unit_past_startup_grace_holds_no_capacity(capacity):
    """A short review that finished between occupancy passes frees memory at once."""
    reserve(capacity)
    _started(capacity, admission.STARTUP_GRACE_SECONDS + 60)
    other = dict(card_id="87654321", owner="next", claim_revision="next-claim")
    CardStore(capacity).create(
        CardCore(
            id=other["card_id"],
            title="Next synthetic work",
            initial_owner=other["owner"],
            initial_claim_revision=other["claim_revision"],
        )
    )
    reserve(capacity, other, "skfleet-worker-next.service")


def test_fresh_start_within_grace_stays_charged(capacity):
    reserve(capacity)
    _started(capacity, 30)
    with pytest.raises(admission.AdmissionError, match="capacity"):
        reserve(capacity, dict(BINDING, attempt="other"), "skfleet-worker-other.service")
