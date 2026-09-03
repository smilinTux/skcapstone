"""Executable tests for the canonical seat authority boundaries."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.link_merge_authority import MergeCandidate
from skcapstone.seat_boundaries import (
    Action,
    BoundaryError,
    DispatchRecommendation,
    append_recommendation,
    assign_distinct_reviewer,
    authorize_recommendation_action,
    evaluate_merge_as_link,
    require_authority,
)


def recommendation() -> DispatchRecommendation:
    """Build one valid advisory recommendation."""

    return DispatchRecommendation(
        card_id="abc12345",
        recommendation_id="mero-abc12345-1",
        recommender="mero",
        observed_at=datetime.now(timezone.utc),
        observed_claim_owner="pi-qwen-chiap01-abc12345",
        observed_claim_revision="revision-1",
        observed_process={"state": "stalled", "pid": 123},
        reason="heartbeat and process output are stale",
        evidence_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    "action",
    [
        Action.CLAIM,
        Action.RELEASE,
        Action.LAUNCH,
        Action.STOP,
        Action.REASSIGN,
        Action.ROTATE,
        Action.MERGE,
        Action.DEPLOY,
        Action.ACTUATE_APPLICATION,
    ],
)
def test_mero_is_read_only(action: Action) -> None:
    """Mero cannot mutate coordination, repositories, or applications."""

    with pytest.raises(BoundaryError):
        require_authority("mero", action)


def test_link_assigns_a_distinct_reviewer() -> None:
    """Link skips itself and the source author deterministically."""

    assert (
        assign_distinct_reviewer(
            author="producer", assigner="link", candidates=["producer", "link", "reviewer"]
        )
        == "reviewer"
    )


def test_link_fails_closed_without_a_distinct_reviewer() -> None:
    """Reviewer assignment never silently falls back to Link or the author."""

    with pytest.raises(BoundaryError, match="no distinct reviewer"):
        assign_distinct_reviewer(
            author="producer", assigner="link", candidates=["producer", "link"]
        )


@pytest.mark.parametrize("candidate", ["", "   "])
def test_link_skips_blank_reviewer(candidate: str) -> None:
    """Blank identities are never accepted as reviewers."""

    with pytest.raises(BoundaryError, match="no distinct reviewer"):
        assign_distinct_reviewer(author="producer", assigner="link", candidates=[candidate])


def test_link_skips_blank_candidates_and_preserves_valid_order() -> None:
    """The first valid normalized identity wins without reordering."""

    assert (
        assign_distinct_reviewer(
            author="producer",
            assigner="link",
            candidates=["", " producer ", "  reviewer-one  ", "reviewer-two"],
        )
        == "reviewer-one"
    )


def test_link_fails_closed_when_every_candidate_is_invalid() -> None:
    """Blank, author, and assigner identities cannot satisfy assignment."""

    with pytest.raises(BoundaryError, match="no distinct reviewer"):
        assign_distinct_reviewer(
            author="producer", assigner="link", candidates=["", " ", "producer", " link "]
        )


def test_only_link_can_evaluate_merge_queue() -> None:
    """Jarvis fleet authority does not grant merge-queue authority."""

    candidate = MergeCandidate(
        repository="owner/repo",
        number=1,
        title="normal source repair",
        categories=("source",),
        head_sha="a" * 40,
        author="producer",
        mergeable=False,
        failed_checks=0,
        review=None,
    )
    decision = evaluate_merge_as_link("link", candidate)
    assert not decision.eligible
    with pytest.raises(BoundaryError, match="not authorized"):
        evaluate_merge_as_link("jarvis", candidate)


@pytest.mark.parametrize(
    "action", [Action.CLAIM, Action.RELEASE, Action.LAUNCH, Action.STOP, Action.DEPLOY]
)
def test_link_cannot_dispatch_or_deploy(action: Action) -> None:
    """Link owns integration, not fleet or deployment actuation."""

    with pytest.raises(BoundaryError):
        require_authority("link", action)


def test_jarvis_fleet_authority_does_not_imply_application_actuation() -> None:
    """Fleet process control grants no application action authority."""

    require_authority("jarvis", Action.LAUNCH)
    with pytest.raises(BoundaryError):
        require_authority("jarvis", Action.ACTUATE_APPLICATION)


def test_only_explicit_fenced_actor_may_mutate_fleet() -> None:
    """Unknown actors fail closed unless named in the exact fence."""

    with pytest.raises(BoundaryError):
        require_authority("system-reaper", Action.RELEASE)
    require_authority("system-reaper", Action.RELEASE, fenced_system_actors={"system-reaper"})


def test_jarvis_accepts_fresh_recommendation_once() -> None:
    """Exact current owner, revision, and process state satisfy the fence."""

    item = recommendation()
    authorize_recommendation_action(
        item,
        actor="jarvis",
        action=Action.RELEASE,
        current_claim_owner=item.observed_claim_owner,
        current_claim_revision=item.observed_claim_revision,
        current_process=item.observed_process,
        used_recommendation_ids=set(),
    )
    assert item.as_event()["schema"] == "skfleet.dispatch-recommendation/v1"


def test_recommendation_append_is_typed_and_idempotent(tmp_path) -> None:
    """Recommendation emission is append-only with a stable duplicate key."""

    item = recommendation()
    CardStore(tmp_path).create(CardCore(id=item.card_id, title="candidate"))
    first = append_recommendation(tmp_path, item)
    second = append_recommendation(tmp_path, item)
    assert first == second
    assert first["action"] == "dispatch_recommendation"
    assert first["schema"] == "skfleet.dispatch-recommendation/v1"
    assert first["transition_id"] == item.recommendation_id


def test_recommendation_replay_fails_closed() -> None:
    """A consumed recommendation id cannot authorize another mutation."""

    item = recommendation()
    with pytest.raises(BoundaryError, match="replay"):
        authorize_recommendation_action(
            item,
            actor="jarvis",
            action=Action.RELEASE,
            current_claim_owner=item.observed_claim_owner,
            current_claim_revision=item.observed_claim_revision,
            current_process=item.observed_process,
            used_recommendation_ids={item.recommendation_id},
        )


@pytest.mark.parametrize("revision", [None, "", " "])
def test_blank_observed_claim_revision_fails_closed(revision: str | None) -> None:
    """A recommendation without an exact observed revision cannot authorize action."""

    item = replace(recommendation(), observed_claim_revision=revision)
    with pytest.raises(BoundaryError, match="observed claim revision is required"):
        authorize_recommendation_action(
            item,
            actor="jarvis",
            action=Action.RELEASE,
            current_claim_owner=item.observed_claim_owner,
            current_claim_revision=revision,
            current_process=item.observed_process,
            used_recommendation_ids=set(),
        )


@pytest.mark.parametrize("revision", [None, "", " "])
def test_blank_current_claim_revision_fails_closed(revision: str | None) -> None:
    """Current CardStore state must supply an exact nonblank revision."""

    item = recommendation()
    with pytest.raises(BoundaryError, match="current claim revision is required"):
        authorize_recommendation_action(
            item,
            actor="jarvis",
            action=Action.RELEASE,
            current_claim_owner=item.observed_claim_owner,
            current_claim_revision=revision,
            current_process=item.observed_process,
            used_recommendation_ids=set(),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"current_claim_owner": "someone-else"}, "owner changed"),
        ({"current_claim_revision": "revision-2"}, "revision changed"),
        ({"current_process": {"state": "running", "pid": 123}}, "process state changed"),
    ],
)
def test_stale_recommendation_fails_closed(changes: dict[str, object], message: str) -> None:
    """Any drift since observation invalidates recommendation authority."""

    item = recommendation()
    arguments = {
        "current_claim_owner": item.observed_claim_owner,
        "current_claim_revision": item.observed_claim_revision,
        "current_process": item.observed_process,
    }
    arguments.update(changes)
    with pytest.raises(BoundaryError, match=message):
        authorize_recommendation_action(
            item,
            actor="jarvis",
            action=Action.RELEASE,
            used_recommendation_ids=set(),
            **arguments,
        )


def test_link_cannot_act_on_recommendation() -> None:
    """Advisory events cannot turn Link into the fleet dispatcher."""

    item = replace(recommendation(), recommender="link")
    with pytest.raises(BoundaryError):
        authorize_recommendation_action(
            item,
            actor="link",
            action=Action.RELEASE,
            current_claim_owner=item.observed_claim_owner,
            current_claim_revision=item.observed_claim_revision,
            current_process=item.observed_process,
            used_recommendation_ids=set(),
        )


def test_invalid_recommendation_envelope_fails_closed() -> None:
    """Evidence and timestamps must be deterministic and auditable."""

    with pytest.raises(BoundaryError, match="timezone aware"):
        replace(recommendation(), observed_at=datetime.now()).validate()
    with pytest.raises(BoundaryError, match="SHA-256"):
        replace(recommendation(), evidence_sha256="not-a-hash").validate()
