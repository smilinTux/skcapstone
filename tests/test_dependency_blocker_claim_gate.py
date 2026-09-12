"""Claim-gate coverage for unresolved dependency blockers (4cd4dd62/4cd4dd63)."""

from __future__ import annotations

from pathlib import Path

import pytest
from skcoord.card_store import CardCore

from skcapstone.card_store import CardStore
from skcapstone.review_admission import (
    assert_governed_review_claim,
    dependency_blocker_unresolved,
    governed_review_gate_reasons,
)


def _review_core(dep: str = "383a7834") -> dict[str, object]:
    payload = {
        "producer_identity": "codex-resume-383a7834",
        "candidate_evidence_sha256": "a" * 64,
        "blocked_on": f"dependency card:{dep}",
        "evidence": f"/tmp/{dep}/BLOCKED-report.json",
        "evidence_sha256": "5658c6eed5fae206138d6adfc7daf45602899ee78f78e8e34ac1716059f3c2f1",
        "link_source_card": dep,
        "link_head_revision": "b" * 40,
        "repository": "https://example.invalid/sklegal.git",
        "base_ref": "main",
    }
    return {
        "id": "4cd4dd62",
        "title": "[W72-COMM01-R][M][REVIEW] Independently verify",
        "description": "review",
        "links": {},
        "meta": payload,
    }


def test_unresolved_dependency_blocker_holds_without_do_not_claim(tmp_path: Path) -> None:
    """Hashed blocked_on dependency parks claims until shared bytes appear."""
    dep = "383a7834"
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id=dep,
            kind="task",
            title="producer",
            description="producer",
            originator="codex",
            acceptance_criteria=["publish bytes"],
        )
    )
    core = _review_core(dep)
    labels = ["review", "seat-seraph", f"parent-{dep}"]
    assert dependency_blocker_unresolved(tmp_path, core, labels) is True
    assert "dependency-blocker" in governed_review_gate_reasons(
        core, labels, dependency_blocker_holds=True
    )
    store.create(
        CardCore(
            id="4cd4dd62",
            kind="task",
            title=str(core["title"]),
            description="review",
            originator="codex",
            acceptance_criteria=["verify"],
            initial_labels=labels,
            meta=dict(core["meta"]),  # type: ignore[arg-type]
        )
    )
    with pytest.raises(ValueError, match="dependency-blocker"):
        assert_governed_review_claim(tmp_path, "4cd4dd62", "pi-seraph-chiap08-4cd4dd62")


def test_dependency_blocker_clears_after_shared_bytes(tmp_path: Path) -> None:
    """Re-eligibility returns only after the dependency publishes work-root bytes."""
    dep = "383a7834"
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id=dep,
            kind="task",
            title="producer",
            description="producer",
            originator="codex",
            acceptance_criteria=["publish bytes"],
        )
    )
    core = _review_core(dep)
    assert dependency_blocker_unresolved(tmp_path, core, ["review"]) is True
    work = tmp_path / "evidence" / "work" / dep
    work.mkdir(parents=True)
    (work / "candidate.patch").write_text("reachable-bytes\n", encoding="utf-8")
    assert dependency_blocker_unresolved(tmp_path, core, ["review"]) is False
