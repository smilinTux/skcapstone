"""Native elastic reviewer identities retain governed seat qualification."""

import json

import pytest

from skcapstone.review_verdict import validate_review_completion
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.fleet.test_production_source_review_brief import (
    git,
    hosted,
    review,  # noqa: F401
    run_script,
    script,
    selected,
)

CARD = "c1a30162"
OWNER = "pi-codex-review-chiap08-c1a30162"


@pytest.fixture
def elastic(request):
    """Use the installed probe's exact owner and absent typed-tree card shape."""
    fixture = request.getfixturevalue("review")
    _, report = hosted(fixture)
    core = fixture["core"]
    core["id"] = CARD
    core["owner"] = core["initial_owner"] = OWNER
    core["labels"] = core["initial_labels"] = ["review", "seat-seraph"]
    del core["meta"]["candidate_tree"]
    path = fixture["path"].parent.with_name(CARD) / "core.json"
    path.parent.mkdir()
    path.write_text(json.dumps(core))
    fixture["path"].unlink()
    fixture["path"] = path
    evidence_dir = report.parent.with_name(CARD)
    evidence_dir.mkdir(mode=0o700)
    report = report.rename(evidence_dir / report.name)
    fixture["env"]["REVIEW_EVIDENCE"] = str(report)
    return fixture


@pytest.mark.parametrize("seat", ["seraph", "qualified-audit"])
def test_actual_elastic_identity_renders_and_hands_off_with_qualified_seat(elastic, seat):
    core = elastic["core"]
    core["labels"] = core["initial_labels"] = ["review", "seat-" + seat]
    core["meta"]["qualified_reviewer_seats"] = [seat]
    elastic["path"].write_text(json.dumps(core))
    brief = selected(elastic)
    assert brief.startswith("PRODUCTION HOSTED INDEPENDENT REVIEW")
    result = run_script(elastic, script(brief, 0))
    assert result.returncode == 0, result.stderr
    LiveCardStoreGateway(elastic["native"]).read_card(CARD)
    validate_review_completion(CARD, core["title"], elastic["native"])
    assert git(elastic["work"], "rev-parse", "HEAD") == elastic["head"]
    assert not git(elastic["work"], "status", "--porcelain")


@pytest.mark.parametrize(
    "problem",
    [
        "wrong-card",
        "malformed-host",
        "wrong-prefix",
        "same-producer",
        "producer-seat",
        "missing-seat",
        "unqualified-seat",
        "multiple-seats",
    ],
)
def test_elastic_identity_does_not_bypass_native_qualification(elastic, problem):
    core = elastic["core"]
    if problem == "wrong-card":
        core["owner"] = OWNER.replace(CARD, "c1a30158")
    elif problem == "malformed-host":
        core["owner"] = OWNER.replace("chiap08", "_chiap08")
    elif problem == "wrong-prefix":
        core["owner"] = OWNER.replace("codex-review", "codex-builder")
    elif problem == "same-producer":
        core["meta"]["producer_identity"] = OWNER
    elif problem == "producer-seat":
        core["meta"]["producer_identity"] = "pi-seraph-other-c1a30138"
    else:
        core["labels"] = {
            "missing-seat": ["review"],
            "unqualified-seat": ["review", "seat-unqualified"],
            "multiple-seats": ["review", "seat-seraph", "seat-unqualified"],
        }[problem]
    assert selected(elastic) == "LEGACY SIX CI INSTRUCTIONS"
    assert not (elastic["native"] / "coordination/card_events").exists()
