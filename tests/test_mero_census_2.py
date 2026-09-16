"""Card 2516480b census classes, part 2: the remaining finding classes.

Continues ``test_mero_census.py`` (AC1), covering contradictory verdicts,
superseded live cards, review identity gaps, selector-ready counts, and the
two census bounds, plus card eacedb1a's durable pagination continuation
(AC: a population above the card cap is covered across bounded cycles and the
findings cap continues durably). The board fixtures build CardStore JSON
through the real store serializer; nothing is ever concatenated into JSON.
"""

from __future__ import annotations

from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc
from tests.census_support import _add, _census, _home, _recs, build_board

# ---------------------------------------------------------------------------
# AC1 (continued): the bounded census finds each required class.
# ---------------------------------------------------------------------------


def test_contradictory_verdicts_block_after_completed_pass(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="ffff0006", title="flip", created_by="jarvis"))
    _add(store, "ffff0006", "verdict", writer="w", verdict="PASS; all green")
    _add(store, "ffff0006", "verdict", writer="w", verdict="BLOCKED. blocked_on=human")
    report = _census(home).run()
    contra = [
        f
        for f in report.findings
        if f["finding_type"] == mc.CensusFindingType.CONTRADICTORY_VERDICTS.value
    ]
    assert contra and contra[0]["risk_class"] == "high"


def test_superseded_live_card(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="11110007", title="successor", created_by="jarvis"))
    _add(store, "11110007", "move", column="done", order=0)
    store.create(CardCore(id="22220008", title="old", created_by="jarvis"))
    _add(store, "22220008", "void", reason="Superseded by 11110007")
    # old is itself terminal; build a third live card superseded by done
    store.create(CardCore(id="33330009", title="live-but-superseded", created_by="jarvis"))
    _add(store, "33330009", "link", writer="jarvis", link_key="successor", link_value="11110007")
    report = _census(home).run()
    sup = [
        f
        for f in report.findings
        if f["finding_type"] == mc.CensusFindingType.SUPERSEDED_LIVE_CARD.value
        and f["card_id"] == "33330009"
    ]
    assert sup and sup[0]["details"]["successors"] == ["11110007"]


