"""Card 2516480b: BLOCKED-contract referent taxonomy and the SKMail join.

Pins the malformed-referent taxonomy of the BLOCKED verdict contract
(what must be flagged, what must not) and proves SKMail signals are joined
as evidence but never mint a finding on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc

from tests.census_support import NOW, _add, _census, _fixed_now, _home


def _board(tmp_path: Path) -> CardStore:
    """Build the shared fixture board with the census clock pinned to NOW.

    Card 201cd059: the board events are stamped at NOW, so any census built
    on this home must observe at NOW too. An unplugged census defaults to
    wall-clock time, which drifts past the 24h stale-claim SLA and mints an
    unintended stale_claim finding that no test in this module asserts.
    """
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    from tests.census_support import build_board

    build_board(store)
    return store


# ---------------------------------------------------------------------------
# Fixtures from the BLOCKED contract: malformed referent taxonomy.
# ---------------------------------------------------------------------------


class TestReferentTaxonomy:
    @pytest.mark.parametrize(
        "text,defect",
        [
            ("BLOCKED", "missing_or_unknown_blocked_on_value"),
            ("BLOCKED. blocked_on: unknown", "missing_or_unknown_blocked_on_value"),
            ("BLOCKED. blocked_on: human", "missing_referent"),
            (
                "BLOCKED blocked_on=dependency referent=notacard",
                "dependency_referent_not_a_card_id",
            ),
            ("BLOCKED blocked_on=capability referent=maybe", "capability_referent_not_ac_or_free"),
            ("BLOCKED blocked_on=card referent=card:abcd1234", "card_referent_not_ac"),
        ],
    )
    def test_malformed_referents_are_detected(self, tmp_path, text, defect) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(CardCore(id="aaaa0001", title="t", created_by="jarvis"))
        _add(store, "aaaa0001", "verdict", writer="w", verdict=text)
        report = _census(home).run()
        malformed = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.MALFORMED_BLOCKER_REFERENT.value
        ]
        assert malformed and all(d["details"]["defect"] == defect for d in malformed)

    @pytest.mark.parametrize(
        "text",
        [
            "BLOCKED. blocked_on: dependency referent=card:bbbb00022222",
            "BLOCKED. blocked_on: human referent=approval:credentials-rotation",
            "BLOCKED. blocked_on: capability referent=ac:3",
            "BLOCKED. blocked_on: capability referent=free",
            "BLOCKED. blocked_on: card referent=ac:1",
            '{"blocked_on": {"value": "dependency", "referent": "card:bbbb00022222"}}',
        ],
    )
    def test_wellformed_referents_are_not_flagged(self, tmp_path, text) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        target = store.create(CardCore(id="bbbb0002", title="dep", created_by="jarvis"))
        _add(store, target, "verdict", writer="w", verdict=text)
        report = _census(home).run()
        flagged = [
            f
            for f in report.findings
            if f["card_id"] == target
            and f["finding_type"] == mc.CensusFindingType.MALFORMED_BLOCKER_REFERENT.value
        ]
        assert flagged == []


class TestUnresolvableDependencyReferent:
    def test_dependency_referent_to_missing_card_is_flagged(self, tmp_path) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(CardCore(id="aaaa0001", title="t", created_by="jarvis"))
        _add(
            store,
            "aaaa0001",
            "verdict",
            writer="w",
            verdict="BLOCKED. blocked_on: dependency referent=card:ffff0009",
        )
        report = _census(home).run()
        malformed = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.MALFORMED_BLOCKER_REFERENT.value
        ]
        assert any(f["details"]["defect"] == "dependency_referent_unresolvable" for f in malformed)


class TestSkmailJoin:
    def test_skmail_signals_are_joined_not_decisive(self, tmp_path: Path) -> None:
        """Mail rows join into source events but never mint a finding alone."""
        board = _board(tmp_path)
        signals = {"bbbb0002": [{"from": "jarvis", "re": "stuck?", "ts": NOW.isoformat()}]}
        census = mc.MeroBlockerCensus(
            board.home,
            now=_fixed_now(),
            process_reader=lambda cid: {},
            skmail_reader=lambda cid: signals.get(cid, []),
        )
        report = census.run()
        assert report.findings, "board findings still surface"
        types = {f["finding_type"] for f in report.findings}
        # a mail signal alone created no finding type that the board does not
        mail_only = types - {
            mc.CensusFindingType.COMPLETED_DEPENDENCY.value,
            mc.CensusFindingType.MALFORMED_BLOCKER_REFERENT.value,
        }
        assert mail_only == set()
