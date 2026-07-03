"""Tests for SKSeed validation in the memory store flow.

Covers the store-flow guard ``validate_seed_for_store`` (schema + size/type
constraints) and its wiring into ``ingest_document`` so malformed seeds are
rejected with a clear error before they reach ``MemoryStore.snapshot``.
"""

from __future__ import annotations

import pytest

from skcapstone.cli.skseed import (
    MAX_SEED_KEY_CLAIMS,
    MAX_SEED_SUMMARY_CHARS,
    SeedValidationError,
    ingest_document,
    validate_seed_for_store,
)


def _valid_seed() -> dict:
    """A minimal well-formed standard-format seed."""
    return {
        "seed_id": "doc-example",
        "version": "1.0",
        "creator": {"model": "skseed-ingest", "instance": "cli"},
        "experience": {
            "summary": "A concise, non-empty experience summary.",
            "key_claims": ["claim one", "claim two"],
        },
        "germination": {"prompt": "Consider this document."},
        "metadata": {"tags": ["seed", "document"]},
    }


# ---------------------------------------------------------------------------
# validate_seed_for_store — happy path
# ---------------------------------------------------------------------------

def test_valid_seed_passes_and_returns_result():
    """A well-formed seed validates and returns the result dict."""
    result = validate_seed_for_store(_valid_seed())
    assert result["valid"] is True
    assert result["errors"] == []
    assert "seed_id" in result["fields"]


def test_cloud9_format_seed_passes():
    """A Cloud9-format seed (seed_metadata block) also validates."""
    seed = {
        "seed_metadata": {"seed_id": "c9-1", "version": "2.0"},
        "experience_summary": {"narrative": "Something happened."},
        "germination_prompt": "Reflect.",
    }
    result = validate_seed_for_store(seed)
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# validate_seed_for_store — rejections with clear errors
# ---------------------------------------------------------------------------

def test_missing_required_field_rejected():
    """A seed missing seed_id is rejected with a clear, specific error."""
    seed = _valid_seed()
    del seed["seed_id"]
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(seed)
    assert "seed_id" in str(exc.value)
    assert any("seed_id" in e for e in exc.value.errors)


def test_empty_experience_summary_rejected():
    """An empty experience.summary is a hard error."""
    seed = _valid_seed()
    seed["experience"]["summary"] = "   "
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(seed)
    assert any("summary" in e for e in exc.value.errors)


def test_non_dict_input_rejected():
    """A non-object top-level value is rejected."""
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(["not", "a", "seed"])  # type: ignore[arg-type]
    assert any("object" in e.lower() for e in exc.value.errors)


# ---------------------------------------------------------------------------
# validate_seed_for_store — size/type constraints (edge cases)
# ---------------------------------------------------------------------------

def test_oversized_summary_rejected():
    """A summary above the store limit is rejected."""
    seed = _valid_seed()
    seed["experience"]["summary"] = "x" * (MAX_SEED_SUMMARY_CHARS + 1)
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(seed)
    assert any("too large" in e for e in exc.value.errors)


def test_summary_at_limit_passes():
    """A summary exactly at the limit is accepted (boundary)."""
    seed = _valid_seed()
    seed["experience"]["summary"] = "x" * MAX_SEED_SUMMARY_CHARS
    result = validate_seed_for_store(seed)
    assert result["valid"] is True


def test_too_many_key_claims_rejected():
    """More key_claims than allowed is rejected."""
    seed = _valid_seed()
    seed["experience"]["key_claims"] = ["c"] * (MAX_SEED_KEY_CLAIMS + 1)
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(seed)
    assert any("key_claims" in e for e in exc.value.errors)


def test_tags_wrong_type_rejected():
    """metadata.tags of the wrong type is rejected."""
    seed = _valid_seed()
    seed["metadata"]["tags"] = "seed,document"  # str, not list
    with pytest.raises(SeedValidationError) as exc:
        validate_seed_for_store(seed)
    assert any("tags" in e and "list" in e for e in exc.value.errors)


def test_error_is_a_valueerror():
    """SeedValidationError subclasses ValueError so existing handlers catch it."""
    assert issubclass(SeedValidationError, ValueError)


# ---------------------------------------------------------------------------
# ingest_document — validation wired BEFORE the store
# ---------------------------------------------------------------------------

class _FakeMemory:
    id = "mem-123"


class _FakeStore:
    def __init__(self):
        self.calls = 0

    def snapshot(self, **kwargs):
        self.calls += 1
        return _FakeMemory()


def test_ingest_valid_document_stores(tmp_path, monkeypatch):
    """A valid document passes validation and reaches the store."""
    doc = tmp_path / "note.txt"
    doc.write_text(
        "# Title Line\n\nThis is a reasonably long body of text that "
        "should extract a couple of key claims. Here is another sentence "
        "with enough length to count as a claim for the heuristic.",
        encoding="utf-8",
    )
    store = _FakeStore()
    monkeypatch.setattr(
        "skcapstone.cli.skseed._get_memory_store", lambda: store
    )
    result = ingest_document(source=str(doc))
    assert result["memory_id"] == "mem-123"
    assert store.calls == 1


def test_ingest_rejects_invalid_seed_before_store(tmp_path, monkeypatch):
    """If the generated seed is invalid, the store is never called."""
    doc = tmp_path / "note.txt"
    doc.write_text("Some extractable content that is long enough.", encoding="utf-8")

    store = _FakeStore()
    monkeypatch.setattr(
        "skcapstone.cli.skseed._get_memory_store", lambda: store
    )

    # Force the generated seed to be malformed (missing required seed_id).
    def _bad_seed(*args, **kwargs):
        return {"version": "1.0", "experience": {"summary": "x"}}

    monkeypatch.setattr(
        "skcapstone.cli.skseed._generate_seed_json", _bad_seed
    )

    with pytest.raises(SeedValidationError):
        ingest_document(source=str(doc))
    assert store.calls == 0, "store must not be called for an invalid seed"
