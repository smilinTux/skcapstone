from pathlib import Path


def test_builder_candidates_are_ordered_by_offer_memory_without_a_profile_store():
    """Every card is equally offerable: ordering is offer memory only, never a profile file."""
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    source = script.read_text(encoding="utf-8")
    loop = source.index("for _candidate in tuple(_builder_candidates)[:MAX_CANDIDATE_SCAN]:")
    ordering = source.rindex("_builder_candidates = _offer_memory.order(", 0, loop)
    window = source[ordering:loop]

    assert "test-profiles" not in source
    assert "lambda _cid: True" in window
