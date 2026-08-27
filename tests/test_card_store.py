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


def test_import_and_parity_roundtrip(tmp_path, monkeypatch):
    from skcapstone.card import CardEvent, CardEventLog
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.coordination import AgentFile, Board, Task, TaskPriority

    # Legacy-only fixture (models pre-cutover data): disable the live mirror so
    # import_from_legacy has real backfill work to do.
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="p1", title="open feature", created_by="opus"))
    board.create_task(
        Task(
            id="p2",
            title="claimed high",
            created_by="opus",
            priority=TaskPriority.HIGH,
            tags=["bug"],
        )
    )
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


def test_import_is_idempotent(tmp_path, monkeypatch):
    from skcapstone.card_store import import_from_legacy
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")  # legacy-only fixture
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
    from skcapstone.card import Column
    from skcapstone.card_store import CardStore
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


def test_dual_write_disabled_only_with_explicit_zero(tmp_path, monkeypatch):
    # Phase 4e: mirroring is ON by default; only an explicit disable token
    # (the rollback escape hatch) turns it off.
    from skcapstone.card_store import CardStore
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="nd1", title="not mirrored", created_by="o"))
    assert CardStore(tmp_path).fold("nd1") is None  # explicit 0 -> no mirror


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
    board.create_task(
        Task(
            id="v2", title="inprog high", created_by="o", priority=TaskPriority.HIGH, tags=["bug"]
        )
    )
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


def test_parity_forces_legacy_read_even_at_flag_1(tmp_path, monkeypatch):
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")  # legacy-only fixture
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="pf1", title="x", created_by="o"))
    import_from_legacy(tmp_path)
    # add a legacy-only task AFTER import -> real drift the monitor must catch
    board.create_task(Task(id="pf2", title="legacy only", created_by="o"))
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")  # cutover mode
    par = parity_check(tmp_path)
    # pf2 exists in legacy but not the store -> must be reported as missing
    assert "pf2" in par["missing"], par


def test_complete_clears_owner_matches_legacy(tmp_path, monkeypatch):
    from skcapstone.card_store import CardStore
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "dual")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="co1", title="x", created_by="o"))
    board.claim_task("opus", "co1")
    board.complete_task("opus", "co1")
    card = CardStore(tmp_path).fold("co1")
    assert card.status.value == "done"
    assert card.owner is None  # matches legacy claimed_by=None for done tasks


def test_claim_demotes_bumped_task(tmp_path, monkeypatch):
    from skcapstone.card_store import CardStore, parity_check
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "dual")
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="bmp_a", title="a", created_by="o"))
    board.create_task(Task(id="bmp_b", title="b", created_by="o"))
    board.claim_task("opus", "bmp_a")  # a is current -> doing
    board.claim_task("opus", "bmp_b")  # b current, a bumped -> claimed/ready
    store = CardStore(tmp_path)
    assert store.fold("bmp_a").status.value == "ready"  # demoted
    assert store.fold("bmp_b").status.value == "doing"
    # and parity with legacy holds
    par = parity_check(tmp_path)
    assert par["mismatches"] == [], par["mismatches"]


# ---------------------------------------------------------------------------
# Fold drift (card ba4af853): the fold must consume sanctioned LEGACY paths
# (archive/<host>.jsonl + card_events overlay), not just the store's own logs.
# ---------------------------------------------------------------------------


def test_legacy_archive_index_folds_to_archived(tmp_path):
    """Card 038005cd scenario: archived via Board.archive_task with the mirror
    off (flag unset, e.g. a cron sweep without the env var). The fold must
    still see it as archived, so it drops out of the open board."""
    from skcapstone.card_store import CardStore, import_from_legacy
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="038005cd", title="done long ago", created_by="o"))
    import_from_legacy(tmp_path)
    # Legacy-only archive: no SKCOORD_CARD_STORE in env -> no store mirror.
    board.archive_task("038005cd", by="archive-done")
    store = CardStore(tmp_path)
    card = store.fold("038005cd")
    assert card.archived is True
    assert "038005cd" not in {c.id for c in store.list_cards()}
    assert "038005cd" in {c.id for c in store.list_cards(include_archived=True)}


def test_overlay_move_on_preexisting_card_folds_to_done(tmp_path):
    """Card 07c78c7f scenario: moved to done via the card_events overlay
    (coord move on a non-mirroring writer). Fold must serve done, not backlog."""
    from skcapstone.card import CardEvent, CardEventLog, Column
    from skcapstone.card_store import CardStore, import_from_legacy
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="07c78c7f", title="completed via overlay", created_by="o"))
    import_from_legacy(tmp_path)
    CardEventLog(tmp_path).append(CardEvent(card_id="07c78c7f", action="move", column="done"))
    assert CardStore(tmp_path).fold("07c78c7f").status == Column.DONE


