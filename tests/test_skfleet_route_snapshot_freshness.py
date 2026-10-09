from pathlib import Path


def test_route_snapshot_refresh_precedes_lane_health_selection():
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    source = script.read_text(encoding="utf-8")
    selection = source.index("while _i<len(owned) and _i<len(_candidate_scan):")
    refresh = source.index("_refresh_review_route_snapshot_if_stale()", selection)
    health = source.index("_card_lane_health=", selection)

    assert selection < refresh < health
