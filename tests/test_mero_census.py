"""Card 2516480b census classes, part 1: fixture and four finding classes.

AC1 coverage continued in ``test_mero_census_2.py``. The board fixture builds
CardStore JSON through the real store serializer and reads every line back
through ``json.loads``; nothing is ever concatenated.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc

from tests.census_support import NOW, _add, _census, _home, build_board

# ---------------------------------------------------------------------------
# Board fixture built through the real serializer.
# ---------------------------------------------------------------------------


@pytest.fixture()
def board(tmp_path: Path) -> CardStore:
    """A small board: one done dep, one voided card, one stuck worker card."""
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    build_board(store)
    return store


# ---------------------------------------------------------------------------
# AC1: the bounded census finds each required class.
# ---------------------------------------------------------------------------


class TestCensusClasses:
    def test_completed_dependency_generation(self, board: CardStore) -> None:
        report = _census(board.home).run()
        types = {f["finding_type"] for f in report.findings}
        assert mc.CensusFindingType.COMPLETED_DEPENDENCY.value in types

    def test_malformed_blocker_referent(self, board: CardStore) -> None:
        # The fixture card is blocked on "ac:2" for card 4 hex of its own id,
        # a shape the contract rejects only if malformed; use a truly
        # malformed spelling here to pin the detector.
        _add(
            board,
            "bbbb0002",
            "verdict",
            writer="worker-a",
            verdict="BLOCKED",
            block_reason="no referent at all",
        )
        report = _census(board.home).run()
        malformed = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.MALFORMED_BLOCKER_REFERENT.value
        ]
        assert malformed, "a BLOCKED with no blocked_on must be flagged"
        assert any(
            f["details"]["defect"] == "missing_or_unknown_blocked_on_value" for f in malformed
        )

    def test_void_dependency_edge(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(CardCore(id="dddd0004", title="voided", created_by="jarvis"))
        _add(store, "dddd0004", "void", reason="Superseded by a replacement")
        store.create(
            CardCore(
                id="eeee0005", title="consumer", created_by="jarvis", dependencies=["dddd0004"]
            )
        )
        report = _census(home).run()
        types = {f["finding_type"] for f in report.findings}
        assert mc.CensusFindingType.VOID_DEPENDENCY_EDGE.value in types

    def test_stale_claim_after_sla(self, board: CardStore) -> None:
        # The fixture claim is pinned to NOW; run the census 30 hours later
        # with a live process read so the stale detector (not dead) runs.
        later = NOW + timedelta(hours=30)
        census = mc.MeroBlockerCensus(
            board.home,
            now=lambda: later,
            process_reader=lambda cid: {"host": "chiap03", "sessions": ["sess-1"]},
            identity_reader=lambda cid: True,
        )
        report = census.run()
        stale = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.STALE_CLAIM.value
        ]
        assert stale and stale[0]["details"]["sla_state"] in ("at_risk", "missed")

    def test_dead_claim_when_process_and_identity_gone(self, board: CardStore) -> None:
        census = mc.MeroBlockerCensus(
            board.home,
            now=lambda: NOW,
            process_reader=lambda cid: {"host": "chiap03", "sessions": []},
            identity_reader=lambda cid: False,
        )
        report = census.run()
        dead = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.DEAD_CLAIM.value
        ]
        assert dead and dead[0]["risk_class"] == "high"
        assert dead[0]["details"]["claim_revision"] == "rev-bbbb-1"