def test_overlay_priority_label_owner_fold_on_preexisting_card(tmp_path):
    """CardEventLog set_priority/add_label/assign on a pre-existing card must
    fold into the store-served state."""
    from skcapstone.card import CardEvent, CardEventLog
    from skcapstone.card_store import CardStore, import_from_legacy
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="ov1", title="overlay mutations", created_by="o"))
    import_from_legacy(tmp_path)
    log = CardEventLog(tmp_path)
    log.append(CardEvent(card_id="ov1", action="set_priority", priority="high"))
    log.append(CardEvent(card_id="ov1", action="add_label", label="urgent"))
    log.append(CardEvent(card_id="ov1", action="assign", owner="lumina"))
    card = CardStore(tmp_path).fold("ov1")
    assert card.priority == "high"
    assert "urgent" in card.labels
    assert card.owner == "lumina"


def test_overlay_review_move_folds_status_and_owner(tmp_path):
    """Card 0f9d3aca scenario: overlay move to review + owner assign on a
    pre-existing card. Store must serve review/lumina, not backlog/None."""
    from skcapstone.card import CardEvent, CardEventLog, Column
    from skcapstone.card_store import CardStore, import_from_legacy, parity_check
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="0f9d3aca", title="in review", created_by="o"))
    import_from_legacy(tmp_path)
    log = CardEventLog(tmp_path)
    log.append(CardEvent(card_id="0f9d3aca", action="move", column="review"))
    log.append(CardEvent(card_id="0f9d3aca", action="assign", owner="lumina"))
    card = CardStore(tmp_path).fold("0f9d3aca")
    assert card.status == Column.REVIEW
    assert card.owner == "lumina"
    # both projections consume the overlay -> no parity mismatch
    par = parity_check(tmp_path)
    assert par["mismatches"] == [], par["mismatches"]


def test_store_event_after_overlay_wins_by_timestamp(tmp_path):
    """Append-only merge: a later store event must win over an earlier overlay
    event (and vice versa), keyed by (ts, writer, seq)."""
    from skcapstone.card import CardEvent, CardEventLog, Column
    from skcapstone.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    store.create(CardCore(id="mix1", title="merge order"))
    CardEventLog(tmp_path).append(
        CardEvent(card_id="mix1", action="move", column="doing", ts="2026-07-01T00:00:00+00:00")
    )
    store.append_event("mix1", "move", "opus", column="review")  # ts=now, later
    assert store.fold("mix1").status == Column.REVIEW


def test_open_count_no_longer_overcounts_archived(tmp_path):
    """The reported bug: store-served open count must exclude cards archived
    via the legacy archive index."""
    from skcapstone.card_store import import_from_legacy, task_views_from_store
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    for i in range(6):
        board.create_task(Task(id=f"oc{i}", title=f"t{i}", created_by="o"))
    import_from_legacy(tmp_path)
    for i in range(4):  # archive 4 of 6 legacy-side only (no mirror env)
        board.archive_task(f"oc{i}", by="age-backlog")
    views = task_views_from_store(tmp_path)
    open_ids = {v.task.id for v in views if v.status.value == "open"}
    assert open_ids == {"oc4", "oc5"}


def _stamp_legacy_status(home, status="open"):
    """Give legacy task records an explicit ``status`` field.

    skcoord 0.1.46 changed parity semantics (skcoord card 9ccc42ec, commit
    eba11a9 "fix(parity): ignore absent legacy lifecycle fields"): a legacy task
    whose record does not physically carry ``status`` is now UNKNOWN rather than
    a disagreement, so status-less immutable birth records no longer inflate the
    parity open-count alert or make complete CardStore chains look stale.

    Board.create_task writes exactly such a status-less birth record, and there
    is no public setter for the field, so a fixture that wants to exercise parity
    drift has to stamp it. Without this the fixtures below assert against records
    the new contract deliberately declines to count, and they measure nothing.
    """
    import json

    from skcapstone.coordination import Board

    for path in Board(home).tasks_dir.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = status
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def test_parity_open_count_alert(tmp_path, monkeypatch):
    """PARITY ALERT: parity_check must flag when the store-served open count
    diverges from legacy beyond the threshold."""
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")  # legacy-only drift fixture
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="al0", title="seed", created_by="o"))
    import_from_legacy(tmp_path)
    # 4 legacy-only tasks the store has never seen -> store undercounts open by 4
    for i in range(1, 5):
        board.create_task(Task(id=f"al{i}", title=f"drift{i}", created_by="o"))
    # parity only counts legacy records that physically carry status
    _stamp_legacy_status(tmp_path, "open")
    par = parity_check(tmp_path, open_drift_threshold=2)
    assert par["open_legacy"] == 5
    assert par["open_store"] == 1
    assert par["open_drift"] == 4
    assert par["open_alert"] is True
    # within threshold -> no alert
    par2 = parity_check(tmp_path, open_drift_threshold=10)
    assert par2["open_alert"] is False


