"""Regression tests for duplicate exact-card worker admission (card d11ca7ed).

Observed reproductions folded into these tests:

* c32a1001: two Pi PIDs simultaneously ran one card under one owner in one
  worktree — the launcher had no per-card admission gate, so a concurrent
  second launcher reached process creation.
* 0d7331e7: a fleet worker unit (pi-codex-chiap04-0d7331e7) was launched for
  a card whose authoritative owner cursor-w105-focus already existed.

The fix is a host-safe atomic admission lock in the fleet launcher: one
receipt per exact card, bound to card ID, claim revision, normalized
worktree, and worker owner, acquired after the claim wins and before the
worker process is created. A concurrent second launcher fails before process
creation and emits a deterministic, non-secret receipt naming the live
holder; stale recovery requires fresh proof that no matching process, unit,
or session identity is alive.

The launcher script is exercised through AST extraction, matching the
convention in ``test_skfleet_scheduler_truth.py``.
"""

import ast
import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"

_FUNCTION_NAMES = (
    "_admission_lock_path",
    "_release_card_admission",
    "_read_admission_receipt",
    "_pid_alive",
    "_unit_active",
    "_session_active",
    "_admission_holder_live",
    "acquire_card_admission",
)

_ASSIGN_NAMES = (
    "_ADMISSION_WRITE_WINDOW_S",
    "_ADMISSION_WRITE_POLL_S",
)

#: The exact non-secret deterministic receipt field set.
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "host",
        "card_id",
        "owner",
        "claim_revision",
        "workspace",
        "unit",
        "session",
        "pid",
        "acquired_at",
    }
)


