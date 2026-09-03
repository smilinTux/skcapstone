"""Regression tests for task-only dispatch and unique fleet counts."""

from __future__ import annotations

import ast
import os
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
    namespace: dict[str, object] = {"os": os}
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_exact_five_host_lane_targets_and_chiap08_dry_summary() -> None:
    """Host profiles drive exact Codex and GLM ceilings."""
    helpers = _load_functions("_required_lane_target", "_slot_summary")
    target = helpers["_required_lane_target"]
    profiles = {
        "chiap01": (8, 3, 6),
        "chiap02": (8, 3, 6),
        "chiap03": (8, 3, 6),
        "chiap04": (4, 0, 6),
        "chiap08": (3, 0, 6),
    }
    for host, expected in profiles.items():
        env = {
            "SKFLEET_TARGET": str(expected[0]),
            "SKFLEET_GLM_TARGET": str(expected[1]),
            "SKFLEET_QWEN_TARGET": str(expected[2]),
        }
        actual = (
            target("SKFLEET_TARGET", env),
            target("SKFLEET_GLM_TARGET", env),
            target("SKFLEET_QWEN_TARGET", env),
        )
        assert actual == expected, host

    codex, glm, qwen = profiles["chiap08"]
    lanes = [
        {"name": name, "busy": [], "target": ceiling, "free": ceiling}
        for name, ceiling in (
            ("codex", codex),
            ("glm", glm),
            ("qwen", qwen),
            ("escalate", 2),
        )
    ]
    assert helpers["_slot_summary"](lanes) == (
        "codex=0/3 glm=0/0 qwen=0/6 escalate=0/2|total_free=11"
    )

    source = ROTATE.read_text(encoding="utf-8")
    assert 'TARGET=_required_lane_target("SKFLEET_TARGET")' in source
    assert 'GLM_TARGET=_required_lane_target("SKFLEET_GLM_TARGET")' in source
    assert 'QWEN_TARGET=_required_lane_target("SKFLEET_QWEN_TARGET", default="6")' in source
    assert '"target":TARGET' in source
    assert '"target":0 if glm_held else GLM_TARGET' in source
    assert '"target":QWEN_TARGET' in source
    assert '"target":int(os.environ.get("SKFLEET_ESC_TARGET","2"))' in source


def test_lane_targets_fail_closed_when_missing_or_invalid() -> None:
    """Missing, empty, non-integer, and negative targets stop rotation."""
    target = _load_functions("_required_lane_target")["_required_lane_target"]
    valid = {"SKFLEET_TARGET": "3", "SKFLEET_GLM_TARGET": "0"}
    for name in valid:
        for invalid in (None, "", "three", "-1"):
            env = dict(valid)
            if invalid is None:
                env.pop(name)
            else:
                env[name] = invalid
            try:
                target(name, env)
            except SystemExit as exc:
                assert str(exc) == (f"BLOCKED|{name}|missing or invalid non-negative integer")
            else:
                raise AssertionError(f"{name}={invalid!r} did not fail closed")


def test_task_only_coord_claim_excludes_itil_ids() -> None:
    """Incident and problem records never reach the task-only claim command."""
    claimable = _load_functions("_coord_task_claimable")["_coord_task_claimable"]
    assert claimable({"id": "a1b2c3d4", "kind": "task"}) is True
    assert claimable({"id": "inc-0e190b2f", "kind": "incident"}) is False
    assert claimable({"id": "prb-41b9fb96", "kind": "problem"}) is False
    source = ROTATE.read_text(encoding="utf-8")
    assert "if not _coord_task_claimable(core):" in source


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


