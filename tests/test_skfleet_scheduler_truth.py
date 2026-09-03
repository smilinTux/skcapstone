"""Fleet logs distinguish structural availability from scheduler-safe work."""

import ast
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


def test_worker_health_is_observed_before_jarvis_releases() -> None:
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
