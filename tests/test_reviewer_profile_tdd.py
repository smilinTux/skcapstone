"""Check the inert reviewer design and reused boundaries, not a new runtime."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from skcapstone.link_cycle import (
    ProducerIdentity,
    PullRequestObservation,
    ReviewerIdentity,
    recommend_one_reviewer,
)
from skcapstone.seat_boundaries import Action, BoundaryError, require_authority

ROOT = Path(__file__).resolve().parents[1]
TDD = ROOT / "docs/fleet/reviewer-profile-tdd-90151318.md"
DESIGN = json.loads(TDD.read_text().split("```json\n", 1)[1].split("\n```", 1)[0])


def test_profile_matrix_is_closed_and_inert() -> None:
    """Pin each responsibility and prevent accidental activation or widening."""
    assert DESIGN["schema"] == "skfleet.reviewer-profile-design/v1"
    assert DESIGN["activation"] == "disabled"
    assert (DESIGN["card"], DESIGN["parent"]) == ("90151318", "91ad57c8")
    expected = {
        "link": ("link", "recurring", "none", "none", 300, 120),
        "seraph": ("seraph", "disposable", "none", "none", 0, 1800),
        "mero": ("mero", "recurring", "none", "none", 600, 180),
        "qwen-reviewer": ("qwen", "disposable", "review.qwen.local/v1", "local-qwen", 0, 1800),
        "codex-reviewer": (
            "codex",
            "disposable",
            "review.codex.approved/v1",
            "openai-codex",
            0,
            1800,
        ),
    }
    rows = DESIGN["profiles"]
    assert len(rows) == len(expected)
    assert {row["id"] for row in rows} == set(expected)
    for field in ("id", "policy", "mailbox"):
        assert len({row[field] for row in rows}) == len(rows)
    forbidden = {
        "claim",
        "release",
        "launch",
        "stop",
        "reassign",
        "rotate",
        "merge",
        "deploy",
        "install_artifact",
        "actuate_application",
        "rollback",
    }
    for row in rows:
        assert set(row) == {
            "id",
            "seat",
            "policy",
            "mailbox",
            "route",
            "provider",
            "mode",
            "operation",
            "authority",
            "tools",
            "interval_s",
            "timeout_s",
        }
        actual = tuple(
            row[k] for k in ("seat", "mode", "route", "provider", "interval_s", "timeout_s")
        )
        assert actual == expected[row["id"]]
        assert not forbidden.intersection(row["authority"])
        assert ("model.propose" in row["tools"]) == (row["route"] != "none")
        assert ("verdict.propose" in row["tools"]) == (row["mode"] == "disposable")
        assert "*" not in row["tools"]


@pytest.mark.parametrize("path,digest", DESIGN["pins"].items())
def test_source_pins_are_reconstructable(path: str, digest: str) -> None:
    """Verify exact historical blobs without trusting a dirty working tree."""
    assert re.fullmatch(r"[0-9a-f]{40}", DESIGN["baseline"])
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    content = subprocess.check_output(["git", "show", f"{DESIGN['baseline']}:{path}"], cwd=ROOT)
    assert hashlib.sha256(content).hexdigest() == digest


@pytest.mark.parametrize("actor", ["link", "mero", "seraph", "qwen-reviewer", "codex-reviewer"])
def test_profiles_have_no_existing_fleet_control(actor: str) -> None:
    """Reviewer names must never turn into dispatcher authorization."""
    for action in (Action.CLAIM, Action.LAUNCH, Action.RELEASE, Action.DEPLOY):
        with pytest.raises(BoundaryError):
            require_authority(actor, action)


@pytest.mark.parametrize("shared", [None, "identity", "host", "session", "workspace"])
def test_each_producer_dimension_must_be_distinct(shared: str | None) -> None:
    """Exercise the actual Link boundary for each required separation field."""
    producer = ProducerIdentity("source-person", "source-host", "source-run", "/source")
    reviewer = ReviewerIdentity("Seraph", "review-person", "review-host", "review-run", "/review")
    if shared is not None:
        reviewer = replace(reviewer, **{shared: getattr(producer, shared).upper()})
    observation = PullRequestObservation(
        repository="smilinTux/skcapstone",
        number=1,
        title="Synthetic candidate",
        author="source-person",
        head_sha="a" * 40,
        base_sha="b" * 40,
        ci_state="success",
        conflict_state="clean",
        review_requests=(),
        source_card="source-card",
        card_generation="source-generation",
        observed_at="2026-09-08T00:00:00+00:00",
    )
    kwargs = dict(
        producer=producer,
        candidates=[reviewer],
        review_card_id="review-card",
        review_card_revision="c" * 64,
    )
    if shared is not None:
        with pytest.raises(BoundaryError, match="no distinct eligible reviewer"):
            recommend_one_reviewer(observation, **kwargs)
    else:
        handoff = recommend_one_reviewer(observation, **kwargs)
        assert handoff.reviewer_identity == reviewer
        assert handoff.head_sha == observation.head_sha
        assert handoff.recommendation_id == handoff.computed_recommendation_id
        assert recommend_one_reviewer(observation, **kwargs) == handoff
        assert (
            recommend_one_reviewer(
                replace(observation, active_review_card="existing-review"), **kwargs
            )
            is None
        )
