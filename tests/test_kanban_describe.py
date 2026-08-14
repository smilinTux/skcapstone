"""The board can edit a card's title/description (SPE P3.1, card be2e849a).

`describe` landed in skcoord as a fold action and in skcapstone as
`coord describe`, but the dashboard's own `_MUTATIONS` allowlist rejected it,
so the one surface a human actually edits cards from could not use it. That
allowlist is a fail-closed gate doing its job; this adds the action to it.

Same invariants as the CLI verb: only the fields actually supplied are written,
an empty string is a deliberate clear, `core.json` is never rewritten, and the
edit is one appended event attributed to its actor.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skdashboard.dashboard_kanban import apply_mutation


def _store(tmp: Path) -> CardStore:
    s = CardStore(tmp)
    s.create(CardCore(id="c1", title="Original title", description="Original body"))
    return s


def test_describe_updates_the_description():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _store(tmp)
        out = apply_mutation(tmp, "c1", "describe", "lumina", description="tightened")
        assert out.get("ok") is True
        assert out["card"]["description"] == "tightened"
        assert out["card"]["title"] == "Original title"


def test_describe_updates_the_title():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _store(tmp)
        out = apply_mutation(tmp, "c1", "describe", "lumina", title="Fixed title")
        assert out["card"]["title"] == "Fixed title"
        assert out["card"]["description"] == "Original body"


def test_describe_requires_at_least_one_field():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store = _store(tmp)
        out = apply_mutation(tmp, "c1", "describe", "lumina")
        assert out.get("error")
        assert store._read_events("c1") == []  # nothing appended on a refusal


def test_describe_can_clear_the_description():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _store(tmp)
        out = apply_mutation(tmp, "c1", "describe", "lumina", description="")
        assert out["card"]["description"] == ""


def test_describe_never_rewrites_core_json():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store = _store(tmp)
        core = store.cards_dir / "c1" / "core.json"
        before = core.read_text(encoding="utf-8")
        apply_mutation(tmp, "c1", "describe", "lumina", title="New", description="New body")
        assert core.read_text(encoding="utf-8") == before
        assert json.loads(before)["title"] == "Original title"


def test_describe_event_is_attributed_to_the_actor():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        store = _store(tmp)
        apply_mutation(tmp, "c1", "describe", "chef", description="edited")
        events = store._read_events("c1")
        assert [e["action"] for e in events] == ["describe"]
        assert events[0]["writer"] == "chef"
        assert events[0]["description"] == "edited"
        assert "title" not in events[0]  # untouched field is not written


def test_describe_on_a_missing_card_is_an_error():
    with tempfile.TemporaryDirectory() as td:
        out = apply_mutation(Path(td), "nope", "describe", "lumina", description="x")
        assert out.get("error") == "card not found"
