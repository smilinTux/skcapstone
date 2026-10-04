"""D1 policy and host-native resource boundaries, without live services."""

import copy
import json
import multiprocessing

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_admission as admission
from skcapstone.fleet.production_policy import load_production_policy
from skcapstone.fleet.production_resources import available_worker_memory

GIB = 1024**3
LIMITS = dict(
    cpu_quota_percent=200, memory_max_bytes=3 * GIB, tasks_max=256, runtime_max_seconds=3600
)


@pytest.fixture
def remote_policy():
    hosts = ["chiap01", "chiap02", "chiap03", "chiap04", "chiap08"]
    return dict(
        schema="skfleet.production/v1",
        authority_host="chiap08",
        capacity_authority="skgateway",
        gateway_url="http://gateway.test",
        lanes={
            **{
                key: dict(enabled=True, provider="skgateway")
                for key in ("codex", "glm", "deepseek", "qwen")
            },
            "kimi": dict(enabled=False),
        },
        node_quotas={host: dict(LIMITS) for host in hosts},
        worker_destinations=hosts,
        remote_review=dict(enabled=True, destinations=["chiap03"], card_ids=["b4594faa"]),
        node_admission={"chiap08": dict(max_concurrent_workers=1, memory_floor_bytes=8 * GIB)},
    )


def load(tmp_path, policy):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return load_production_policy(path, host="chiap08")


def test_explicit_rollout_policy_and_legacy_compatibility(tmp_path, remote_policy):
    assert load(tmp_path, remote_policy) == remote_policy
    legacy = {
        k: v
        for k, v in remote_policy.items()
        if k not in ("worker_destinations", "remote_review", "node_admission")
    }
    assert load(tmp_path, legacy) == legacy


@pytest.mark.parametrize(
    "change",
    [
        "quota",
        "duplicate",
        "unknown",
        "restriction",
        "cap",
        "floor",
        "enabled",
        "allowlist",
        "admission",
    ],
)
def test_partial_or_ambiguous_rollout_fails_closed(tmp_path, remote_policy, change):
    if change == "quota":
        del remote_policy["node_quotas"]["chiap03"]
    elif change == "duplicate":
        remote_policy["worker_destinations"].append("chiap03")
    elif change == "unknown":
        remote_policy["remote_review"]["destinations"] = ["chiap09"]
    elif change == "restriction":
        del remote_policy["remote_review"]["card_ids"]
    elif change == "cap":
        remote_policy["node_admission"]["chiap08"]["max_concurrent_workers"] = True
    elif change == "floor":
        remote_policy["node_admission"]["chiap08"]["memory_floor_bytes"] = -1
    elif change == "enabled":
        remote_policy["remote_review"]["enabled"] = "false"
    else:
        del remote_policy["worker_destinations" if change == "allowlist" else "node_admission"]
    with pytest.raises(ValueError):
        load(tmp_path, remote_policy)


def command(unit):
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--unit=" + unit,
        "--property=CPUQuota=200%",
        "--property=MemoryMax=" + str(3 * GIB),
        "--property=TasksMax=256",
        "--property=RuntimeMaxSec=3600",
        "--",
        "/bin/true",
    ]


def reserve(home, policy, host, token, kind="review"):
    # The deployed reserve/start fence locks a real, exactly claimed core.
    cards = CardStore(home)
    owner = "pi-seraph-" + host + "-" + token
    if cards.fold(token) is None:
        cards.create(
            CardCore(
                id=token,
                title="Synthetic remote capacity claim",
                initial_owner=owner,
                initial_claim_revision=token * 4,
            )
        )
    prefix = "skfleet-worker-deepseek-" if kind == "review" else "skfleet-builder-" + kind + "-"
    unit = prefix + token + ".service"
    return admission.reserve_launch(
        home,
        policy,
        host,
        unit,
        dict(card_id=token, owner=owner, claim_revision=token * 4),
        command(unit),
    )


@pytest.fixture
def capacity(tmp_path, monkeypatch):
    (tmp_path / "fleet").mkdir()
    monkeypatch.setattr(admission.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *a: (True, "ample RAM"))
    monkeypatch.setattr(admission, "unit_state", lambda *a, **kw: dict(LoadState="not-found"))
    return tmp_path


