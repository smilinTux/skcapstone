"""Regression tests for task-only dispatch and unique fleet counts."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
WATCH = ROOT / "scripts" / "fleet" / "skfleet-distribution-watch.sh"


def _load_functions(*names: str) -> dict[str, object]:
    """Load selected dependency-free functions without running the launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(wanted) == set(names)
    module = ast.Module(body=[wanted[name] for name in names], type_ignores=[])
    namespace: dict[str, object] = {}
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_task_only_coord_claim_excludes_itil_ids() -> None:
    """Incident and problem records never reach the task-only claim command."""
    claimable = _load_functions("_coord_task_claimable")["_coord_task_claimable"]
    assert claimable({"id": "a1b2c3d4", "kind": "task"}) is True
    assert claimable({"id": "inc-0e190b2f", "kind": "incident"}) is False
    assert claimable({"id": "prb-41b9fb96", "kind": "problem"}) is False
    source = ROTATE.read_text(encoding="utf-8")
    assert "if not _coord_task_claimable(core): continue" in source


def test_claim_refusal_is_not_a_race() -> None:
    """Only a changed final assignability check is classified as a race."""
    classify = _load_functions("_classify_claim_outcome")["_classify_claim_outcome"]
    assert classify(False) == "raced"
    assert classify(True, 1, None, "codex-chiap08-a1b2c3d4") == "claim_refused"
    assert classify(True, 0, "another-owner", "codex-chiap08-a1b2c3d4") == "claim_refused"
    assert (
        classify(
            True,
            0,
            "codex-chiap08-a1b2c3d4",
            "codex-chiap08-a1b2c3d4",
        )
        == "claimed"
    )
    source = ROTATE.read_text(encoding="utf-8")
    assert source.count("raced += 1") == 1
    assert "CLAIM_REFUSED|" in source
    assert "CLAIM_FAILED|" not in source


def test_pool_pass_parking_metric_is_truthful_and_selection_stays_stable() -> None:
    """The wire rename preserves counter position and candidate selection."""
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    pool_formats = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("POOL|%s|ready=")
    ]
    assert pool_formats == [
        "POOL|%s|ready=%d sklegal=%d eng=%d biz=%d dep_blocked=%d "
        "unclaimable=%d itil_closed=%d blocked_backoff=%d "
        "pass_outcome_parked=%d pinned_elsewhere=%d foreign=%d "
        "not_claimable=%d top_unblocks=%d"
    ]
    assert " awaiting_review=" not in source

    pool_log = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.BinOp)
        and isinstance(node.args[1].left, ast.Constant)
        and node.args[1].left.value == pool_formats[0]
    )
    counter_args = pool_log.args[1].right
    assert isinstance(counter_args, ast.Tuple)
    assert [ast.unparse(item) for item in counter_args.elts] == [
        "HOST",
        "len(pool)",
        "lc[0]",
        "lc[1]",
        "lc[2]",
        "blocked",
        "skipped_unclaimable",
        "skipped_terminal",
        "skipped_blocked",
        "skipped_review",
        "pinned_elsewhere",
        "foreign_skipped",
        "not_claimable_skipped",
        "top",
    ]

    # Keep the selector and ownership expressions explicit in this focused test.
    assert "pool.sort(key=lambda x:(x[0],-x[4],x[1],x[2]))" in source
    assert "owned=[x for x in pool if owns(x[2])]" in source


def test_pass_outcome_parking_includes_rereview_and_excludes_non_pass() -> None:
    """Only successful outcomes are recognized by the parking predicate."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_PASS_RE"
            for target in node.targets
        )
    )
    assert isinstance(assignment.value, ast.Call)
    pattern = re.compile(ast.literal_eval(assignment.value.args[0]), re.I)

    for outcome in ("PASS", "PASS detail", "PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"):
        assert pattern.match(outcome), outcome
    for outcome in (
        "BLOCKED",
        "FAIL",
        "NOT_PASS",
        "PASSING",
        "PASS_FOR_REVIEW_PENDING",
        "PASS_FOR_REREVIEW_PENDING",
    ):
        assert not pattern.match(outcome), outcome


def _sample_records() -> dict[str, str]:
    return {
        "CHIAP01_RECORD": "0|success|POOL|chiap01|ready=2 POOL_IDS|chiap01|ids=aaaaaaaa,bbbbbbbb",
        "CHIAP02_RECORD": "0|success|POOL|chiap02|ready=2 POOL_IDS|chiap02|ids=aaaaaaaa,bbbbbbbb",
        "CHIAP03_RECORD": "0|success|POOL|chiap03|ready=2 POOL_IDS|chiap03|ids=bbbbbbbb,cccccccc",
        "CHIAP04_RECORD": "0|success|POOL|chiap04|ready=1 POOL_IDS|chiap04|ids=cccccccc",
        "CHIAP08_RECORD": "0|success|POOL|chiap08|ready=3 "
        "POOL_IDS|chiap08|ids=aaaaaaaa,bbbbbbbb,cccccccc",
    }


def _run_watch_sample(tmp_path: Path, records: dict[str, str]) -> str:
    script = """
