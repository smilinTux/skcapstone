"""Focused reconciliation tests for the two live fleet reaper fixes."""

from __future__ import annotations

import ast
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
HELPERS = runpy.run_path(str(ROOT / "tests" / "test_skfleet_reaper_provenance.py"))
_claim_event = HELPERS["_claim_event"]
_reaper_fixture = HELPERS["_reaper_fixture"]


def _replace_revisionless_claim(tmp_path: Path, owner: str, timestamp: str | None) -> None:
    """Replace the fresh event with a synthetic revisionless claim."""
    event = _claim_event(owner, "unused", timestamp)
    event.pop("claim_revision")
    path = tmp_path / "cards" / "deadbeef" / "events" / "claim.jsonl"
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def _revisionless_fixture(tmp_path: Path, owner: str = "pi-codex-chiap02-deadbeef"):
    """Return the standard dead-worker fixture without a claim revision."""
    return _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner=owner,
        claim_revision=None,
        launch_revision=None,
    )


def _ineffective_namespace(
    path: Path, states: dict[str, str], clock: dict[str, float]
) -> dict[str, object]:
    """Load only the ineffective-state helpers with isolated dependencies."""
    names = {
        "_read_ineffective_raw",
        "_write_ineffective",
        "_load_ineffective",
        "_record_ineffective",
    }
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(functions) == names
    module = ast.Module(body=[functions[name] for name in names], type_ignores=[])
    namespace: dict[str, object] = {
        "INEFFECTIVE_TTL": 24 * 3600,
        "_INEFFECTIVE_PATH": str(path),
        "json": json,
        "lifecycle_state": lambda card: states.get(card, "claimed"),
        "os": os,
        "time": SimpleNamespace(time=lambda: clock["now"]),
    }
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_revisionless_dead_claim_releases_without_revision_flag(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path)
    assert namespace["reap_dead_claims"]() == 1
    assert "--expected-claim-revision" not in released[0]


def test_revisionless_same_owner_new_timestamp_is_preserved(tmp_path: Path) -> None:
    namespace, released, messages = _revisionless_fixture(tmp_path)
    cached = namespace["event_rows"]("deadbeef")
    namespace["event_rows"] = lambda _card: cached
    _replace_revisionless_claim(tmp_path, "pi-codex-chiap02-deadbeef", "2026-08-29T10:00:00+00:00")
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_RECLAIMED|") for message in messages)


def test_revisionless_new_owner_is_preserved(tmp_path: Path) -> None:
    namespace, released, messages = _revisionless_fixture(tmp_path)
    cached = namespace["event_rows"]("deadbeef")
    namespace["event_rows"] = lambda _card: cached
    _replace_revisionless_claim(tmp_path, "pi-glm-chiap03-deadbeef", "2026-08-29T09:00:00+00:00")
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_RECLAIMED|") for message in messages)


def test_revisionless_below_quorum_is_preserved(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path)
    namespace["live_report"] = lambda: (namespace["time"].time(), set(), 2)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_revisionless_running_card_is_preserved(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path)
    namespace["live_report"] = lambda: (namespace["time"].time(), {"deadbeef"}, 3)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_revisionless_claim_inside_grace_is_preserved(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path)
    _, claim_ts, _ = namespace["_claim_identity"](namespace["event_rows"]("deadbeef"))
    namespace["live_report"] = lambda: (claim_ts + namespace["CLAIM_GRACE"] - 1, set(), 3)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_revisionless_ineffective_card_is_preserved(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path)
    namespace["_load_ineffective"] = lambda: {"deadbeef"}
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_revisionless_named_owner_is_preserved(tmp_path: Path) -> None:
    namespace, released, _messages = _revisionless_fixture(tmp_path, owner="jarvis")
    assert namespace["reap_dead_claims"]() == 0
    assert released == []


def test_revisionless_invalid_cached_timestamp_is_preserved(tmp_path: Path) -> None:
    namespace, released, messages = _revisionless_fixture(tmp_path)
    event = _claim_event("pi-codex-chiap02-deadbeef", "unused", None)
    event.pop("claim_revision")
    namespace["event_rows"] = lambda _card: [event]
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_UNPROVEN|") for message in messages)