def _load_namespace() -> dict:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _FUNCTION_NAMES)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in _ASSIGN_NAMES
                for target in node.targets
            )
        )
    ]
    namespace = {
        "os": os,
        "json": json,
        "subprocess": subprocess,
        "datetime": datetime,
        "time": time,
        "HOST": "chiap08",
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _acquire(namespace, home, cid, owner, revision, workspace, **overrides):
    overrides.setdefault("unit", "skfleet-worker-codex-%s.service" % cid)
    overrides.setdefault("session", "codex-auto-%s" % cid)
    return namespace["acquire_card_admission"](
        str(home),
        cid,
        owner=owner,
        claim_revision=revision,
        workspace=workspace,
        **overrides,
    )


def test_second_launcher_refused_while_holder_pid_live(tmp_path) -> None:
    """The c32a1001 shape: a live concurrent holder blocks before launch."""
    namespace = _load_namespace()
    first = _acquire(
        namespace,
        tmp_path,
        "c32a1001",
        "pi-codex-chiap08-c32a1001-manual",
        "rev-a",
        "/home/skuser01/work/sklegal-c32a1001",
        pid=os.getpid(),
        now="2026-09-12T06:00:00+00:00",
    )
    assert first["admitted"] is True

    second = _acquire(
        namespace,
        tmp_path,
        "c32a1001",
        "pi-glm-chiap08-c32a1001",
        "rev-b",
        "/home/skuser01/work/sklegal-c32a1001",
        pid=_dead_pid(),
        now="2026-09-12T06:00:01+00:00",
    )
    assert second["admitted"] is False
    assert second["reason"] == "live-holder"
    holder = second["receipt"]
    assert holder["owner"] == "pi-codex-chiap08-c32a1001-manual"
    assert holder["claim_revision"] == "rev-a"
    assert holder["workspace"] == os.path.realpath("/home/skuser01/work/sklegal-c32a1001")


def test_live_holder_via_unit_refuses_second_launcher(tmp_path) -> None:
    """The 0d7331e7 shape: the recorded worker unit is still active."""

    def runner(_argv, **_kwargs):
        class _Result:
            returncode = 0

        return _Result()

    namespace = _load_namespace()
    _acquire(
        namespace,
        tmp_path,
        "0d7331e7",
        "cursor-w105-focus",
        "466349d7384c45adbea59491ec080ac2",
        "/home/skuser01/work/sklegal-0d7331e7",
        pid=_dead_pid(),
        runner=runner,
    )
    second = _acquire(
        namespace,
        tmp_path,
        "0d7331e7",
        "pi-codex-chiap04-0d7331e7",
        "fe5cd901d33149dba747829f561d1d08",
        "/home/skuser01/work/sklegal-0d7331e7",
        pid=_dead_pid(),
        runner=runner,
    )
    assert second["admitted"] is False
    assert second["reason"] == "live-holder"
    assert second["receipt"]["owner"] == "cursor-w105-focus"


def test_stale_holder_is_recovered_and_second_launcher_admitted(tmp_path) -> None:
    """Stale recovery needs fresh proof the holder is gone, then admits."""

    def runner(_argv, **_kwargs):
        class _Result:
            returncode = 1

        return _Result()

    namespace = _load_namespace()
    _acquire(
        namespace,
        tmp_path,
        "deadbeef",
        "pi-codex-chiap08-deadbeef",
        "rev-a",
        "/home/skuser01/work/skcapstone-deadbeef",
        pid=_dead_pid(),
        runner=runner,
    )
    second = _acquire(
        namespace,
        tmp_path,
        "deadbeef",
        "pi-glm-chiap08-deadbeef",
        "rev-b",
        "/home/skuser01/work/skcapstone-deadbeef",
        pid=_dead_pid(),
        runner=runner,
    )
    assert second["admitted"] is True
    assert second["receipt"]["owner"] == "pi-glm-chiap08-deadbeef"


def test_malformed_receipt_is_recovered(tmp_path) -> None:
    namespace = _load_namespace()
    path = namespace["_admission_lock_path"](str(tmp_path), "badc0de")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("not-json")
    result = _acquire(
        namespace,
        tmp_path,
        "badc0de",
        "pi-codex-chiap08-badc0de",
        "rev-a",
        "/home/skuser01/work/skcapstone-badc0de",
        pid=_dead_pid(),
    )
    assert result["admitted"] is True


def test_distinct_cards_admit_in_parallel(tmp_path) -> None:
    """Criterion: distinct cards with distinct worktrees remain parallel."""
    namespace = _load_namespace()
    first = _acquire(
        namespace,
        tmp_path,
        "aaaa0001",
        "pi-codex-chiap08-aaaa0001",
        "rev-a",
        "/home/skuser01/work/ws-aaaa0001",
        pid=_dead_pid(),
    )
    second = _acquire(
        namespace,
        tmp_path,
        "aaaa0002",
        "pi-codex-chiap08-aaaa0002",
        "rev-b",
        "/home/skuser01/work/ws-aaaa0002",
        pid=_dead_pid(),
    )
    assert first["admitted"] is True
    assert second["admitted"] is True


def test_receipt_is_non_secret_and_deterministic(tmp_path) -> None:
    """Receipts expose only the fixed field set and persist byte-identically."""
    namespace = _load_namespace()
    first = _acquire(
        namespace,
        tmp_path,
        "feedbeef",
        "pi-codex-chiap08-feedbeef",
        "rev-a",
        "/home/skuser01/work/ws-feedbeef",
        pid=1234,
        now="2026-09-12T06:00:00+00:00",
    )
    receipt = first["receipt"]
    assert set(receipt) == RECEIPT_FIELDS
    on_disk = namespace["_read_admission_receipt"](
        namespace["_admission_lock_path"](str(tmp_path), "feedbeef")
    )
    assert on_disk == receipt


def test_launch_path_checks_admission_after_claim_before_process() -> None:
    """The gate sits between the claim classification and process creation."""
    source = SCRIPT.read_text(encoding="utf-8")
    claim_check = source.index("_admission=acquire_card_admission(")
    claim_gate = source.index('if claim_outcome == "claim_refused":')
    process_create = source.index("r=subprocess.run(_worker_launch_command(")
    assert claim_gate < claim_check < process_create
    assert "ADMISSION_REFUSED|%s|%s|%s|reason=%s|holder_owner=%s|" in source
    assert "ADMISSION_REFUSED_TOTAL" in source


# ---------------------------------------------------------------------------
# d11ca7ef: race tests — exactly one process starts across concurrent
# launchers, the in-flight receipt write is never stolen, and the live
# 84baffed reproduction shape (duplicate unit after authoritative ownership,
# stopped exactly) is folded in.
# ---------------------------------------------------------------------------


def test_concurrent_launchers_exactly_one_admission(tmp_path) -> None:
    """16 barrier-released launchers race one card: exactly one may start."""
    namespace = _load_namespace()
    barrier = threading.Barrier(17)
    results: list = []
    lock = threading.Lock()

    def contender(index: int) -> None:
        barrier.wait(timeout=30)
        result = _acquire(
            namespace,
            tmp_path,
            "c32a1001",
            (
                "pi-codex-chiap08-c32a1001-manual"
                if index == 0
                else "pi-glm-chiap08-c32a1001-%d" % index
            ),
            "rev-a" if index == 0 else "rev-%d" % index,
            "/home/skuser01/work/sklegal-c32a1001",
            # One live process identity models one live model process; the
            # holder's pid stays alive for every contender's liveness probe.
            pid=os.getpid(),
        )
        with lock:
            results.append(result)

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=30)
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 16
    assert sum(int(r["admitted"]) for r in results) == 1, results
    assert all(r["reason"] == "live-holder" for r in results if not r["admitted"]), results
    namespace["_release_card_admission"](str(tmp_path), "c32a1001")


