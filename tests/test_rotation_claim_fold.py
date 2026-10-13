"""Regression tests for the fleet rotation's claim event source."""

import ast
import json
from pathlib import Path


def _load_event_fold(tmp_path: Path):
    source = Path("scripts/fleet/skfleet-rotate.py").read_text()
    tree = ast.parse(source)
    wanted = {"_load_legacy_rows", "event_rows"}
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    module = ast.Module(body=body, type_ignores=[])
    namespace = {
        "CARDS": str(tmp_path / "cards"),
        "LEGACY_EVENTS": str(tmp_path / "coordination" / "card_events"),
        "Path": Path,
        "glob": __import__("glob"),
        "json": json,
        "os": __import__("os"),
        "_legacy_rows": None,
        "_rows": {},
    }
    exec(compile(module, "skfleet-rotate.py", "exec"), namespace)
    return namespace["event_rows"]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_rotation_event_rows_union_cardstore_and_legacy_events(tmp_path: Path) -> None:
    """Legacy unassign must clear a native claim in the rotation fold too."""
    cid = "2b614910"
    _write_jsonl(
        tmp_path / "cards" / cid / "events" / "chiap08.jsonl",
        [
            {
                "event_id": "claim-revision",
                "card_id": cid,
                "action": "claim",
                "owner": "dead-worker",
                "writer": "dead-worker",
                "ts": "2026-08-22T04:42:39+00:00",
                "seq": 0,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "coordination" / "card_events" / "chiap08.jsonl",
        [
            {
                "card_id": cid,
                "action": "unassign",
                "writer": "jarvis",
                "ts": "2026-08-22T05:03:55+00:00",
                "seq": 0,
            }
        ],
    )

    rows = _load_event_fold(tmp_path)(cid)

    assert [row["action"] for row in rows] == ["claim", "unassign"]


def test_rotation_event_rows_use_cardstore_merge_order(tmp_path: Path) -> None:
    """The event union orders equal timestamps by writer and sequence."""
    cid = "equal-ts"
    timestamp = "2026-08-22T05:03:55+00:00"
    _write_jsonl(
        tmp_path / "cards" / cid / "events" / "node.jsonl",
        [
            {
                "event_id": "native",
                "card_id": cid,
                "action": "claim",
                "writer": "z-writer",
                "ts": timestamp,
                "seq": 0,
            }
        ],
    )
    _write_jsonl(
        tmp_path / "coordination" / "card_events" / "node.jsonl",
        [
            {
                "card_id": cid,
                "action": "unassign",
                "writer": "a-writer",
                "ts": timestamp,
                "seq": 2,
            }
        ],
    )

    rows = _load_event_fold(tmp_path)(cid)

    assert [row["writer"] for row in rows] == ["a-writer", "z-writer"]