def _race(home, policy, token, start, results, kind):
    start.wait()
    try:
        reserve(home, policy, "chiap08", token, kind)
        results.put("admitted")
    except admission.AdmissionError:
        results.put("held")


@pytest.mark.parametrize("kind", ["review", "source", "test"])
def test_real_lock_cap_race_and_pending_custody(capacity, remote_policy, kind):
    ctx = multiprocessing.get_context("fork")
    start, results = ctx.Event(), ctx.Queue()
    workers = [
        ctx.Process(
            target=_race,
            args=(
                capacity,
                remote_policy,
                "%08x" % n,
                start,
                results,
                kind if n % 2 else "review",
            ),
        )
        for n in range(4)
    ]
    for process in workers:
        process.start()
    start.set()
    for process in workers:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in workers) == ["admitted", "held", "held", "held"]
    assert len(list((capacity / "fleet/resource-admission/chiap08").glob("*/intent.json"))) == 1


@pytest.mark.parametrize("host", ["chiap09", "chiap10", "chiwk", "ziowk"])
def test_forged_quota_does_not_grant_placement(capacity, remote_policy, monkeypatch, host):
    remote_policy["node_quotas"][host] = dict(LIMITS)
    monkeypatch.setattr(admission.socket, "gethostname", lambda: host)
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, remote_policy, host, "b4594faa")
    assert not list((capacity / "fleet").glob("resource-admission/*/*/intent.json"))


def test_floor_preserves_every_unused_allowance():
    rows = [dict(MemoryMax=GIB, MemoryCurrent=GIB // 2) for _ in range(5)]
    # All five protected service reservations remain in the memory calculation.
    available = 8 * GIB + 3 * GIB + 5 * (GIB // 2)
    info = "MemTotal: 33554432 kB\nMemAvailable: %d kB\n" % (available // 1024)
    assert available_worker_memory(info, rows, memory_floor_bytes=8 * GIB) == 3 * GIB
    rows[0]["MemoryCurrent"] -= 1
    assert available_worker_memory(info, rows, memory_floor_bytes=8 * GIB) == 3 * GIB - 1


def test_ack_loss_or_absent_unit_never_frees_capped_slot(capacity, remote_policy):
    reserve(capacity, remote_policy, "chiap08", "12345678")
    path = next((capacity / "fleet/resource-admission/chiap08").glob("*/intent.json"))
    intent = json.loads(path.read_text())
    observed = dict(
        reservation_id=path.parent.name,
        unit=intent["unit"],
        memory_max_bytes=3 * GIB,
        invocation="a" * 32,
    )
    admission.write_once(path.parent / "observed.json", observed)
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, remote_policy, "chiap08", "87654321")


def test_protected_identity_mismatch_holds(capacity, remote_policy, monkeypatch):
    unit = "skfleet-worker-c177-trial-api-99ebb4ba.service"
    remote_policy["node_admission"]["chiap08"]["protected_service_bindings"] = {unit: "a" * 64}
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [{"unit": unit}])
    import subprocess

    monkeypatch.setattr(
        admission.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=b"changed unit"),
    )
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, remote_policy, "chiap08", "b4594faa")


def test_proven_terminal_service_frees_slot_without_replaying_intent(
    capacity, remote_policy, monkeypatch
):
    reserve(capacity, remote_policy, "chiap08", "12345678")
    path = next((capacity / "fleet/resource-admission/chiap08").glob("*/intent.json"))
    intent = json.loads(path.read_text())
    state = dict(
        Id=intent["unit"],
        LoadState="loaded",
        ActiveState="inactive",
        SubState="dead",
        InvocationID="a" * 32,
        SKFLEET_ADMISSION_ID=path.parent.name,
        MemoryMax=str(3 * GIB),
        MainPID="0",
        ControlPID="0",
        TasksCurrent="[not set]",
        ControlGroup="",
        ExecMainCode="1",
        ExecMainStatus="0",
        Result="success",
    )
    monkeypatch.setattr(admission, "unit_state", lambda *a, **kw: state)
    reserve(capacity, remote_policy, "chiap08", "87654321")
    with pytest.raises(admission.AdmissionError):
        reserve(capacity, remote_policy, "chiap08", "12345678")
