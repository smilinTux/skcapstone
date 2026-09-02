"""Card 2516480b: dedupe unchanged findings; re-emit on generation or SLA.

AC3 of the census: an unchanged board emits nothing new, a new authoritative
generation re-emits, a missed recommendation SLA re-emits, and a finding
within its SLA on an unchanged board stays suppressed.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc

from tests.census_support import NOW, _add, _census, _recs


def _board(tmp_path: Path) -> CardStore:
    home = tmp_path / "skcapstone"
    (home / "coordination").mkdir(parents=True)
    (home / "cards").mkdir()
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    from tests.census_support import build_board

    build_board(store)
    return store


# ---------------------------------------------------------------------------
# AC3: dedupe unchanged findings; re-emit on new generation or missed SLA.
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_unchanged_board_emits_nothing_new(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        census = _census(board.home)
        first = census.run()
        census.emit(first)
        second = census.run()
        assert second.findings == []
        assert second.suppressed_unchanged == len(first.findings)
        census.emit(second)
        assert len(_recs(board, "bbbb0002")) == len(first.findings)

    def test_new_generation_reemits(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        census = _census(board.home)
        first = census.run()
        census.emit(first)
        # authoritative state changes: a new claim revision
        _add(
            board,
            "bbbb0002",
            "claim",
            writer="worker-b",
            owner="worker-b",
            claim_revision="rev-bbbb-2",
            transition_id="t-bbbb-claim-2",
        )
        second = census.run()
        assert second.findings, "changed generation must re-emit"
        assert second.suppressed_unchanged < len(first.findings) + len(second.findings)
        census.emit(second)
        rows = _recs(board, "bbbb0002")
        ids_first = {f["recommendation_id"] for f in first.findings}
        ids_second = {f["recommendation_id"] for f in second.findings}
        assert ids_first & ids_second <= {r["recommendation_id"] for r in rows}

    def test_missed_sla_reemits(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        census = _census(board.home)
        first = census.run()
        census.emit(first)
        later = mc.MeroBlockerCensus(
            board.home,
            now=lambda: NOW + timedelta(hours=49),
            process_reader=lambda cid: {"host": "chiap03", "sessions": ["alive"]},
            identity_reader=lambda cid: True,
        )
        second = later.run()
        assert second.findings, "missed recommendation SLA must re-emit"
        assert any(
            f["details"].get("sla_state") == "missed"
            or f["finding_type"] == mc.CensusFindingType.STALE_CLAIM.value
            for f in second.findings
        )

    def test_within_sla_and_unchanged_is_suppressed(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        census = _census(board.home)
        first = census.run()
        census.emit(first)
        slightly_later = mc.MeroBlockerCensus(
            board.home,
            now=lambda: NOW + timedelta(hours=2),
            process_reader=lambda cid: {"host": "chiap03", "sessions": ["alive"]},
            identity_reader=lambda cid: True,
        )
        second = slightly_later.run()
        assert second.findings == []
        assert second.suppressed_unchanged == len(first.findings)
