"""Tests for the event-sourced CardStore engine (Phase 4a)."""
from __future__ import annotations

from skcapstone.card import Column, Kind
from skcapstone.card_store import CardCore, CardStore


def test_create_and_fold_defaults(tmp_path):
    store = CardStore(tmp_path)
    cid = store.create(CardCore(id="c1", title="Do the thing", created_by="opus"))
    assert cid == "c1"
    card = store.fold("c1")
    assert card.title == "Do the thing"
    assert card.status == Column.BACKLOG
    assert card.kind == Kind.TASK
    assert card.source == "cards"
    assert card.originator == "opus"


def test_create_is_write_once(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c2", title="first"))
    store.create(CardCore(id="c2", title="second attempt"))  # loser, ignored
    assert store.fold("c2").title == "first"


def test_move_and_assign_events(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c3", title="movable"))
    store.append_event("c3", "move", "opus", column="doing", order=2)
    store.append_event("c3", "assign", "opus", owner="lumina")
    card = store.fold("c3")
    assert card.status == Column.DOING
    assert card.order == 2
    assert card.owner == "lumina"


def test_claim_and_complete_convenience(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c4", title="lifecycle"))
    store.append_event("c4", "claim", "lumina", owner="lumina")
    assert store.fold("c4").status == Column.DOING  # coord claim = current_task = in_progress
    assert store.fold("c4").owner == "lumina"
    store.append_event("c4", "complete", "lumina")
    assert store.fold("c4").status == Column.DONE


def test_labels_links_priority_swimlane(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c5", title="rich"))
    store.append_event("c5", "add_label", "o", label="urgent")
    store.append_event("c5", "add_label", "o", label="tmp")
    store.append_event("c5", "remove_label", "o", label="tmp")
    store.append_event("c5", "link", "o", link_key="pr", link_value="#7")
    store.append_event("c5", "priority", "o", priority="high")
    store.append_event("c5", "swimlane", "o", swimlane="bug")
    card = store.fold("c5")
    assert "urgent" in card.labels and "tmp" not in card.labels
    assert card.links["pr"] == "#7"
    assert card.priority == "high"
    assert card.swimlane == "bug"


def test_archive_and_reopen(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c6", title="arc"))
    store.append_event("c6", "archive", "o")
    assert store.fold("c6").archived is True
    assert "c6" not in {c.id for c in store.list_cards()}
    assert "c6" in {c.id for c in store.list_cards(include_archived=True)}
    store.append_event("c6", "reopen", "o", column="ready")
    card = store.fold("c6")
    assert card.archived is False
    assert card.status == Column.READY


def test_fold_deterministic_across_writers(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c7", title="multi"))
    # two writers, later ts wins for column
    store.append_event("c7", "move", "agentA", column="ready")
    store.append_event("c7", "move", "agentB", column="doing")
    # fold is ordered by (ts, writer, seq); both exist, last-applied wins
    card = store.fold("c7")
    assert card.status in (Column.READY, Column.DOING)
    # idempotent: fold twice gives same result
    assert store.fold("c7").status == card.status


def test_import_and_parity_roundtrip(tmp_path):
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.card import CardEvent, CardEventLog
    from skcapstone.coordination import AgentFile, Board, Task, TaskPriority
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="p1", title="open feature", created_by="opus"))
    board.create_task(Task(id="p2", title="claimed high", created_by="opus",
                           priority=TaskPriority.HIGH, tags=["bug"]))
    board.save_agent(AgentFile(agent="lumina", current_task="p2", claimed_tasks=["p2"]))
    board.create_task(Task(id="p3", title="done old", created_by="opus"))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["p3"]))
    board.archive_task("p3", by="opus")
    # an explicit overlay move must be reproduced too
    board.create_task(Task(id="p4", title="moved to review", created_by="opus"))
    CardEventLog(tmp_path).append(CardEvent(card_id="p4", action="move", column="review"))

    result = import_from_legacy(tmp_path)
    assert result["imported"] == 4
    parity = parity_check(tmp_path)
    assert parity["matched"] == parity["checked"], parity["mismatches"]
    assert parity["mismatches"] == []
    assert parity["missing"] == []


def test_import_is_idempotent(tmp_path):
    from skcapstone.card_store import import_from_legacy
    from skcapstone.coordination import Board, Task
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="i1", title="x", created_by="o"))
    first = import_from_legacy(tmp_path)
    second = import_from_legacy(tmp_path)
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["skipped"] == 1


def test_flag_gated_read_serves_from_card_store(tmp_path, monkeypatch):
    from skcapstone.card import KanbanBoard
    from skcapstone.card_store import import_from_legacy
    from skcapstone.coordination import Board, Task
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="fr1", title="flagged", created_by="opus"))
    import_from_legacy(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    cards = KanbanBoard(tmp_path).cards()
    assert any(c.id == "fr1" and c.source == "cards" for c in cards)


def test_dual_write_mirrors_create_claim_complete(tmp_path, monkeypatch):
    from skcapstone.card_store import CardStore
    from skcapstone.card import Column
    from skcapstone.coordination import Board, Task
    monkeypatch.setenv("SKCOORD_CARD_STORE", "dual")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="dw1", title="mirrored", created_by="opus"))
    store = CardStore(tmp_path)
    assert store.fold("dw1") is not None
    assert store.fold("dw1").status == Column.BACKLOG
    board.claim_task("lumina", "dw1")
    assert store.fold("dw1").owner == "lumina"
    board.complete_task("lumina", "dw1")
    assert store.fold("dw1").status == Column.DONE


def test_dual_write_disabled_by_default(tmp_path):
    from skcapstone.card_store import CardStore
    from skcapstone.coordination import Board, Task
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="nd1", title="not mirrored", created_by="o"))
    assert CardStore(tmp_path).fold("nd1") is None  # flag off -> no mirror


def test_dual_write_mirrors_archive(tmp_path, monkeypatch):
    from skcapstone.card_store import CardStore
    from skcapstone.coordination import Board, Task
    monkeypatch.setenv("SKCOORD_CARD_STORE", "dual")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="ar1", title="to archive", created_by="opus"))
    assert CardStore(tmp_path).fold("ar1").archived is False
    board.archive_task("ar1", by="opus")
    assert CardStore(tmp_path).fold("ar1").archived is True


def test_get_task_views_from_store_matches_legacy(tmp_path, monkeypatch):
    from skcapstone.card_store import import_from_legacy
    from skcapstone.coordination import AgentFile, Board, Task, TaskPriority
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="v1", title="open", created_by="o"))
    board.create_task(Task(id="v2", title="inprog high", created_by="o",
                           priority=TaskPriority.HIGH, tags=["bug"]))
    board.save_agent(AgentFile(agent="lumina", current_task="v2", claimed_tasks=["v2"]))
    board.create_task(Task(id="v3", title="done", created_by="o"))
    board.save_agent(AgentFile(agent="opus", completed_tasks=["v3"]))

    legacy = {v.task.id: (v.status.value, v.claimed_by) for v in board.get_task_views()}
    import_from_legacy(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    stored = {v.task.id: (v.status.value, v.claimed_by) for v in board.get_task_views()}
    assert stored == legacy, f"legacy={legacy} store={stored}"


def test_read_cutover_falls_back_when_store_empty(tmp_path, monkeypatch):
    # flag=1 but no cards imported: reconstruct returns empty, must not crash.
    from skcapstone.coordination import Board, Task
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="fb1", title="legacy only", created_by="o"))
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    views = board.get_task_views()  # store empty -> returns [] (not a crash)
    assert isinstance(views, list)
