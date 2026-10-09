from pathlib import Path


def test_route_snapshot_refresh_precedes_lane_health_selection():
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    source = script.read_text(encoding="utf-8")
    selection = source.index("while _i<len(owned) and _i<len(_candidate_scan):")
    refresh = source.index("_refresh_review_route_snapshot_if_stale()", selection)
    health = source.index("_card_lane_health=", selection)

    assert selection < refresh < health


def test_route_snapshot_refresh_precedes_attempt_admission_health():
    """The admission loop also refreshes before resolving lane health.

    Admission runs minutes after the candidate scan, so without this every
    pick past the snapshot TTL was skipped as route-snapshot-stale or
    no-compatible-healthy-lane:glm while the gateway was healthy.
    """
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    source = script.read_text(encoding="utf-8")
    attempt = source.index(
        "for _pick_index,(_LANE,(_,_,cid,core,_labels,_nb)) in enumerate(picks):"
    )
    refresh = source.index("_refresh_review_route_snapshot_if_stale()", attempt)
    health = source.index('_attempt_health={lane["name"]:_health_for(', attempt)
    skip = source.index("SKIPPED_ATTEMPT_ADMISSION", attempt)

    assert attempt < refresh < health < skip