def test_parity_no_alert_when_in_sync(tmp_path):
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="ns1", title="x", created_by="o"))
    import_from_legacy(tmp_path)
    par = parity_check(tmp_path)
    assert par["open_alert"] is False
    assert par["open_drift"] == 0


def test_import_from_legacy_forces_legacy_read_at_flag_1(tmp_path, monkeypatch):
    """Post-cutover migrate must import from the LEGACY projection, not read
    the store back to itself (which would hide missing cards)."""
    from skcapstone.card_store import CardStore, import_from_legacy
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")  # legacy-only fixture
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="mg1", title="legacy only", created_by="o"))
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    res = import_from_legacy(tmp_path)
    assert res["imported"] == 1
    assert CardStore(tmp_path).fold("mg1") is not None


def test_reconcile_repairs_agent_file_claim_drift(tmp_path):
    """One-time reconcile: a claim/complete recorded only in agents/*.json
    (pre-mirror) is repaired by appending corrective store events."""
    from skcapstone.card_store import (
        CardStore,
        import_from_legacy,
        parity_check,
        reconcile_from_legacy,
    )
    from skcapstone.coordination import AgentFile, Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="rc1", title="claimed post-import", created_by="o"))
    board.create_task(Task(id="rc2", title="done post-import", created_by="o"))
    import_from_legacy(tmp_path)
    # legacy-only mutations: agent file claims/completes, never mirrored
    board.save_agent(
        AgentFile(
            agent="lumina", current_task="rc1", claimed_tasks=["rc1"], completed_tasks=["rc2"]
        )
    )
    # parity only counts legacy records that physically carry status
    _stamp_legacy_status(tmp_path, "open")
    pre = parity_check(tmp_path)
    assert pre["mismatches"], "fixture must drift before reconcile"

    dry = reconcile_from_legacy(tmp_path, dry_run=True)
    assert dry["would_fix"] >= 2
    assert parity_check(tmp_path)["mismatches"], "dry-run must not write"

    res = reconcile_from_legacy(tmp_path, dry_run=False)
    assert res["fixed"] >= 2
    store = CardStore(tmp_path)
    assert store.fold("rc1").owner == "lumina"
    assert store.fold("rc1").status.value == "doing"
    assert store.fold("rc2").status.value == "done"
    post = parity_check(tmp_path)
    assert post["mismatches"] == [], post["mismatches"]


# --- Phase 4e: default-ON flip (store is the default, `=0` is the escape hatch) ---


def test_write_enabled_by_default_when_unset(monkeypatch):
    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    from skcapstone.card_store import card_store_read_enabled, card_store_write_enabled

    assert card_store_write_enabled() is True
    assert card_store_read_enabled() is True


def test_disabled_with_explicit_zero(monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    from skcapstone.card_store import card_store_read_enabled, card_store_write_enabled

    assert card_store_write_enabled() is False
    assert card_store_read_enabled() is False


def test_dual_mode_writes_but_reads_legacy(monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "dual")
    from skcapstone.card_store import card_store_read_enabled, card_store_write_enabled

    assert card_store_write_enabled() is True
    assert card_store_read_enabled() is False


def test_default_on_write_mirrors_without_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    from skcapstone.card_store import CardStore
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="do1", title="mirrored by default", created_by="opus"))
    assert CardStore(tmp_path).fold("do1") is not None


def test_default_on_read_serves_from_store(tmp_path, monkeypatch):
    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    from skcapstone.card import KanbanBoard
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="dr1", title="served from store", created_by="opus"))
    cards = KanbanBoard(tmp_path).cards()
    assert any(c.id == "dr1" and c.source == "cards" for c in cards)


def test_empty_store_falls_back_to_legacy_catastrophe_guard(tmp_path, monkeypatch):
    # Default-ON read must NOT silently empty the board if the store is behind.
    # Create a legacy-only task (writes off), then read with the store enabled:
    # the guard falls back to the legacy projection rather than returning [].
    from skcapstone.coordination import Board, Task

    board = Board(tmp_path)
    board.ensure_dirs()
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board.create_task(Task(id="cg1", title="legacy only", created_by="o"))
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    views = board.get_task_views()
    assert any(v.task.id == "cg1" for v in views), "guard must fall back to legacy"


