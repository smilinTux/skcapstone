from pathlib import Path


def test_qualified_builder_candidates_are_offered_first():
    """The deadline must not expire on unqualified cards while a qualified one waits."""
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    source = script.read_text(encoding="utf-8")
    loop = source.index("for _candidate in tuple(_builder_candidates)[:MAX_CANDIDATE_SCAN]:")
    ordering = source.rindex("_qualified_profiles = ", 0, loop)
    window = source[ordering:loop]

    assert '".skcapstone/fleet/test-profiles"' in window
    assert 'key=lambda _c: not (_qualified_profiles / (str(_c[2]) + ".json")).is_file()' in window


def test_qualified_first_ordering_is_stable():
    profiles = {"b"}
    pool = [(0, 0, "a"), (0, 0, "b"), (0, 0, "c"), (0, 0, "d")]
    ordered = sorted(pool, key=lambda c: c[2] not in profiles)
    assert [c[2] for c in ordered] == ["b", "a", "c", "d"]