def _sample_records() -> dict[str, str]:
    return {
        "CHIAP01_RECORD": "META\tchiap01\tok\tsuccess\nPOOL\tchiap01\t"
        "POOL|chiap01|ready=2 POOL_IDS|chiap01|ids=aaaaaaaa,bbbbbbbb",
        "CHIAP02_RECORD": "META\tchiap02\tok\tsuccess\nPOOL\tchiap02\t"
        "POOL|chiap02|ready=2 POOL_IDS|chiap02|ids=aaaaaaaa,bbbbbbbb",
        "CHIAP03_RECORD": "META\tchiap03\tok\tsuccess\nPOOL\tchiap03\t"
        "POOL|chiap03|ready=2 POOL_IDS|chiap03|ids=bbbbbbbb,cccccccc",
        "CHIAP04_RECORD": "META\tchiap04\tok\tsuccess\nPOOL\tchiap04\t"
        "POOL|chiap04|ready=1 POOL_IDS|chiap04|ids=cccccccc",
        "CHIAP08_RECORD": "META\tchiap08\tok\tsuccess\nPOOL\tchiap08\t"
        "POOL|chiap08|ready=3 POOL_IDS|chiap08|ids=aaaaaaaa,bbbbbbbb,cccccccc",
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
probe_host() { record_for "$1"; }
collect_claims() {
  local variable line kind host session card lane state command identity revision
  for variable in CHIAP01_RECORD CHIAP02_RECORD CHIAP03_RECORD CHIAP04_RECORD CHIAP08_RECORD; do
    while IFS=$'\t' read -r kind host session card lane state command identity revision; do
      [[ "$kind" == SESSION && "$state" == live ]] || continue
      printf 'CLAIM\t%s\t%s\tdoing\trevision\t%s\t%s\n' "$card" "$identity" "$host" "$lane"
    done <<<"${!variable}"
  done
}
collect_gateway_activity() { printf 'GATEWAY_UNATTRIBUTED\t0\n'; }
curl() { printf '{"pool":{"totalActive":0,"totalQueued":0}}\n'; }
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
    empty_records["CHIAP04_RECORD"] = (
        "META\tchiap04\tok\tsuccess\nPOOL\tchiap04\tPOOL|chiap04|ready=0 POOL_IDS|chiap04|ids=-"
    )
    output = _run_watch_sample(tmp_path / "empty", empty_records)
    assert "workable=3" in output
    assert "candidate_inventory_missing_hosts=0" in output

    bad_records = {
        "missing": "POOL|chiap04|ready=1",
        "host-mismatch": "POOL|chiap04|ready=1 POOL_IDS|chiap03|ids=dddddddd",
        "pool-host-mismatch": "POOL|chiap03|ready=1 POOL_IDS|chiap04|ids=dddddddd",
        "invalid-id": "POOL|chiap04|ready=2 POOL_IDS|chiap04|ids=dddddddd,NOTHEX",
        "count-mismatch": "POOL|chiap04|ready=2 POOL_IDS|chiap04|ids=dddddddd",
    }
    for name, record in bad_records.items():
        records = _sample_records()
        records["CHIAP04_RECORD"] = f"META\tchiap04\tok\tsuccess\nPOOL\tchiap04\t{record}"
        output = _run_watch_sample(tmp_path / name, records)
        assert "workable=3" in output
        assert "candidate_inventory_missing_hosts=1" in output

    assert "POOL_IDS|%s|ids=%s" in ROTATE.read_text(encoding="utf-8")
    assert "record_candidate_pool" in WATCH.read_text(encoding="utf-8")


def test_escalation_only_sessions_keep_distribution_watch_up(tmp_path: Path) -> None:
    """The structured probe counts every governed worker lane."""
    source = WATCH.read_text(encoding="utf-8")
    for prefix in ("codex-auto-*", "glm-auto-*", "qwen-auto-*", "esc-auto-*"):
        assert prefix in source

    records = _sample_records()
    records["CHIAP01_RECORD"] += (
        "\nSESSION\tchiap01\tesc-auto-deadbeef\tdeadbeef\tescalate\tlive\tpi"
        "\tpi-escalate-chiap01-deadbeef\t-"
    )
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
    assert '_NOT_CLAIMABLE = {"not-claimable", "sprint-container", "do-not-claim"}' in rotate
    assert "if non_implementation(folded_core, labels):" in rotate
    assert "pin = host_pin(folded_core, labels)" in rotate
    assert 'MAX_LAUNCH=int(os.environ.get("SKFLEET_MAX_LAUNCH","11"))' in rotate
    assert 'remaining={lane["name"]:lane["free"] for lane in LANES}' in rotate
    assert "off = ROTATION_HOSTS.index(HOST) if HOST in ROTATION_HOSTS else 0" in rotate
    assert '_LANE_RANK={"qwen":0,"glm":1,"kimi":2,"codex":3,"escalate":4}' in rotate
    assert "chiap01 chiap02 chiap03 chiap04 chiap08" in watch
    assert "sleep 300" in watch
