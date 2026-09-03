"""Focused live-impact selector priority contracts."""

import ast
import datetime
import re
import time
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
NOW = 1_788_467_200.0


def function():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "_live_impact_priorities")
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_LIVE_IMPACT_CLASSES" for t in node.targets
            )
        )
    ]
    namespace = {"datetime": datetime, "re": re, "time": time}
    exec(compile(ast.Module(nodes, []), str(ROTATE), "exec"), namespace)
    return namespace["_live_impact_priorities"]


def event(**changes):
    process = {
        "finding_id": "finding-1",
        "finding_class": "lane_outage",
        "observed_at": datetime.datetime.fromtimestamp(
            NOW - 60, datetime.timezone.utc
        ).isoformat(),
        "acknowledged": True,
        "resolved": False,
        "affected_lanes": ["glm"],
    }
    process.update(changes.pop("process", {}))
    row = {
        "action": "mero_observation",
        "card_id": "repair-card",
        "payload": {"process": process, "evidence_sha256": "a" * 64},
    }
    row.update(changes)
    return row


def test_current_acknowledged_finding_preempts_with_evidence():
    priorities, rejected = function()([event()], now=NOW)
    assert rejected == ()
    assert priorities["repair-card"]["finding_class"] == "lane_outage"
    assert priorities["repair-card"]["evidence_sha256"] == "a" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"acknowledged": False},
        {"resolved": True},
        {"finding_class": "feature_request"},
        {"affected_lanes": []},
        {"observed_at": "malformed"},
    ],
)
def test_unqualified_findings_never_preempt(change):
    priorities, _ = function()([event(process=change)], now=NOW)
    assert priorities == {}


def test_stale_expiry_and_newer_resolution():
    old = event(
        process={
            "observed_at": datetime.datetime.fromtimestamp(
                NOW - 901, datetime.timezone.utc
            ).isoformat()
        }
    )
    priorities, rejected = function()([old], now=NOW)
    assert priorities == {} and rejected == ("repair-card",)
    resolved_at = datetime.datetime.fromtimestamp(NOW - 30, datetime.timezone.utc).isoformat()
    priorities, rejected = function()(
        [event(), event(process={"resolved": True, "observed_at": resolved_at})], now=NOW
    )
    assert priorities == {} and rejected == ()


def test_duplicate_is_deduplicated_and_conflict_fails_closed():
    row = event()
    priorities, rejected = function()([row, row], now=NOW)
    assert tuple(priorities) == ("repair-card",) and rejected == ()
    priorities, rejected = function()([row, event(card_id="other-card")], now=NOW)
    assert priorities == {} and rejected == ("other-card", "repair-card")
    priorities, rejected = function()([row, event(card_id="other-card"), row], now=NOW)
    assert priorities == {} and rejected == ("other-card", "repair-card")


def test_source_preserves_healthy_lane_progress_and_records_decision():
    source = ROTATE.read_text(encoding="utf-8")
    assert "x[6],x[0],-x[5],x[1],x[2]" in source
    assert "LIVE_IMPACT_PRIORITY|" in source
    assert "LIVE_IMPACT_REJECTED|" in source
