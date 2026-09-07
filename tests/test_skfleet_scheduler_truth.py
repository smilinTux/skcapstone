"""Fleet logs distinguish structural availability from scheduler-safe work."""

import ast
import glob
import hashlib
import json
import os
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def test_pool_report_exposes_owned_and_unleased_work() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "owned_ready=%d" in source
    assert "structural_leaf=%d human_gated=%d" in source
    assert "leaf_eligibility_counts" in source
    assert '"safety_filtered=%d top_unblocks=%d"' in source
    assert 'reason.startswith("owned-")' in source


def test_worker_health_is_observed_before_niobe_releases() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    health = source.index("MeroObservation(", source.index("def reap_dead_claims"))
    outcome = source.index("_record_reap_outcome(", health)
    release = source.index('"--agent", "jarvis"', outcome)

    assert health < outcome < release
    assert "WORKER_HEALTH|%s|sessions=%d claims_exact=%d" in source
    assert "duplicates=%d" in source


def test_worker_health_joins_sessions_to_exact_owner_and_detects_duplicates(tmp_path) -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_health_snapshot"
    )
    cards = tmp_path / "cards"
    (cards / "deadbeef").mkdir(parents=True)
    (cards / "deadbeef" / "core.json").write_text(json.dumps({"id": "deadbeef"}), encoding="utf-8")
    namespace = {
        "CARDS": str(cards),
        "HOST": "chiap08",
        "LANES": [
            {"name": "codex", "prefix": "codex-auto-"},
            {"name": "glm", "prefix": "glm-auto-"},
        ],
        "json": json,
        "os": os,
        "seat_for": lambda _cid, _core: None,
        "_worker_owner": lambda lane, cid, _seat: f"pi-{lane}-chiap08-{cid}",
        "_current_claim_identity_fresh": lambda _cid: (
            "pi-codex-chiap08-deadbeef",
            1.0,
            "revision-1",
        ),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)

    assert namespace["_worker_health_snapshot"](["codex-auto-deadbeef", "glm-auto-deadbeef"]) == {
        "sessions": 2,
        "claims_exact": 1,
        "mismatched": 1,
        "duplicates": 1,
    }


def test_pool_v2_claimability_is_fail_closed_for_unknown_and_review() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_pool_v2_claimability_flags"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    flags = namespace["_pool_v2_claimability_flags"]

    assert flags({"claimable": True, "reason": "claimable"}) == (False, False)
    assert flags({"claimable": False, "reason": "review"}) == (False, True)
    assert flags({"claimable": False, "reason": "new-unmapped-state"}) == (True, False)
    assert flags({"reason": "missing-claimability"}) == (True, False)


def test_pool_v2_selection_and_preclaim_share_admission_fingerprint() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "_POOL_V2_ADMISSIONS" in source
    assert "_pool_v2_admission_fingerprint(fresh_claimability)" in source
    assert "SKIPPED_ADMISSION_DRIFT" in source
    assert "fresh_claimability.get(\"claimable\") is not True" in source


def test_pool_v2_source_revision_uses_raw_core_for_amended_cards() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_pool_v2_source_revision"
    )
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "_strict_card_events": lambda _cid, fresh=False: [],
        "_legacy_claimability_events": lambda fresh=False: {},
        "_load_outcome_revision_rows": lambda fresh=False: {},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    revision = namespace["_pool_v2_source_revision"]
    raw = {"id": "deadbeef", "title": "original"}
    folded = {"id": "deadbeef", "title": "amended"}

    assert revision("deadbeef", raw) == revision("deadbeef", raw, fresh=True)
    assert revision("deadbeef", raw) != revision("deadbeef", folded)
    source = SCRIPT.read_text(encoding="utf-8")
    assert "_pool_v2_source_revision(cid, _raw_core, fresh=True)" in source


def test_pool_v2_source_revision_detects_new_outcome_event(tmp_path) -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {
            "_fold_key", "_load_outcome_revision_rows", "_pool_v2_source_revision"
        }
    }
    namespace = {
        "glob": glob,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "_EVID_DIR": str(tmp_path),
        "_OUTCOME_KEYS": ("verdict", "result", "disposition", "review_decision"),
        "_outcome_revision_rows": None,
        "_strict_card_events": lambda _cid, fresh=False: [],
        "_legacy_claimability_events": lambda fresh=False: {},
        "re": __import__("re"),
    }
    exec(
        compile(ast.Module(body=[wanted[name] for name in (
            "_fold_key", "_load_outcome_revision_rows", "_pool_v2_source_revision"
        )], type_ignores=[]),
        str(SCRIPT),
        "exec",
    ), namespace)
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({
        "card_id": "deadbeef", "action": "verdict", "verdict": "PASS",
        "ts": "2026-09-06T20:00:00Z", "event_id": "one",
    }) + "\n", encoding="utf-8")
    revision = namespace["_pool_v2_source_revision"]
    raw = {"id": "deadbeef", "title": "original"}
    before = revision("deadbeef", raw, fresh=True)
    path.write_text(path.read_text(encoding="utf-8") + json.dumps({
        "card_id": "deadbeef", "action": "blocked", "verdict": "BLOCKED",
        "ts": "2026-09-06T20:01:00Z", "event_id": "two",
    }) + "\n", encoding="utf-8")
    after = revision("deadbeef", raw, fresh=True)

    assert before != after