def test_parity_still_reads_legacy_under_default_on(tmp_path, monkeypatch):
    # The force-legacy shim must survive the default-ON flip: a legacy-only task
    # must still register as store-missing drift (not be masked by a store read).
    from skcapstone.card_store import import_from_legacy, parity_check
    from skcapstone.coordination import Board, Task

    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="pd1", title="x", created_by="o"))
    import_from_legacy(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")  # create legacy-only (no mirror)
    board.create_task(Task(id="pd2", title="legacy only", created_by="o"))
    monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)  # default-ON
    par = parity_check(tmp_path)
    assert "pd2" in par["missing"], par


# --- Phase 4e-retire safety net: store -> legacy exporter -------------------


def test_export_to_legacy_round_trips_status(tmp_path):
    """A store card of each status reconstructs the same legacy status+owner."""
    from skcapstone.card_store import _forced_legacy_read, export_to_legacy
    from skcapstone.coordination import Board, TaskStatus

    store = CardStore(tmp_path)
    store.create(CardCore(id="t_open", title="open task", created_by="opus"))
    store.create(CardCore(id="t_claim", title="claimed task", created_by="opus"))
    store.append_event("t_claim", "move", "opus", column="ready")
    store.append_event("t_claim", "assign", "opus", owner="lumina")
    store.create(CardCore(id="t_prog", title="prog task", created_by="opus"))
    store.append_event("t_prog", "claim", "jarvis", owner="jarvis")  # -> doing
    store.create(CardCore(id="t_done", title="done task", created_by="opus"))
    store.append_event("t_done", "claim", "lumina", owner="lumina")
    store.append_event("t_done", "complete", "lumina")

    res = export_to_legacy(tmp_path)
    assert res["cards"] == 4
    assert res["tasks_written"] == 4  # no pre-existing legacy files

    with _forced_legacy_read():
        views = {v.task.id: v for v in Board(tmp_path).get_task_views()}

    assert views["t_open"].status == TaskStatus.OPEN
    assert views["t_open"].claimed_by is None
    assert views["t_claim"].status == TaskStatus.CLAIMED
    assert views["t_claim"].claimed_by == "lumina"
    assert views["t_prog"].status == TaskStatus.IN_PROGRESS
    assert views["t_prog"].claimed_by == "jarvis"
    assert views["t_done"].status == TaskStatus.DONE


def test_export_to_legacy_is_idempotent(tmp_path):
    """A second export writes no new task files and yields the same board."""
    from skcapstone.card_store import export_to_legacy

    store = CardStore(tmp_path)
    store.create(CardCore(id="a1", title="one", created_by="opus"))
    store.append_event("a1", "claim", "lumina", owner="lumina")

    first = export_to_legacy(tmp_path)
    assert first["tasks_written"] == 1
    second = export_to_legacy(tmp_path)
    assert second["tasks_written"] == 0  # already on disk, immutable
    assert second["cards"] == 1


def test_export_to_legacy_preserves_existing_task_files(tmp_path):
    """Existing immutable task files (with notes) are never overwritten."""
    from skcapstone.card_store import export_to_legacy
    from skcapstone.coordination import Board, Task, TaskPriority

    board = Board(tmp_path)
    board.ensure_dirs()
    task = Task(
        id="keep1",
        title="rich legacy task",
        description="d",
        priority=TaskPriority.HIGH,
        notes=["a note the store never modelled"],
    )
    path = board.create_task(task)  # writes legacy file + mirrors to store

    export_to_legacy(tmp_path)

    import json as _json

    on_disk = _json.loads(path.read_text())
    assert on_disk["notes"] == ["a note the store never modelled"]


def test_export_to_legacy_ownerless_done_survives(tmp_path):
    """A done card with no owner still round-trips as DONE via a synthetic holder."""
    from skcapstone.card_store import _forced_legacy_read, export_to_legacy
    from skcapstone.coordination import Board, TaskStatus

    store = CardStore(tmp_path)
    store.create(CardCore(id="orphan_done", title="ownerless done", created_by="x"))
    store.append_event("orphan_done", "move", "x", column="done")  # done, no owner

    export_to_legacy(tmp_path)
    with _forced_legacy_read():
        views = {v.task.id: v for v in Board(tmp_path).get_task_views()}
    assert views["orphan_done"].status == TaskStatus.DONE