set -euo pipefail
export SKFLEET_DISTRIBUTION_WATCH_LIB_ONLY=1
source "$WATCH_PATH"
record_for() {
  case "$1" in
    chiap01) printf '%s\n' "$CHIAP01_RECORD" ;;
    chiap02) printf '%s\n' "$CHIAP02_RECORD" ;;
    chiap03) printf '%s\n' "$CHIAP03_RECORD" ;;
    chiap04) printf '%s\n' "$CHIAP04_RECORD" ;;
    chiap08) printf '%s\n' "$CHIAP08_RECORD" ;;
  esac
}
ssh() {
  local arg host=""
  for arg in "$@"; do
    case "$arg" in chiap01|chiap02|chiap03|chiap04) host=$arg ;; esac
  done
  record_for "$host"
}
bash() { record_for chiap08; }
curl() { printf '{"totalActive":0,"totalQueued":0}\n'; }
skmail() { :; }
sample
"""
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "WATCH_PATH": str(WATCH),
            **records,
        },
    )
    return result.stdout.strip()


def test_five_host_candidate_inventory_counts_unique_ids(tmp_path: Path) -> None:
    """The exact five-host sample path counts once and rejects bad records."""
    output = _run_watch_sample(tmp_path / "valid", _sample_records())
    assert "workable=3" in output
    assert "candidate_inventory_missing_hosts=0" in output

    empty_records = _sample_records()
    empty_records["CHIAP04_RECORD"] = "0|success|POOL|chiap04|ready=0 POOL_IDS|chiap04|ids=-"
    output = _run_watch_sample(tmp_path / "empty", empty_records)
    assert "workable=3" in output
    assert "candidate_inventory_missing_hosts=0" in output

    bad_records = {
        "missing": "0|success|POOL|chiap04|ready=1",
        "host-mismatch": "0|success|POOL|chiap04|ready=1 POOL_IDS|chiap03|ids=dddddddd",
        "pool-host-mismatch": "0|success|POOL|chiap03|ready=1 POOL_IDS|chiap04|ids=dddddddd",
        "invalid-id": "0|success|POOL|chiap04|ready=2 POOL_IDS|chiap04|ids=dddddddd,NOTHEX",
        "count-mismatch": "0|success|POOL|chiap04|ready=2 POOL_IDS|chiap04|ids=dddddddd",
    }
    for name, record in bad_records.items():
        records = _sample_records()
        records["CHIAP04_RECORD"] = record
        output = _run_watch_sample(tmp_path / name, records)
        assert "workable=3" in output
        assert "candidate_inventory_missing_hosts=1" in output

    assert "POOL_IDS|%s|ids=%s" in ROTATE.read_text(encoding="utf-8")
    assert 'grep "POOL_IDS|"' in WATCH.read_text(encoding="utf-8")


def test_escalation_only_sessions_keep_distribution_watch_up(tmp_path: Path) -> None:
    """Both host probes count escalation workers instead of reporting zero."""
    source = WATCH.read_text(encoding="utf-8")
    probe = 'grep -Ec "^(codex-auto-|glm-auto-|esc-auto-)"'
    assert source.count(probe) == 2
    assert 'grep -Ec "^(codex-auto-|glm-auto-)"' not in source

    count = subprocess.run(
        ["grep", "-Ec", "^(codex-auto-|glm-auto-|esc-auto-)"],
        input="esc-auto-deadbeef\n",
        check=True,
        capture_output=True,
        text=True,
    )
    assert count.stdout.strip() == "1"

    records = _sample_records()
    records["CHIAP01_RECORD"] = records["CHIAP01_RECORD"].replace("0|", "1|", 1)
    output = _run_watch_sample(tmp_path / "escalation-only", records)
    assert "state=up workers=1" in output


def test_legacy_pool_is_reported_missing_without_double_counting(
    tmp_path: Path,
) -> None:
    """A simplified fixture cannot bypass the exact record parser."""
    script = f"""
set -euo pipefail
export SKFLEET_DISTRIBUTION_WATCH_LIB_ONLY=1
source {WATCH}
reset_candidate_inventory
record_candidate_pool chiap01 'POOL|chiap01|ready=2 ids=aaaaaaaa,bbbbbbbb'
printf '%d|%d\n' "${{#candidate_ids[@]}}" "$candidate_manifests_missing"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result.stdout.strip() == "0|1"


def test_existing_holds_reservations_capacity_and_cadence_remain() -> None:
    """The repair retains the scheduler's existing structural safety rails."""
    rotate = ROTATE.read_text(encoding="utf-8")
    watch = WATCH.read_text(encoding="utf-8")
    assert '_NOT_CLAIMABLE = {"not-claimable", "sprint-container"}' in rotate
    assert "if non_implementation(core,labels): continue" in rotate
    assert "_pin = host_pin(core,labels)" in rotate
    assert 'MAX_LAUNCH=int(os.environ.get("SKFLEET_MAX_LAUNCH","11"))' in rotate
    assert 'remaining={lane["name"]:lane["free"] for lane in LANES}' in rotate
    assert "hosts=(chiap01 chiap02 chiap03 chiap04 chiap08)" in watch
    assert "sleep 300" in watch