def test_review_identity_gap_recommender_not_link(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(
        CardCore(id="44440010", title="review me", created_by="jarvis", initial_labels=["review"])
    )
    _add(
        store,
        "44440010",
        "review_assignment_recommendation",
        writer="jarvis",
        reviewer="someone-else",
        recommendation_id="recx-1",
    )
    report = _census(home).run()
    gaps = [
        f
        for f in report.findings
        if f["finding_type"] == mc.CensusFindingType.REVIEW_IDENTITY_GAP.value
    ]
    assert gaps
    assert any(g["defect"] == "recommender_not_link" for g in gaps[0]["details"]["gaps"])


def test_review_identity_gap_reviewer_not_distinct(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(
        CardCore(
            id="55550011", title="review me 2", created_by="jarvis", initial_labels=["review"]
        )
    )
    _add(
        store,
        "55550011",
        "claim",
        writer="worker-x",
        owner="worker-x",
        claim_revision="rev-x",
        transition_id="t-x",
    )
    _add(
        store,
        "55550011",
        "review_assignment_recommendation",
        writer="link",
        reviewer="worker-x",
        recommendation_id="recx-2",
        author="worker-x",
    )
    report = _census(home).run()
    gaps = [
        f
        for f in report.findings
        if f["finding_type"] == mc.CensusFindingType.REVIEW_IDENTITY_GAP.value
    ]
    assert gaps
    assert any(g["defect"] == "reviewer_not_distinct" for g in gaps[0]["details"]["gaps"])


def test_seraph_launch_identity_receipt_is_consistent(tmp_path: Path) -> None:
    """The exact 206dc07d shape is valid: Seraph both claims and launches."""
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(
        CardCore(id="206dc07d", title="review", created_by="jarvis", initial_labels=["review"])
    )
    reviewer = "pi-seraph-chiap08-206dc07d"
    _add(
        store,
        "206dc07d",
        "review_assignment_recommendation",
        writer="link",
        reviewer=reviewer,
        author="jarvis",
        recommendation_id="rec-206dc07d",
    )
    _add(
        store,
        "206dc07d",
        "claim",
        writer=reviewer,
        owner=reviewer,
        claim_revision="claim-206dc07d",
        transition_id="claim-transition",
    )
    _add(
        store,
        "206dc07d",
        "review_assignment_launch",
        writer=reviewer,
        reviewer=reviewer,
        recommendation_id="rec-206dc07d",
        claim_revision="claim-206dc07d",
    )
    report = _census(home).run()
    assert not any(
        finding["finding_type"] == mc.CensusFindingType.REVIEW_IDENTITY_GAP.value
        for finding in report.findings
    )


def test_launch_receipt_must_match_exact_claim_owner(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(
        CardCore(id="66660012", title="review", created_by="jarvis", initial_labels=["review"])
    )
    _add(
        store,
        "66660012",
        "claim",
        writer="seraph-a",
        owner="seraph-a",
        claim_revision="claim-a",
        transition_id="claim-a",
    )
    _add(
        store,
        "66660012",
        "review_assignment_recommendation",
        writer="link",
        reviewer="seraph-b",
        author="producer",
        recommendation_id="rec-b",
    )
    _add(
        store,
        "66660012",
        "review_assignment_launch",
        writer="seraph-b",
        reviewer="seraph-b",
        recommendation_id="rec-b",
        claim_revision="claim-a",
    )
    gaps = [
        finding
        for finding in _census(home).run().findings
        if finding["finding_type"] == mc.CensusFindingType.REVIEW_IDENTITY_GAP.value
    ]
    assert any(gap["defect"] == "launch_identity_mismatch" for gap in gaps[0]["details"]["gaps"])


def test_selector_ready_counts(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    from tests.census_support import build_board

    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    build_board(store)
    report = _census(home).run()
    assert report.selector_ready["total_open"] == 1  # only bbbb0002 is open
    assert report.selector_ready["blocked"] == 1
    assert report.selector_ready["ready"] == 0


def test_bounded_cards_examined(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    build_board(store)
    report = _census(home, max_cards=1).run()
    assert report.cards_examined == 1
    assert report.cards_total == 3
    assert report.truncated is True


def test_bounded_findings_per_run(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    # Two live cards, each with a well-formed completed dependency.
    for i in range(2):
        dep_id = f"aa00000{i}"
        store.create(CardCore(id=dep_id, title=f"dep{i}", created_by="jarvis"))
        _add(store, dep_id, "move", column="done", order=0)
        cid = f"bb00000{i}"
        store.create(CardCore(id=cid, title=f"c{i}", created_by="jarvis", dependencies=[dep_id]))
    report = _census(home, max_findings=1).run()
    assert len(report.findings) <= 1
    assert report.suppressed_by_bound >= 1


# ---------------------------------------------------------------------------
# Card eacedb1a: deterministic durable pagination across bounded cycles.
# ---------------------------------------------------------------------------


def _block_card(store: CardStore, cid: str) -> None:
    """Give one live card a contradictory-verdict finding (deterministic)."""
    store.create(CardCore(id=cid, title=f"c-{cid}", created_by="jarvis"))
    _add(store, cid, "verdict", writer="w", verdict="PASS; all green")
    _add(store, cid, "verdict", writer="w", verdict="BLOCKED. blocked_on=human")


def test_pagination_reaches_tail_cards_across_bounded_cycles(tmp_path: Path) -> None:
    """No permanent tail: the card beyond the first window is examined later.

    Six cards, a two-card window, and a blocker only on the last sorted id.
    Each run constructs a fresh census, so coverage must be durable on disk:
    the tail finding appears exactly on the third cycle, never earlier.
    """
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(6):
        store.create(CardCore(id=f"aa00000{i}", title=f"c{i}", created_by="jarvis"))
    _block_card(store, "aa000005")
    seen_at: int | None = None
    for cycle in range(1, 5):
        report = _census(home, max_cards=2).run()
        assert report.cards_examined <= 2
        if any(f["card_id"] == "aa000005" for f in report.findings):
            seen_at = cycle if seen_at is None else seen_at
    assert seen_at == 3


def test_pagination_reports_deterministic_coverage(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(5):
        store.create(CardCore(id=f"bb00000{i}", title=f"c{i}", created_by="jarvis"))
    reports = [_census(home, max_cards=2).run() for _ in range(4)]
    coverage = [r.coverage for r in reports]
    assert [c["window_start"] for c in coverage] == [0, 2, 4, 0]
    assert [c["pass_complete"] for c in coverage] == [False, False, True, False]
    assert [c["covered_in_pass"] for c in coverage] == [2, 4, 0, 2]
    assert coverage[2]["cycle"] == 1
    assert coverage[3]["cycle"] == 1  # the new pass is still pass one
    assert all(c["cards_total"] == 5 for c in coverage)


def test_pagination_recovers_from_corrupt_state(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(2):
        store.create(CardCore(id=f"cc00000{i}", title="c", created_by="jarvis"))
    state = home / "mero_census" / "pagination.json"
    state.parent.mkdir(parents=True)
    state.write_text("{not json", encoding="utf-8")
    report = _census(home, max_cards=1).run()
    assert report.cards_examined == 1
    assert report.coverage["position"] == 1


def test_pagination_never_folds_outside_the_window(tmp_path: Path, monkeypatch) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(7):
        store.create(CardCore(id=f"dd00000{i}", title="c", created_by="jarvis"))
    calls = {"n": 0}
    real_fold = CardStore.fold

    def counting_fold(self, cid):
        calls["n"] += 1
        return real_fold(self, cid)

    monkeypatch.setattr(CardStore, "fold", counting_fold)
    for _ in range(4):
        calls["n"] = 0
        _census(home, max_cards=3).run()
        assert calls["n"] <= 3


def test_full_pass_examines_every_card_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """One full pass over N cards costs ceil(N/window) runs and zero extra folds.

    This is the small-scale proof of AC1's invariant (the window arithmetic
    is pure indexing over the sorted id list, so it holds at any population,
    including above the default 4000 cap): the runs' windows tile the id
    list exactly once, and no run ever folds a card outside its window.
    """
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(7):
        store.create(CardCore(id=f"dd00000{i}", title="c", created_by="jarvis"))
    calls = {"n": 0}
    real_fold = CardStore.fold

    def counting_fold(self, cid):
        calls["n"] += 1
        return real_fold(self, cid)

    monkeypatch.setattr(CardStore, "fold", counting_fold)
    reports = []
    for _ in range(3):
        calls["n"] = 0
        reports.append(_census(home, max_cards=3).run())
        assert calls["n"] == reports[-1].cards_examined
    assert [(r.coverage["window_start"], r.coverage["window_end"]) for r in reports] == [
        (0, 3),
        (3, 6),
        (6, 7),
    ]
    assert sum(r.cards_examined for r in reports) == 7
    assert reports[-1].coverage["pass_complete"] is True
    assert reports[-1].coverage["cycle"] == 1


def test_pagination_emission_stays_idempotent_across_cycles(tmp_path: Path) -> None:
    home = _home(tmp_path)
    store = CardStore(home)
    _block_card(store, "ee000001")
    first = _census(home, max_cards=1).run()
    _census(home, max_cards=1).emit(first)
    second = _census(home, max_cards=1).run()
    assert second.findings == []
    assert second.suppressed_unchanged == len(first.findings)
    _census(home, max_cards=1).emit(second)
    rows = _recs(store, "ee000001")
    assert len(rows) == len(first.findings)
    assert len({row["recommendation_id"] for row in rows}) == len(rows)


def test_findings_cap_continues_durably_across_cycles(tmp_path: Path) -> None:
    """The 200-cap defers, never drops: unemitted findings resurface next cycle."""
    home = _home(tmp_path)
    store = CardStore(home)
    for i in range(5):
        dep_id = f"0d00000{i}"
        store.create(CardCore(id=dep_id, title=f"dep{i}", created_by="jarvis"))
        _add(store, dep_id, "move", column="done", order=0)
        store.create(
            CardCore(
                id=f"0c00000{i}",
                title=f"c{i}",
                created_by="jarvis",
                dependencies=[dep_id],
            )
        )
    emitted = 0
    for _ in range(4):
        report = _census(home, max_findings=2).run()
        assert len(report.findings) <= 2
        _census(home, max_findings=2).emit(report)
        emitted += len(report.findings)
    assert emitted == 5
    distinct = {row["recommendation_id"] for i in range(5) for row in _recs(store, f"0c00000{i}")}
    assert len(distinct) == 5
