"""Card 2516480b census classes, part 2: the remaining finding classes.

Continues ``test_mero_census.py`` (AC1), covering contradictory verdicts,
superseded live cards, review identity gaps, selector-ready counts, and the
two census bounds. The board fixtures build CardStore JSON through the real
store serializer; nothing is ever concatenated into JSON.
"""

from __future__ import annotations

from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc
from tests.census_support import _add, _census, _home, build_board

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
    )
    report = _census(home).run()
    gaps = [
        f
        for f in report.findings
        if f["finding_type"] == mc.CensusFindingType.REVIEW_IDENTITY_GAP.value
    ]
    assert gaps
    assert any(g["defect"] == "reviewer_not_distinct" for g in gaps[0]["details"]["gaps"])


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
