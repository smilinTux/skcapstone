"""Synthetic qualification for exact child progress lease handling."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import skcapstone.fleet.child_progress as child_progress
from skcapstone.fleet.child_progress import (
    ChildProgressSnapshot,
    active_receipt,
    cancel_exact_child,
    exact_child_matches,
    lease_receipts,
    load_snapshot,
    process_cgroup,
    process_start_ticks,
    receipt_payload,
    snapshot_key,
    write_receipt,
    write_snapshot,
)
from skcapstone.fleet.worker_watchdog import ChildLeaseConfig

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts/fleet/skfleet-rotate.py"
WRAPPER = ROOT / "scripts/fleet/skfleet-worker-wrapper.py"


def _snapshot(**changes: object) -> ChildProgressSnapshot:
    values: dict[str, object] = {
        "card_id": "feedbeef",
        "owner": "pi-codex-chiap01-feedbeef",
        "claim_revision": "revision-1",
        "host": "chiap01",
        "lane": "codex",
        "model_bucket": "sk-codex",
        "session_id": "codex-auto-feedbeef",
        "unit": "skfleet-worker-codex-feedbeef.service",
        "control_group": "/user.slice/skfleet-worker-codex-feedbeef.service",
        "wrapper_pid": 101,
        "child_pid": 102,
        "child_start_ticks": 103,
        "started_at": 10.0,
        "started_at_utc": "2026-09-08T08:00:00+00:00",
        "observed_at": 20.0,
        "startup_complete_at": None,
        "provider_started_at": None,
        "first_output_at": None,
        "last_progress_at": None,
        "stdout_bytes": 0,
        "child_alive": True,
        "human_gate": False,
        "side_effects": False,
        "config": ChildLeaseConfig(5, 10, 15, 20),
    }
    values.update(changes)
    return ChildProgressSnapshot(**values)  # type: ignore[arg-type]


def test_config_round_trip_and_invalid_values_fail_closed() -> None:
    config = ChildLeaseConfig(1, 2, 3, 4)
    assert ChildLeaseConfig.from_mapping(config.as_dict()) == config
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ChildLeaseConfig(value, 2, 3, 4)
    with pytest.raises(ValueError):
        ChildLeaseConfig.from_mapping({"startup_s": 1})


def test_active_wrapper_with_sleeping_zero_output_child_stalls() -> None:
    snapshot = _snapshot(
        card_id="516a2b5f",
        owner="pi-codex-chiap01-516a2b5f",
        session_id="codex-auto-516a2b5f",
        unit="skfleet-worker-codex-516a2b5f.service",
        control_group="/user.slice/skfleet-worker-codex-516a2b5f.service",
        observed_at=100.0,
    )
    receipts = lease_receipts(snapshot, now=100.0)
    receipt = active_receipt(snapshot, receipts)
    assert receipt.phase == "startup"
    assert receipt.state == "child-stalled"
    assert receipt.card == "516a2b5f"
    assert receipt.child_pid == 102
    assert receipt.child_start_ticks == 103


def test_all_reached_phases_are_evaluated_and_productive_child_is_kept() -> None:
    snapshot = _snapshot(
        observed_at=24.0,
        startup_complete_at=11.0,
        provider_started_at=12.0,
        first_output_at=13.0,
        last_progress_at=19.0,
        stdout_bytes=42,
    )
    receipts = lease_receipts(snapshot, now=24.0)
    assert [receipt.phase for receipt in receipts] == [
        "startup",
        "first-output",
        "provider-response",
        "progress",
    ]
    assert all(receipt.state == "healthy" for receipt in receipts)
    assert active_receipt(snapshot, receipts).phase == "progress"


def test_provider_response_expiry_is_the_active_failure() -> None:
    snapshot = _snapshot(
        observed_at=40.0,
        startup_complete_at=11.0,
        provider_started_at=12.0,
    )
    receipt = active_receipt(snapshot, lease_receipts(snapshot, now=40.0))
    assert (receipt.phase, receipt.state) == ("provider-response", "child-stalled")


@pytest.mark.parametrize("flag", ["human_gate", "side_effects"])
def test_snapshot_protections_are_non_replayable(flag: str) -> None:
    snapshot = _snapshot(**{flag: True})
    assert active_receipt(snapshot, lease_receipts(snapshot, now=100)).state == "not-replayable"


@pytest.mark.parametrize("flag", ["terminal", "superseded", "ambiguous_progress"])
def test_fresh_card_protections_are_non_replayable(flag: str) -> None:
    snapshot = _snapshot()
    kwargs = {flag: True}
    assert active_receipt(snapshot, lease_receipts(snapshot, now=100, **kwargs)).state == (
        "not-replayable"
    )


def test_snapshot_and_receipt_are_durable_and_hash_bound(tmp_path: Path) -> None:
    snapshot = _snapshot()
    path = tmp_path / "snapshot.json"
    digest = write_snapshot(path, snapshot)
    assert load_snapshot(path) == snapshot
    assert digest == __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    receipt = active_receipt(snapshot, lease_receipts(snapshot, now=100))
    payload = receipt_payload(
        snapshot,
        receipt,
        stage="pre-action",
        mode="observe",
        outcome="synthetic",
        retry_disposition="retryable",
    )
    first, first_hash = write_receipt(tmp_path / "receipts", payload)
    second, second_hash = write_receipt(tmp_path / "receipts", payload)
    assert (first, first_hash) == (second, second_hash)
    repeated = json.loads(json.dumps(payload))
    repeated["lease"]["elapsed_s"] += 500
    third, third_hash = write_receipt(tmp_path / "receipts", repeated)
    assert (third, third_hash) == (first, first_hash)
    changed = replace(snapshot, config=ChildLeaseConfig(2, 3, 4, 5))
    changed_payload = receipt_payload(
        changed,
        receipt,
        stage="pre-action",
        mode="observe",
        outcome="synthetic",
        retry_disposition="retryable",
    )
    assert changed_payload["receipt_id"] != payload["receipt_id"]
    assert first.stat().st_mode & 0o777 == 0o600
    assert json.loads(first.read_text())["snapshot_identity"]["claim_revision"] == "revision-1"


def test_concurrent_receipt_replays_converge_on_one_immutable_file(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = active_receipt(snapshot, lease_receipts(snapshot, now=100))
    payload = receipt_payload(
        snapshot,
        receipt,
        stage="pre-action",
        mode="observe",
        outcome="synthetic-concurrency",
        retry_disposition="retryable",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _index: write_receipt(tmp_path / "receipts", payload), range(32))
        )
    assert len(set(results)) == 1
    assert len(list((tmp_path / "receipts").glob("*.json"))) == 1
    assert not list((tmp_path / "receipts").glob("*.tmp"))


def test_real_wrapper_persists_progress_without_reading_output_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("child_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    config = ChildLeaseConfig(1, 2, 3, 4)
    args = SimpleNamespace(
        card="feedbeef",
        owner="pi-codex-chiap01-feedbeef",
        claim_revision="revision-1",
        host="chiap01",
        lane="codex",
        model="sk-codex",
        stdout=tmp_path / "worker.log",
        evidence_dir=tmp_path / "evidence/worker-exits",
        session="codex-auto-feedbeef",
        worker_executable=sys.executable,
        unit="skfleet-worker-codex-feedbeef.service",
        startup_timeout=1.0,
        observation_interval=0.01,
        human_gate=False,
        side_effects=False,
        child_lease_config=config,
        command=[
            sys.executable,
            "-c",
            "import time; print('progress', flush=True); time.sleep(0.2)",
        ],
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)
    assert module.main() == 0
    progress = tmp_path / "evidence/worker-child-progress"
    (path,) = progress.glob("*.json")
    snapshot = child_progress.load_snapshot(path)
    assert snapshot.identity()[:3] == (
        "feedbeef",
        "pi-codex-chiap01-feedbeef",
        "revision-1",
    )
    assert snapshot.config == config
    assert snapshot.stdout_bytes == len(b"progress\n")
    assert snapshot.first_output_at is not None
    assert snapshot.provider_started_at is not None


def test_exact_pidfd_canary_cancels_only_matching_synthetic_child() -> None:
    identity = {
        "SKAGENT": "pi-codex-chiap01-feedbeef",
        "SKFLEET_CARD_ID": "feedbeef",
        "SKFLEET_CLAIM_REVISION": "revision-1",
        "SKFLEET_SESSION_ID": "codex-auto-feedbeef",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, **identity},
    )
    try:
        snapshot = _snapshot(
            wrapper_pid=os.getpid(),
            child_pid=child.pid,
            child_start_ticks=process_start_ticks(child.pid),
            control_group=process_cgroup(child.pid),
        )
        assert exact_child_matches(snapshot) is True
        assert exact_child_matches(replace(snapshot, claim_revision="wrong")) is False
        assert cancel_exact_child(snapshot, terminate_timeout_s=3, kill_timeout_s=1) in {
            "terminated",
            "killed",
        }
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def _reconcile_function() -> ast.FunctionDef:
    return next(
        node
        for node in ast.parse(ROTATE.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "_reconcile_child_progress"
    )


def _run_reconcile(
    tmp_path: Path,
    *,
    mode: str,
    protected: bool = False,
    claim: bool = True,
    after_cancel: tuple[object, object, object] | None = None,
):
    (tmp_path / "one.json").write_text("{}\n", encoding="utf-8")
    snapshot = _snapshot()
    receipt = SimpleNamespace(state="child-stalled", reason="lease-expired")
    calls: list[str] = []
    exact_claim = (
        (snapshot.owner, 1.0, snapshot.claim_revision) if claim else ("other", 2.0, "new")
    )
    claims = [exact_claim, exact_claim, after_cancel or exact_claim]

    def current_claim(_cid: str):
        return claims.pop(0) if len(claims) > 1 else claims[0]

    namespace = {
        "Path": Path,
        "CHILD_PROGRESS": str(tmp_path),
        "HOST": snapshot.host,
        "_CHILD_LEASE_MODE": mode,
        "load_snapshot": lambda _path: snapshot,
        "_child_card_policy": lambda _cid: {
            "terminal": False,
            "superseded": False,
            "ambiguous": False,
            "human_gate": protected,
            "side_effects": False,
        },
        "lease_receipts": lambda *args, **kwargs: (receipt,),
        "active_receipt": lambda *args: receipt,
        "_persist_child_receipt": lambda _s, _r, stage, outcome, disposition: calls.append(
            f"receipt:{stage}:{outcome}:{disposition}"
        ),
        "_current_claim_identity_fresh": current_claim,
        "_child_unit_bound": lambda _snapshot: True,
        "exact_child_matches": lambda _snapshot: True,
        "cancel_exact_child": lambda _snapshot: calls.append("cancel") or "terminated",
        "_release_child_claim": lambda _snapshot: calls.append("release") or True,
        "_child_projection_terminal": lambda _owner, _cid: True,
        "time": time,
        "subprocess": subprocess,
        "OSError": OSError,
        "RuntimeError": RuntimeError,
        "TimeoutError": TimeoutError,
        "TypeError": TypeError,
        "ValueError": ValueError,
        "d": "unused",
        "log": lambda *args: None,
    }
    exec(
        compile(ast.Module([_reconcile_function()], type_ignores=[]), str(ROTATE), "exec"),
        namespace,
    )
    return namespace["_reconcile_child_progress"]([{"unit": snapshot.unit}]), calls


def test_live_controller_orders_pre_receipt_cancel_release_post_receipt(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(tmp_path, mode="act")
    assert acted == 1
    assert calls == [
        "receipt:assessment:lease-expired:unchanged",
        "receipt:pre-action:exact-child-cancel-planned:retryable",
        "cancel",
        "release",
        "receipt:post-action:terminated-claim-released:retryable",
    ]


def test_live_controller_observe_mode_never_cancels(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(tmp_path, mode="observe")
    assert acted == 0
    assert "cancel" not in calls and "release" not in calls
    assert calls[-1] == "receipt:post-action:observe-only:unchanged"


def test_live_controller_claim_race_fails_before_cancel(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(tmp_path, mode="act", claim=False)
    assert acted == 0
    assert "cancel" not in calls and "release" not in calls
    assert calls == []


def test_wrapper_release_race_is_a_successful_retryable_terminal(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(tmp_path, mode="act", after_cancel=(None, None, None))
    assert acted == 1
    assert "cancel" in calls
    assert "release" not in calls
    assert calls[-1] == "receipt:post-action:terminated-claim-released:retryable"


def test_new_claim_generation_after_cancel_is_never_released(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(
        tmp_path, mode="act", after_cancel=("other", 2.0, "new-revision")
    )
    assert acted == 0
    assert "cancel" in calls
    assert "release" not in calls
    assert calls[-1] == "receipt:post-action:claim-changed-after-cancel:forbidden"


def test_live_controller_protected_work_is_never_cancelled(tmp_path: Path) -> None:
    acted, calls = _run_reconcile(tmp_path, mode="act", protected=True)
    assert acted == 0
    assert "cancel" not in calls and "release" not in calls
    assert calls == ["receipt:assessment:lease-expired:forbidden"]


def test_forbidden_receipt_holds_scheduler_until_authored_change(tmp_path: Path) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    payload = {
        "recorded_at": "2026-09-08T08:00:00+00:00",
        "retry_disposition": "forbidden",
        "snapshot_identity": {"card_id": "feedbeef"},
    }
    (receipt_dir / "one.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    source = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_child_replay_dispositions", "_child_replay_held"}
    nodes = [
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    nodes.insert(
        0,
        ast.Assign(
            targets=[ast.Name(id="_child_replay_cache", ctx=ast.Store())],
            value=ast.Constant(value=None),
        ),
    )
    changed = {"at": 0.0}
    namespace = {
        "CHILD_RECEIPTS": str(receipt_dir),
        "glob": __import__("glob"),
        "json": json,
        "open": open,
        "os": os,
        "re": __import__("re"),
        "_ts_epoch": lambda _value: 100.0,
        "_authored_change_epoch": lambda _cid, threshold: (
            changed["at"] if changed["at"] > threshold else 0
        ),
    }
    module = ast.fix_missing_locations(ast.Module(nodes, type_ignores=[]))
    exec(compile(module, str(ROTATE), "exec"), namespace)
    assert namespace["_child_replay_held"]("feedbeef") is True
    changed["at"] = 101.0
    assert namespace["_child_replay_held"]("feedbeef") is False


def test_blocked_backoff_consumes_child_no_replay_decision() -> None:
    function = next(
        node
        for node in ast.parse(ROTATE.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "blocked_backoff"
    )
    namespace = {"_child_replay_held": lambda _cid: True}
    exec(
        compile(ast.Module([function], type_ignores=[]), str(ROTATE), "exec"),
        namespace,
    )
    assert namespace["blocked_backoff"]("feedbeef") is True


def test_production_entrypoints_wire_observation_action_and_retry_fence() -> None:
    rotate = ROTATE.read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts/fleet/skfleet-worker-wrapper.py").read_text(encoding="utf-8")
    assert "_reconcile_child_progress(active_worker_units())" in rotate
    assert "SLOTS_FULL|%s|watchdog and reconciliation still run" in rotate
    assert "NOOP|%s|all slots busy" not in rotate
    assert 'SKFLEET_CHILD_LEASE_MODE", "observe"' in rotate
    assert 'globals().get("_child_replay_held"' in rotate
    assert "monitor_child_progress" in wrapper
    assert '"--claim-revision",claimed_revision' in rotate
    assert '"--unit",unit' in rotate


def test_snapshot_key_binds_card_owner_and_claim_revision() -> None:
    baseline = snapshot_key("feedbeef", "owner", "revision-1")
    assert baseline != snapshot_key("feedbeef", "owner", "revision-2")
    assert baseline != snapshot_key("feedbeef", "other", "revision-1")
