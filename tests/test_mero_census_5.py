"""Card 2516480b negative tests: Mero cannot mutate anything.

AC4 of the census. Sixteen refusals prove Mero holds exactly OBSERVE and
RECOMMEND: no claim, release, launch, stop, reassign, rotate, repair, merge,
deploy, card creation or mutation, selector rerun, credential or protected
data reach, and no write path that changes lifecycle state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc
from skcapstone.seat_boundaries import Action, BoundaryError, require_authority
from tests.census_support import _board_store, _census, _home


def _board(tmp_path: Path) -> CardStore:
    store = _board_store(tmp_path)
    return store


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

    def test_only_mero_may_emit_census_recommendations(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        census = _census(board.home)
        report = census.run()
        for actor in ("jarvis", "link", "worker-a", "chef", "pi-mero-chiap03-2516480b"):
            with pytest.raises(BoundaryError):
                census.emit(report, actor=actor)
        census.emit(report, actor="mero")

    def test_census_default_run_does_not_write(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        before = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        _census(board.home).run()
        after = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        assert before == after

    def test_run_blocker_census_emit_false_is_pure(self, tmp_path: Path) -> None:
        board = _board(tmp_path)
        before = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        mc.run_blocker_census(board.home, now=lambda: mc_census_now())
        after = {cid: len(board._read_events(cid)) for cid in board.list_card_ids()}
        assert before == after

    def test_mero_cannot_append_lifecycle_events(self, tmp_path: Path) -> None:
        """The store has no Mero path to claim, complete, move, or void.

        Mero's only write is the typed recommendation via emit(); this test
        pins that emit() never changes lifecycle fields on the folded card.
        """
        board = _board(tmp_path)
        census = _census(board.home)
        before = board.fold("bbbb0002")
        report = census.run()
        census.emit(report)
        after = board.fold("bbbb0002")
        assert before.status == after.status
        assert before.owner == after.owner
        assert before.archived == after.archived
        assert before.dependencies == after.dependencies

    def test_creating_a_card_stays_outside_mero(self) -> None:
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

    def test_credentials_and_protected_data_are_unreachable(self) -> None:
        """The census package imports no auth or provider surface.

        The word-level assertion pins imports and call targets rather than
        prose: the package must not reach the estate's auth, secret, or
        provider machinery anywhere in its source.
        """
        package_dir = Path(mc.__file__).parent
        for path in sorted(package_dir.glob("*.py")):
            source = path.read_text().lower()
            for forbidden in (
                "import capauth",
                "capauth.",
                "from .capauth",
                "from skcapstone.capauth",
                "keyring",
                "getpass",
                "secrets",
                "protected_data",
                "os.environ",
            ):
                assert forbidden not in source, f"{path.name} must not use {forbidden}"

    def test_selector_rerun_is_impossible_from_the_census(self) -> None:
        """No census entry point shells out or invokes the fleet selector."""
        package_dir = Path(mc.__file__).parent
        for path in sorted(package_dir.glob("*.py")):
            source = path.read_text()
            for forbidden in (
                "subprocess",
                "skfleet-rotate",
                "popen",
                "system(",
                "os.exec",
                "skcapstone coord claim",
            ):
                assert forbidden not in source, f"{path.name} must not use {forbidden}"


def mc_census_now():
    from tests.census_support import NOW

    return NOW
