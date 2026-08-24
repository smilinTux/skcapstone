from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card import Card, Column, Kind

from skdashboard.control_plane_adapters import SPECS, aggregate_reader
from skdashboard.dashboard_governance import get_governance_projection

NOW = datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
QUERY = {
    "role": "governance",
    "scope": "estate",
    "window": "latest",
    "baseline": "none",
    "service": "all",
}


def readers(*, stale=False, partial=False):
    output = {}
    for spec in SPECS:
        aggregate = {field: 1 for field in spec.fields}
        if spec.adapter_id == "capauth.policy":
            aggregate = {"available": True, "denials": 2}
        output[spec.adapter_id] = aggregate_reader(
            aggregate,
            expected=2 if partial and spec.adapter_id == "skcoord.flow" else 1,
            reporting=1,
            observed_at=(
                "2026-08-24T17:00:00Z"
                if stale and spec.adapter_id == "skcapstone.fleet"
                else "2026-08-24T18:00:00Z"
            ),
            errors=["partial"] if partial and spec.adapter_id == "skcoord.flow" else [],
            watermark_data=spec.adapter_id,
        )
    return output


def card(card_id, *, criteria=None, dependencies=None, owner=None):
    return Card(
        id=card_id,
        kind=Kind.TASK,
        title="excluded title",
        description="excluded protected description",
        status=Column.DOING if owner else Column.BACKLOG,
        swimlane="feature",
        priority="high",
        originator="fixture",
        owner=owner,
        labels=[],
        acceptance_criteria=list(criteria or []),
        dependencies=list(dependencies or []),
        links={},
        meta={},
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-24T17:00:00Z",
    )


class Store:
    def __init__(self, _home):
        self.cards = [
            card("source", dependencies=["missing"], owner="agent-a"),
            card("complete", criteria=["Has exact criteria"]),
        ]

    def list_cards(self, include_archived=False):
        assert include_archived is True
        return self.cards

    def _read_events(self, card_id):
        if card_id != "source":
            return []
        return [
            {
                "event_id": "event-1",
                "action": "amend_criteria",
                "writer": "reviewer",
                "ts": "2026-08-24T17:30:00Z",
                "criteria": ["excluded event payload"],
            },
            {
                "event_id": "event-2",
                "action": "note",
                "writer": "reviewer",
                "ts": "2026-08-24T17:31:00Z",
                "text": "excluded note payload",
            },
        ]


def test_governance_lineage_findings_and_append_only_history_are_distinct(tmp_path: Path) -> None:
    projection = get_governance_projection(
        tmp_path,
        QUERY,
        aggregate_readers=readers(stale=True, partial=True),
        store_factory=Store,
        policy_decision={
            "state": "allow",
            "decision_ref": "decision-1",
            "expires_at": "2026-08-24T18:05:00Z",
        },
        now=NOW,
    )
    classes = projection["finding_classes"]
    assert set(classes) >= {
        "policy_unavailable",
        "policy_denial",
        "stale_evidence",
        "partial_coverage",
        "orphan_dependency",
        "duplicate_card",
        "missing_criteria",
        "claim_ttl",
    }
    assert classes["policy_denial"]["count"] == 2
    assert classes["stale_evidence"]["count"] == 1
    assert classes["partial_coverage"]["count"] == 1
    assert classes["orphan_dependency"]["count"] == 1
    assert classes["missing_criteria"]["count"] == 1
    assert classes["duplicate_card"]["count"] is None
    assert classes["duplicate_card"]["truth_state"] == "unknown"
    assert classes["claim_ttl"]["truth_state"] == "unknown"

    assert len(projection["metric_lineage"]) == len(projection["registry"]["definition_hashes"])
    for item in projection["metric_lineage"]:
        assert item["definition_hash"].startswith("sha256:")
        assert item["authoritative_owner"]
        assert item["source_watermark"]["value"]
        assert item["classification"]
        assert item["calculation"]["calculation_ref"]
        assert item["human_review"]["state"] == "unknown"
    confidential = next(
        item for item in projection["metric_lineage"] if item["classification"] == "confidential"
    )
    assert confidential["classification_policy_decision_ref"] == "decision-1"

    assert projection["correction_history"] == [
        {
            "record_id": "event-1",
            "target_id": "source",
            "kind": "correction",
            "action": "amend_criteria",
            "attributed_to": "reviewer",
            "recorded_at": "2026-08-24T17:30:00Z",
            "append_only_ref": "skcoord.card_store:source:event:event-1",
        }
    ]
    serialized = json.dumps(projection)
    assert "excluded title" not in serialized
    assert "excluded protected description" not in serialized
    assert "excluded event payload" not in serialized
    assert "excluded note payload" not in serialized
    assert projection["history_contract"]["rewrites_history"] is False
    assert all(item["remediation_preview"]["preview_only"] for item in projection["findings"])
    assert all(
        not item["remediation_preview"]["dispatch_authorized"] for item in projection["findings"]
    )


def test_policy_unavailable_is_not_zero_or_denial(tmp_path: Path) -> None:
    unavailable = readers()
    unavailable.pop("capauth.policy")
    projection = get_governance_projection(
        tmp_path,
        QUERY,
        aggregate_readers=unavailable,
        store_factory=Store,
        now=NOW,
    )
    assert projection["policy"]["available"] is None
    assert projection["policy"]["denials"] is None
    assert projection["finding_classes"]["policy_unavailable"]["count"] == 1
    assert projection["finding_classes"]["policy_denial"]["count"] is None
    assert projection["report_registry"]["truth_state"] == "unknown"


def test_governance_workspace_is_table_first_read_only_and_protected() -> None:
    root = Path(__file__).parents[1]
    html = (root / "src/skdashboard/static/governance.html").read_text()
    js = (root / "src/skdashboard/static/js/governance.js").read_text()
    assert html.count("<table") == 4
    for value in (
        "Policy unavailable, denial, stale evidence, partial coverage",
        "preview-only",
        "cannot rewrite history",
        "cannot rewrite history",
        "Matter data are excluded",
    ):
        assert value.lower() in html.lower()
    assert "fetch(" not in js
    assert "postJSON" not in js
    assert "individual" in html.lower()
