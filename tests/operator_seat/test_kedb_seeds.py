"""Seed the ITIL KEDB with the operator adapters' known errors (R2.12/R2.14).

Every kedb_ref an app adapter declares must resolve to a real KEDB entry. These
tests prove seeding creates one entry per referenced id (with a real workaround),
that re-running is idempotent, and (the drift guard) that every kedb_ref across
the registered app adapters has a corresponding seed so the set can never silently
drift.
Storage is injected on tmp_path: no real filesystem is touched.
"""

from __future__ import annotations

from skcapstone.itil import ITILManager
from skcapstone.operator_seat import kedb_seeds, registration


def _itil(tmp_path) -> ITILManager:
    return ITILManager(tmp_path / "skcapstone-home")


# --- referenced ids from the adapters ----------------------------------------


def referenced_kedb_refs() -> set[str]:
    """Walk every registered app adapter's explain() actions and collect the full
    set of declared kedb_refs. This is the authority the drift guard checks."""
    refs: set[str] = set()
    for meta in registration.APP_REGISTRY.values():
        payload = meta["explain"]()
        for action in payload.get("actions", []):
            for ref in action.get("kedb_refs", []):
                refs.add(ref)
    return refs


# --- seeding ------------------------------------------------------------------


def test_seed_creates_one_entry_per_referenced_id(tmp_path):
    itil = _itil(tmp_path)
    created = kedb_seeds.seed_operator_kedb(tmp_path, itil=itil)

    # Every referenced id got a persisted KEDBEntry with a non-empty workaround.
    by_id = {e.id: e for e in itil._load_kedb()}
    for ref in referenced_kedb_refs():
        assert ref in by_id, f"kedb_ref {ref} was not seeded"
        entry = by_id[ref]
        assert entry.id == ref
        assert entry.workaround.strip(), f"{ref} seeded with an empty workaround"
        assert entry.symptoms, f"{ref} seeded with no symptoms"
        assert entry.title.strip()

    assert set(created) == referenced_kedb_refs()


def test_seed_is_idempotent_no_duplicates(tmp_path):
    itil = _itil(tmp_path)
    first = kedb_seeds.seed_operator_kedb(tmp_path, itil=itil)
    assert first, "first run should create entries"

    # Second run creates nothing and never duplicates.
    second = kedb_seeds.seed_operator_kedb(tmp_path, itil=itil)
    assert second == []

    entries = itil._load_kedb()
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids)), "duplicate KEDB entries after re-seeding"
    assert len(entries) == len(kedb_seeds.OPERATOR_KEDB_SEEDS)

    # One file per entry on disk (no shadow copies).
    assert len(list(itil.kedb_dir.glob("*.json"))) == len(kedb_seeds.OPERATOR_KEDB_SEEDS)


def test_existing_entry_is_left_as_is(tmp_path):
    itil = _itil(tmp_path)
    itil.ensure_dirs()
    sample = next(iter(referenced_kedb_refs()))
    # A pre-existing entry with a hand-edited workaround must survive seeding.
    itil.create_kedb_entry(
        title="hand authored",
        symptoms=["pre-existing"],
        workaround="do not touch this",
        entry_id=sample,
    )

    created = kedb_seeds.seed_operator_kedb(tmp_path, itil=itil)

    assert sample not in created  # skipped, not recreated
    by_id = {e.id: e for e in itil._load_kedb()}
    assert by_id[sample].workaround == "do not touch this"
    assert by_id[sample].title == "hand authored"


# --- drift guard --------------------------------------------------------------


def test_every_adapter_kedb_ref_is_seeded():
    """DRIFT GUARD: every kedb_ref declared across the registered app adapters must
    have a seed. If a new adapter action names a new ke-* id without a seed here, this
    fails, so the KEDB can never silently fall behind the adapters."""
    missing = referenced_kedb_refs() - kedb_seeds.SEEDED_IDS
    assert not missing, f"adapter kedb_refs with no KEDB seed: {sorted(missing)}"


def test_seed_ids_have_no_dangling_extras():
    """Every seeded id is actually referenced by an adapter (no orphan seeds)."""
    extras = kedb_seeds.SEEDED_IDS - referenced_kedb_refs()
    assert not extras, f"seeded ids no adapter references: {sorted(extras)}"


def test_seeded_ids_constant_matches_seed_table():
    assert kedb_seeds.SEEDED_IDS == frozenset(
        s["id"] for s in kedb_seeds.OPERATOR_KEDB_SEEDS
    )