def test_inflight_receipt_write_is_waited_not_stolen(tmp_path) -> None:
    """An empty receipt mid-write is awaited, never recovered as stale."""
    namespace = _load_namespace()
    path = namespace["_admission_lock_path"](str(tmp_path), "0d7331e7")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").close()  # created, not yet written

    def finish_write() -> None:
        time.sleep(0.1)
        receipt = {
            "schema_version": 1,
            "host": "chiap08",
            "card_id": "0d7331e7",
            "owner": "cursor-w105-focus",
            "claim_revision": "466349d7384c45adbea59491ec080ac2",
            "workspace": "/home/skuser01/work/sklegal-0d7331e7",
            "unit": "skfleet-worker-cursor-0d7331e7.service",
            "session": "cursor-auto-0d7331e7",
            "pid": os.getpid(),
            "acquired_at": "2026-09-12T06:50:30+00:00",
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True)

    writer = threading.Thread(target=finish_write)
    writer.start()
    result = _acquire(
        namespace,
        tmp_path,
        "0d7331e7",
        "pi-codex-chiap04-0d7331e7",
        "fe5cd901d33149dba747829f561d1d08",
        "/home/skuser01/work/sklegal-0d7331e7",
        pid=_dead_pid(),
    )
    writer.join()
    assert result["admitted"] is False
    assert result["reason"] == "live-holder", result
    assert result["receipt"]["owner"] == "cursor-w105-focus"


def test_84baffed_shape_unit_holder_blocks_then_self_release_recovers(
    tmp_path,
) -> None:
    """Live 84baffed fold: duplicate unit f9333eed... blocked after cursor
    ownership; exact self-release of the duplicate recovers the lock."""
    namespace = _load_namespace()
    unit = "skfleet-worker-codex-84baffed.service"

    def runner(argv, **_kwargs):
        class _Result:
            returncode = 0 if unit in argv else 1

        return _Result()

    duplicate = _acquire(
        namespace,
        tmp_path,
        "84baffed",
        "pi-codex-chiap01-84baffed",
        "f9333eedd5fd4f248e7204c61cc90eca",
        "/home/skuser01/work/skcapstone-84baffed",
        unit=unit,
        session="codex-auto-84baffed",
        pid=_dead_pid(),
        runner=runner,
    )
    assert duplicate["admitted"] is True
    # Authoritative owner arrives after the duplicate unit is live; the
    # concurrent launcher must be refused while the unit runs.
    refused = _acquire(
        namespace,
        tmp_path,
        "84baffed",
        "cursor-84baffed",
        "2f6a04874f014d61ad4e59bacca0f3d9",
        "/home/skuser01/work/skcapstone-84baffed",
        unit="skfleet-worker-cursor-84baffed.service",
        session="cursor-auto-84baffed",
        pid=_dead_pid(),
        runner=runner,
    )
    assert refused["admitted"] is False
    assert refused["reason"] == "live-holder"
    assert refused["receipt"]["claim_revision"] == "f9333eedd5fd4f248e7204c61cc90eca"
    # The duplicate is stopped exactly: it releases only its own receipt.
    namespace["_release_card_admission"](str(tmp_path), "84baffed")
    recovered = _acquire(
        namespace,
        tmp_path,
        "84baffed",
        "cursor-84baffed",
        "2f6a04874f014d61ad4e59bacca0f3d9",
        "/home/skuser01/work/skcapstone-84baffed",
        unit="skfleet-worker-cursor-84baffed.service",
        session="cursor-auto-84baffed",
        pid=_dead_pid(),
        runner=runner,
    )
    assert recovered["admitted"] is True


def test_claim_displaced_recheck_sits_between_lock_and_process() -> None:
    """The d11ca7ef recheck refolds the claim under the lock pre-launch."""
    source = SCRIPT.read_text(encoding="utf-8")
    lock = source.index("_admission=acquire_card_admission(")
    recheck = source.index(
        "_final_owner,_final_at,_final_revision=_current_claim_identity_fresh(cid)"
    )
    launch = source.index("r=subprocess.run(_worker_launch_command(")
    assert lock < recheck < launch
    assert "reason=claim-displaced" in source
    assert "_release_card_admission(HOME,cid)" in source
