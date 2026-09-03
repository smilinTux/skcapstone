"""Regression coverage for append-only void resurrection reconciliation."""

import json
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone.coordination import Board

_SCRIPT = Path(__file__).parents[1] / "scripts" / "repair_void_resurrections.py"
_SPEC = spec_from_file_location("repair_void_resurrections", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
repair = _MODULE.repair


def _append_historical_resurrection(
    store: CardStore, card_id: str, action: str, *, column: str
) -> None:
    """Synthesize an event written before void became terminal."""
    events_dir = store.home / "cards" / card_id / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "action": action,
        "column": column,
        "seq": 0,
        "ts": datetime.now(timezone.utc).isoformat(),
        "writer": "historical-bypass",
    }
    with (events_dir / "historical-bypass.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def test_repair_rearchives_without_changing_void_audit_record(tmp_path) -> None:
    store = CardStore(tmp_path)
    card_id = "voidfix1"
    store.create(CardCore(id=card_id, kind="task", title="void", created_by="test"))
    store.append_event(card_id, "void", "chef", reason="Superseded by successor")
    store.append_event(card_id, "archive", "chef")
    _append_historical_resurrection(store, card_id, "move", column="ready")
    original_void = next(
        event for event in store._read_events(card_id) if event["action"] == "void"
    )

    result = repair(tmp_path, writer="void-reconcile", apply=True)

    current_void = next(
        event for event in store._read_events(card_id) if event["action"] == "void"
    )
    assert result["candidate_count"] == result["repaired_count"] == 1
    assert result["remaining_count"] == 0
    assert current_void == original_void
    assert current_void["writer"] == "chef"
    assert current_void["reason"] == "Superseded by successor"
    assert store.fold(card_id).archived is True
    assert card_id in Board(tmp_path).archived_ids()


def test_repair_dry_run_writes_nothing(tmp_path) -> None:
    store = CardStore(tmp_path)
    card_id = "voidfix2"
    store.create(CardCore(id=card_id, kind="task", title="void", created_by="test"))
    store.append_event(card_id, "void", "lumina", reason="Duplicate")
    store.append_event(card_id, "archive", "lumina")
    _append_historical_resurrection(store, card_id, "reopen", column="backlog")
    before = store._read_events(card_id)

    result = repair(tmp_path, writer="void-reconcile", apply=False)

    assert result["candidate_count"] == 1
    assert result["repaired_count"] == 0
    assert store._read_events(card_id) == before