def test_revisionless_invalid_fresh_timestamp_is_preserved(tmp_path: Path) -> None:
    namespace, released, messages = _revisionless_fixture(tmp_path)
    cached = namespace["event_rows"]("deadbeef")
    namespace["event_rows"] = lambda _card: cached
    _replace_revisionless_claim(tmp_path, "pi-codex-chiap02-deadbeef", None)
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_RECLAIMED|") for message in messages)


def test_revisioned_claim_without_provenance_is_preserved(tmp_path: Path) -> None:
    namespace, released, messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-codex-chiap02-deadbeef",
        claim_revision="revision-1",
        launch_revision=None,
    )
    assert namespace["reap_dead_claims"]() == 0
    assert released == []
    assert any(message.startswith("REAP_UNPROVEN|") for message in messages)


def test_revisioned_claim_uses_live_cli_shape_after_fresh_fence(tmp_path: Path) -> None:
    namespace, released, _messages = _reaper_fixture(
        tmp_path,
        card_id="deadbeef",
        owner="pi-codex-chiap02-deadbeef",
        claim_revision="revision-1",
        launch_revision="revision-1",
    )
    assert namespace["reap_dead_claims"]() == 1
    assert released[0] == [
        "skcapstone",
        "coord",
        "release-claim",
        "deadbeef",
        "--owner",
        "pi-codex-chiap02-deadbeef",
        "--agent",
        "fleet-liveness-reaper",
    ]


def test_legacy_flat_list_is_read_and_pruned(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    path.write_text('{"cards": ["legacy"]}\n', encoding="utf-8")
    namespace = _ineffective_namespace(path, {"legacy": "claimed"}, {"now": 100000.0})
    assert namespace["_load_ineffective"]() == set()
    assert json.loads(path.read_text(encoding="utf-8")) == {"cards": {}}


def test_no_longer_claimed_entry_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    path.write_text('{"cards": {"done": 99999}}\n', encoding="utf-8")
    namespace = _ineffective_namespace(path, {"done": "complete"}, {"now": 100000.0})
    assert namespace["_load_ineffective"]() == set()
    assert json.loads(path.read_text(encoding="utf-8")) == {"cards": {}}


def test_claimed_entry_older_than_ttl_is_retried(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    path.write_text('{"cards": {"old": 13600}}\n', encoding="utf-8")
    namespace = _ineffective_namespace(path, {"old": "claimed"}, {"now": 100000.0})
    assert namespace["_load_ineffective"]() == set()
    assert json.loads(path.read_text(encoding="utf-8")) == {"cards": {}}


def test_fresh_claimed_entry_is_kept_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    path.write_text('{"cards": {"fresh": 13601}}\n', encoding="utf-8")
    namespace = _ineffective_namespace(path, {"fresh": "claimed"}, {"now": 100000.0})
    writes: list[dict[str, float]] = []
    namespace["_write_ineffective"] = lambda entries: writes.append(entries)
    assert namespace["_load_ineffective"]() == {"fresh"}
    assert writes == []


def test_ineffective_state_is_written_atomically_and_sorted(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    namespace = _ineffective_namespace(path, {}, {"now": 100000.0})
    namespace["_write_ineffective"]({"z": 2.0, "a": 1.0})
    assert path.read_text(encoding="utf-8") == '{"cards": {"a": 1.0, "z": 2.0}}'
    assert not Path(str(path) + ".new").exists()


def test_rerecord_preserves_original_first_seen(tmp_path: Path) -> None:
    path = tmp_path / "ineffective.json"
    clock = {"now": 100.0}
    namespace = _ineffective_namespace(path, {}, clock)
    namespace["_record_ineffective"]("deadbeef")
    clock["now"] = 200.0
    namespace["_record_ineffective"]("deadbeef")
    assert json.loads(path.read_text(encoding="utf-8")) == {"cards": {"deadbeef": 100.0}}
