"""Card 2516480b: the read-only recurring blocker census for Mero.

Positive tests pin the bounded census (dead and stale claims, completed
dependency generations, contradictory verdicts, malformed blocker referents,
void dependency edges, superseded live cards, review identity gaps, and
selector-ready counts), the typed recommendation envelope, and dedup +
generation re-emission. Negative tests prove Mero cannot claim, release,
launch, stop, create or mutate cards, merge, deploy, access credentials or
protected data, or rerun the selector.

All fixtures build CardStore JSON through the real store serializer and read
every line back through ``json.loads``; nothing is ever concatenated.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc
from skcapstone.seat_boundaries import (
    Action,
    BoundaryError,
    require_authority,
)

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now():
    return lambda: NOW


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "skcapstone"
    (home / "coordination").mkdir(parents=True)
    (home / "cards").mkdir()
    return home


def _add(store: CardStore, cid: str, action: str, **payload) -> dict:
    return store.append_event(cid, action, payload.pop("writer", "jarvis"), **payload)


def _recs(store: CardStore, cid: str) -> list[dict]:
    rows = [e for e in store._read_events(cid) if e.get("action") == mc.RECOMMENDATION_EVENT]
    return sorted(rows, key=lambda e: str(e.get("ts")))


def _census(home: Path, **kwargs) -> mc.MeroBlockerCensus:
    kwargs.setdefault("now", _fixed_now())
    return mc.MeroBlockerCensus(home, **kwargs)


# ---------------------------------------------------------------------------
# Board fixtures built through the real serializer.
# ---------------------------------------------------------------------------


@pytest.fixture()
def board(tmp_path: Path) -> CardStore:
    """A small board: one done dep, one voided card, one stuck worker card."""
    home = _home(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    _add(store, "aaaa0001", "move", column="done", order=0)

    store.create(
        CardCore(id="bbbb0002", title="stuck", created_by="jarvis", dependencies=["aaaa0001"])
    )
    _add(
        store,
        "bbbb0002",
        "claim",
        writer="worker-a",
        owner="worker-a",
        claim_revision="rev-bbbb-1",
        transition_id="t-bbbb-claim",
    )
    _add(
        store,
        "bbbb0002",
        "verdict",
        writer="worker-a",
        verdict="BLOCKED. blocked_on: card referent=ac:2",
        evidence_link="/tmp/e-bbbb.json",
        artifact_sha256="a" * 64,
    )

    store.create(CardCore(id="cccc0003", title="gate", created_by="jarvis"))
    _add(store, "cccc0003", "void", reason="Superseded by aaaa0001 which is COMPLETE")
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
        # Claim is at NOW in the fixture; run the census 30 hours later with
        # a dead process read so the stale detector (not dead) is exercised.
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
            now=_fixed_now(),
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

    def test_contradictory_verdicts_block_after_completed_pass(self, tmp_path: Path) -> None:
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

    def test_superseded_live_card(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(CardCore(id="11110007", title="successor", created_by="jarvis"))
        _add(store, "11110007", "move", column="done", order=0)
        store.create(CardCore(id="22220008", title="old", created_by="jarvis"))
        _add(store, "22220008", "void", reason="Superseded by 11110007")
        # old is itself terminal; build a third live card superseded by done
        store.create(CardCore(id="33330009", title="live-but-superseded", created_by="jarvis"))
        _add(
            store, "33330009", "link", writer="jarvis", link_key="successor", link_value="11110007"
        )
        report = _census(home).run()
        sup = [
            f
            for f in report.findings
            if f["finding_type"] == mc.CensusFindingType.SUPERSEDED_LIVE_CARD.value
            and f["card_id"] == "33330009"
        ]
        assert sup and sup[0]["details"]["successors"] == ["11110007"]

    def test_review_identity_gap_recommender_not_link(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(
            CardCore(
                id="44440010", title="review me", created_by="jarvis", initial_labels=["review"]
            )
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

    def test_review_identity_gap_reviewer_not_distinct(self, tmp_path: Path) -> None:
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

    def test_selector_ready_counts(self, board: CardStore) -> None:
        report = _census(board.home).run()
        assert report.selector_ready["total_open"] == 1  # only bbbb0002 is open
        assert report.selector_ready["blocked"] == 1
        assert report.selector_ready["ready"] == 0

    def test_bounded_cards_examined(self, board: CardStore) -> None:
        report = _census(board.home, max_cards=1).run()
        assert report.cards_examined == 1
        assert report.cards_total == 3
        assert report.truncated is True

    def test_bounded_findings_per_run(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        store = CardStore(home)
        # Two live cards, each with a well-formed completed dependency.
        for i in range(2):
            dep_id = f"aa00000{i}"
            store.create(CardCore(id=dep_id, title=f"dep{i}", created_by="jarvis"))
            _add(store, dep_id, "move", column="done", order=0)
            cid = f"bb00000{i}"
            store.create(
                CardCore(id=cid, title=f"c{i}", created_by="jarvis", dependencies=[dep_id])
            )
        report = _census(home, max_findings=1).run()
        assert len(report.findings) <= 1
        assert report.suppressed_by_bound >= 1


# ---------------------------------------------------------------------------
# AC2: the typed recommendation envelope.
# ---------------------------------------------------------------------------


class TestRecommendationEnvelope:
    def test_every_finding_pins_the_required_fields(self, board: CardStore) -> None:
        report = _census(board.home).run()
        assert report.findings
        for finding in report.findings:
            assert finding["card_id"]
            assert finding["card_revision"]
            assert finding["blocker_generation"]
            assert finding["generation"]
            assert finding["source_events"], "findings must cite source events"
            for ref in finding["source_events"]:
                assert set(ref) >= {"event_id", "ts", "action", "writer"}
            assert finding["evidence_sha256"], "findings must pin evidence hashes"
            for digest in finding["evidence_sha256"]:
                assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
            assert finding["risk_class"] in {r.value for r in mc.RiskClass}
            assert finding["proposed_consumer_action"]
            assert finding["stop_conditions"], "findings must pin stop conditions"
            assert finding["recommendation_id"].startswith("mrc-")

    def test_emitted_event_is_typed_and_complete(self, board: CardStore) -> None:
        census = _census(board.home)
        report = census.run()
        census.emit(report)
        rows = _recs(board, "bbbb0002")
        assert rows
        row = rows[0]
        assert row["schema"] == mc.RECOMMENDATION_SCHEMA
        assert row["writer"] == "mero"
        assert row["observed_by"] == "mero"
        assert row["finding_type"] in {t.value for t in mc.CensusFindingType}
        # the durable line round-trips through the serializer/parser pair
        line = mc.recommendation_event_to_json(row)
        assert mc.parse_recommendation_line(line) == row


# ---------------------------------------------------------------------------
# AC3: dedupe unchanged findings; re-emit on new generation or missed SLA.
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_unchanged_board_emits_nothing_new(self, board: CardStore) -> None:
        census = _census(board.home)
        first = census.run()
        census.emit(first)
        second = census.run()
        assert second.findings == []
        assert second.suppressed_unchanged == len(first.findings)
        census.emit(second)
        assert len(_recs(board, "bbbb0002")) == len(first.findings)

    def test_new_generation_reemits(self, board: CardStore) -> None:
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

    def test_missed_sla_reemits(self, board: CardStore) -> None:
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

    def test_within_sla_and_unchanged_is_suppressed(self, board: CardStore) -> None:
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


# ---------------------------------------------------------------------------
# AC4: negative tests. Mero cannot mutate anything.
# ---------------------------------------------------------------------------


class TestMeroCannotMutate:
    def _assert_refused(self, actor: str, action: Action) -> None:
        with pytest.raises(BoundaryError):
            require_authority(actor, action)

    def test_mero_cannot_claim_release_launch_stop(self) -> None:
        for action in (Action.CLAIM, Action.RELEASE, Action.LAUNCH, Action.STOP):
            self._assert_refused("mero", action)

    def test_mero_cannot_reassign_rotate_or_repair(self) -> None:
        for action in (Action.REASSIGN, Action.ROTATE, Action.REPAIR_WORKER):
            self._assert_refused("mero", action)

    def test_mero_cannot_merge_or_deploy(self) -> None:
        for action in (Action.MERGE, Action.DEPLOY, Action.EVALUATE_MERGE):
            self._assert_refused("mero", action)

    def test_mero_holds_exactly_observe_and_recommend(self) -> None:
        allowed = {Action.OBSERVE, Action.RECOMMEND}
        for action in Action:
            if action in allowed:
                require_authority("mero", action)
            else:
                with pytest.raises(BoundaryError):
                    require_authority("mero", action)

    def test_only_mero_may_emit_census_recommendations(self, board: CardStore) -> None:
        census = _census(board.home)
        report = census.run()
        for actor in ("jarvis", "link", "worker-a", "chef", "pi-mero-chiap03-2516480b"):
            with pytest.raises(BoundaryError):
                census.emit(report, actor=actor)
        census.emit(report, actor="mero")

    def test_census_default_run_does_not_write(self, board: CardStore) -> None:
        before = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        _census(board.home).run()
        after = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        assert before == after

    def test_run_blocker_census_emit_false_is_pure(self, board: CardStore) -> None:
        before = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        mc.run_blocker_census(board.home, now=_fixed_now())
        after = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        assert before == after

    def test_mero_cannot_append_lifecycle_events(self, board: CardStore) -> None:
        """The store has no Mero path to claim, complete, move, or void.

        Mero's only write is the typed recommendation via emit(); this test
        pins that emit() never changes lifecycle fields on the folded card.
        """
        census = _census(board.home)
        before = board.fold("bbbb0002")
        report = census.run()
        census.emit(report)
        after = board.fold("bbbb0002")
        assert before.status == after.status
        assert before.owner == after.owner
        assert before.archived == after.archived
        assert before.dependencies == after.dependencies

    def test_creating_a_card_stays_outside_mero(self, tmp_path: Path) -> None:
        """Card creation is a governed store write, not a Mero seat action.

        Mero's module exposes no create path at all; the only creation API is
        the store's governed ``create``, whose use by Mero is denied by the
        seat boundary tests above (no CREATE action exists for any seat).
        """
        assert not hasattr(mc.MeroBlockerCensus, "create")
        assert not hasattr(mc.MeroBlockerCensus, "claim")
        assert not hasattr(mc.MeroBlockerCensus, "release")
        assert not hasattr(mc.MeroBlockerCensus, "launch")
        assert not hasattr(mc.MeroBlockerCensus, "stop")
        assert not hasattr(mc.MeroBlockerCensus, "merge")
        assert not hasattr(mc.MeroBlockerCensus, "deploy")
        assert not hasattr(mc, "rerun_selector")

    def test_mero_actor_cannot_append_events_as_another_writer(self, tmp_path: Path) -> None:
        """Emission is fenced to the mero actor name, not just the seat."""
        home = _home(tmp_path)
        store = CardStore(home)
        store.create(CardCore(id="aaaa0001", title="t", created_by="jarvis"))
        census = _census(home)
        report = census.run()
        assert report.findings == []
        # even a mero-named actor cannot emit through a non-mero identity
        with pytest.raises(BoundaryError):
            census.emit(report, actor="MERO-IMPOSTOR")

    def test_credentials_and_protected_data_are_unreachable(self, tmp_path: Path) -> None:
        """The census module imports no auth or provider surface.

        The word-level assertion pins imports and call targets rather than
        prose: the module must not reach the estate's auth, secret, or
        provider machinery anywhere in its source.
        """
        source = Path(mc.__file__).read_text().lower()
        for forbidden in (
            "import capauth",
            "capauth.",
            "from .capauth",
            "keyring",
            "getpass",
            "secrets",
            "protected_data",
            "os.environ",
        ):
            assert forbidden not in source

    def test_selector_rerun_is_impossible_from_the_census(self, board: CardStore) -> None:
        """No census entry point shells out or invokes the fleet selector."""
        source = Path(mc.__file__).read_text()
        for forbidden in (
            "subprocess",
            "skfleet-rotate",
            "popen",
            "system(",
            "os.exec",
            "skcapstone coord claim",
        ):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# AC5 support: serializer discipline.
# ---------------------------------------------------------------------------


class TestSerializerDiscipline:
    def test_event_to_json_then_parse_round_trips(self) -> None:
        event = {"b": 1, "a": [1, 2, {"c": "x"}], "schema": mc.RECOMMENDATION_SCHEMA}
        line = mc.recommendation_event_to_json(event)
        assert line == json.dumps(event, sort_keys=True, separators=(",", ":"))
        assert mc.parse_recommendation_line(line) == event

    def test_parse_rejects_non_json_garbage(self) -> None:
        with pytest.raises(ValueError):
            mc.parse_recommendation_line("this is not json")

    def test_parse_rejects_non_object_lines(self) -> None:
        with pytest.raises(ValueError):
            mc.parse_recommendation_line("[1, 2, 3]")

    def test_findings_digests_are_stable(self, board: CardStore) -> None:
        first = _census(board.home).run()
        second = _census(board.home).run()
        ids1 = {f["recommendation_id"] for f in first.findings}
        ids2 = {f["recommendation_id"] for f in second.findings}
        assert ids1 == ids2


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
    def test_skmail_signals_are_joined_not_decisive(self, board: CardStore) -> None:
        """Mail rows join into source events but never mint a finding alone."""
        signals = {"bbbb0002": [{"from": "jarvis", "re": "stuck?", "ts": NOW.isoformat()}]}
        census = mc.MeroBlockerCensus(
            board.home, now=_fixed_now(), skmail_reader=lambda cid: signals.get(cid, [])
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
