"""Review assignment consumes folded typed metadata with a legacy fallback."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
COORD = ROOT / "src" / "skcapstone" / "cli" / "coord.py"


class BoundaryError(Exception):
    """Test boundary failure."""


def _load_assignment() -> tuple[dict[str, object], list[dict[str, object]]]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_review_assignment"
    )
    seen = []

    def recommend(_home, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    namespace = {
        "BoundaryError": BoundaryError,
        "HOME": "/tmp",
        "Path": Path,
        "authorize_review_launch": lambda *_args, **_kwargs: SimpleNamespace(reviewer="link"),
        "event_rows": lambda _cid: [],
        "hashlib": hashlib,
        "re": re,
        "recommend_reviewer": recommend,
        "_card_process_snapshot": lambda _cid: {"sessions": []},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace, seen


def test_typed_metadata_wins_over_stale_description() -> None:
    namespace, seen = _load_assignment()
    digest = "b" * 64
    core = {
        "description": "Producer identity: stale. Candidate evidence sha256=" + "a" * 64 + ".",
        "links": {
            "producer_identity": "current-producer",
            "candidate_evidence_sha256": digest,
        },
    }

    reviewer, _recommendation, _handoff = namespace["_review_assignment"](
        "deadbeef", core, ["review"], "link"
    )

    assert reviewer == "link"
    assert seen[0]["author"] == "current-producer"
    assert seen[0]["evidence_sha256"] == digest


def test_coord_create_help_example_supplies_claimable_review_metadata() -> None:
    source = COORD.read_text(encoding="utf-8")
    assert "coord link e5f6a7b8 producer_identity pi-codex-source" in source
    assert "coord link e5f6a7b8 candidate_evidence_sha256 " in source
    namespace, seen = _load_assignment()
    digest = "a" * 64

    namespace["_review_assignment"](
        "e5f6a7b8",
        {
            "description": "Producer identity: pi-codex-source. Candidate evidence "
            f"sha256={digest}.",
            "links": {
                "producer_identity": "pi-codex-source",
                "candidate_evidence_sha256": digest,
            },
        },
        ["review"],
        "link",
    )

    assert seen[0]["author"] == "pi-codex-source"
    assert seen[0]["evidence_sha256"] == digest


def test_legacy_description_remains_supported() -> None:
    namespace, seen = _load_assignment()
    digest = "c" * 64
    core = {
        "description": "Producer identity: legacy-producer. Candidate evidence sha256="
        + digest
        + "."
    }

    namespace["_review_assignment"]("deadbeef", core, ["review"], "link")

    assert seen[0]["author"] == "legacy-producer"
    assert seen[0]["evidence_sha256"] == digest


@pytest.mark.parametrize(
    "links",
    [
        {"producer_identity": "producer-only"},
        {"candidate_evidence_sha256": "d" * 64},
        {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "not-a-digest",
        },
    ],
)
def test_incomplete_or_malformed_typed_metadata_fails_closed(links) -> None:
    namespace, seen = _load_assignment()
    core = {
        "description": "Producer identity: valid-fallback. Candidate evidence sha256="
        + "e" * 64
        + ".",
        "links": links,
    }

    with pytest.raises(BoundaryError):
        namespace["_review_assignment"]("deadbeef", core, ["review"], "link")
    assert seen == []
