from pathlib import Path

from skcapstone.quality_harness import build_report
from skcoord.card_store import CardCore, CardStore


def test_quality_report_is_deterministic_and_separates_evidence(tmp_path: Path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="a1b2c3d4", kind="feature", title="A", description="desc", agent="worker"))
    store.append_event("a1b2c3d4", "admit", "worker", packet_id="p1")
    store.append_event("a1b2c3d4", "verdict", "reviewer", verdict="PASS", independent=True,
                       evidence=[{"key": "artifact_sha256", "value": "abc"}])
    first = build_report(tmp_path)
    second = build_report(tmp_path)
    assert first == second
    row = first["cards"][0]
    assert row["first_independent_pass"] is True
    assert row["evidence"][0]["link"]["key"] == "artifact_sha256"
    assert row["event_sha256"]
    assert first["baseline"]["first_pass_pass_rate"] == 1.0


def test_repeated_defects_produce_bounded_proposal(tmp_path: Path):
    store = CardStore(tmp_path)
    for card_id in ("a1b2c3d4", "b1c2d3e4"):
        store.create(CardCore(id=card_id, kind="bug", title=card_id, description="desc", agent="worker"))
        store.append_event(card_id, "escaped_defect", "worker", escaped=True, defect_class="admission")
    report = build_report(tmp_path)
    assert report["prevention_proposals"] == [{
        "defect_class": "admission", "count": 2, "bounded": True, "duplicate_or_owned": False
    }]
