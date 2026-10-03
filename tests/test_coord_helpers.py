"""Bounded owner assistance through the public CLI and fenced Board API."""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore
from skcoord.coordination import Board

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coord_helpers import build_helper, load_packet, validate_packet

PARENT = "afc16716"
OWNER = "test-owner"
REVISION = "a" * 32
SOURCE = {
    "repository": "https://example.test/team/project.git",
    "base_ref": "main",
    "base_revision": "b" * 40,
}


def _packet():
    """Return an explicit small helper request."""
    return {
        "request_id": "verify-retry",
        "title": "[S] Verify retry seam",
        "objective": "Reproduce the retry failure against the pinned source",
        "criteria": ["Report the reproducer and exact observed result"],
        "allowed_paths": [],
        "verification_commands": ["python -m pytest -q tests/test_retry.py"],
    }


def _parent(tmp_path):
    """Create only isolated synthetic cards, never use the live board."""
    store = CardStore(tmp_path)
    store.create(CardCore(id="deaf1234", title="[S] Existing prerequisite"))
    store.create(
        CardCore(
            id=PARENT,
            title="[L] Parent delivery",
            description="No provider calls or production deployment.",
            initial_owner=OWNER,
            initial_claim_revision=REVISION,
            initial_labels=["source-only", "no-external-action", "parent-dead1234"],
            acceptance_criteria=["The complete integrated journey works"],
            dependencies=["deaf1234"],
            meta=dict(SOURCE),
        )
    )
    return store.fold(PARENT)


def _main():
    """Register the real coordination command without importing the fleet."""

    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _invoke(tmp_path, packet, *extra):
    """Invoke the supported CLI with a packet outside the board home."""
    path = tmp_path.parent / f"{tmp_path.name}-packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return CliRunner().invoke(
        _main(),
        [
            "coord",
            "request-help",
            PARENT,
            "--agent",
            OWNER,
            "--expected-claim-revision",
            REVISION,
            "--packet",
            str(path),
            "--home",
            str(tmp_path),
            *extra,
        ],
    )


def test_build_preserves_parent_and_inherits_exact_contract(tmp_path):
    parent = _parent(tmp_path)
    before = parent.model_dump()
    helper = build_helper(parent, _packet(), OWNER, REVISION)
    assert parent.model_dump() == before
    assert helper.dependencies == ["deaf1234"]
    assert PARENT not in helper.dependencies
    assert helper.tags == [
        "source-only",
        "no-external-action",
        "parent-dead1234",
        "owner-helper",
        f"parent-{PARENT}",
        "sk-s",
    ]
    assert helper.meta["logical_route"] == "sk-s"
    assert {key: helper.meta[key] for key in SOURCE} == SOURCE
    assert helper.meta["helper_parent_claim_revision"] == REVISION
    assert helper.meta["helper_allowed_paths"] == []
    assert parent.description in helper.description
    assert parent.acceptance_criteria[0] in helper.description
    assert helper.created_at == parent.created_at
    assert helper == build_helper(parent, _packet(), OWNER, REVISION)


def test_request_identity_stable_when_contents_change(tmp_path):
    parent = _parent(tmp_path)
    first = build_helper(parent, _packet(), OWNER, REVISION)
    changed = _packet()
    changed["objective"] = "A different output"
    second = build_helper(parent, changed, OWNER, REVISION)
    assert first.id == second.id
    assert first != second


def test_generated_helper_reaches_actual_fleet_pool_admission(tmp_path):
    """A helper must survive the real dispatch ID fence, not just Board.claim."""
    parent = _parent(tmp_path)
    board = Board(tmp_path)
    board.claim_task("dependency-owner", "deaf1234")
    board.complete_task("dependency-owner", "deaf1234")
    helper = build_helper(parent, _packet(), OWNER, REVISION)
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_pool_v2_candidate_allowed", "_pool_v2_dispatchable"}
    ]
    assert len(functions) == 2
    namespace = {"re": re}
    exec(compile(ast.Module(functions, []), str(script), "exec"), namespace)
    admission = {
        "card_id": helper.id,
        "labels": helper.tags,
        "title": helper.title,
        "core": helper.model_dump(mode="json"),
        "claimable": True,
        "reason": "claimable",
        "overlay": {"backoff": False},
        "source_revision": "c" * 64,
    }
    assert re.fullmatch(r"[0-9a-f]{8}", helper.id)
    assert namespace["_pool_v2_candidate_allowed"](admission) is True
    assert namespace["_pool_v2_dispatchable"](admission) is True
    old_shape = {**admission, "card_id": helper.id + "deadbeef"}
    assert namespace["_pool_v2_candidate_allowed"](old_shape) is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("request_id", 12),
        ("request_id", "../bad"),
        ("title", "No size"),
        ("title", "[L] Too large"),
        ("title", "[REVIEW][S] Impersonated reviewer"),
        ("objective", ""),
        ("criteria", "not a list"),
        ("criteria", []),
        ("criteria", [1]),
        ("allowed_paths", ["../escape.py"]),
        ("allowed_paths", ["/absolute/file.py"]),
        ("allowed_paths", ["src/**"]),
        ("allowed_paths", ["."]),
        ("allowed_paths", [".git/config"]),
        ("allowed_paths", ["src", "src/file.py"]),
        ("allowed_paths", ["src/file.py", "src"]),
        ("allowed_paths", ["src//file.py"]),
        ("verification_commands", []),
        ("verification_commands", ["echo a\necho b"]),
    ],
)
def test_packet_rejects_ambiguous_or_unbounded_values(key, value):
    packet = _packet()
    packet[key] = value
    with pytest.raises(ValueError):
        validate_packet(packet)


def test_packet_cannot_override_authority():
    for key in ("repository", "base_revision", "dependencies", "tags", "owner"):
        with pytest.raises(ValueError, match="exactly"):
            validate_packet({**_packet(), key: "override"})


def test_bounded_file_read_rejects_duplicate_and_oversized_json(tmp_path):
    packet = tmp_path / "packet.json"
    packet.write_text('{"request_id":"first","request_id":"second"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_packet(packet)
    packet.write_bytes(b" " * 32769)
    with pytest.raises(ValueError, match="exceeds"):
        load_packet(packet)


@pytest.mark.parametrize(
    "change", ["foreign", "stale", "conflicted", "archived", "review", "nested"]
)
def test_parent_requires_current_unconflicted_source_owner(tmp_path, change):
    parent = _parent(tmp_path)
    if change == "foreign":
        parent.owner = "other-owner"
    elif change == "stale":
        parent.meta["_claim_revision"] = "new-revision"
    elif change == "conflicted":
        parent.meta["claim_conflicts"] = [{"owner": OWNER}]
    elif change == "archived":
        parent.archived = True
    elif change == "review":
        parent.labels.append("review")
    elif change == "nested":
        parent.meta["helper_parent_id"] = "another-parent"
    with pytest.raises(ValueError):
        build_helper(parent, _packet(), OWNER, REVISION)


def test_source_conflict_or_wrong_type_refuses(tmp_path):
    parent = _parent(tmp_path)
    parent.links["base_revision"] = "c" * 40
    with pytest.raises(ValueError, match="conflict"):
        build_helper(parent, _packet(), OWNER, REVISION)
    parent.links.clear()
    parent.meta["base_ref"] = 12
    with pytest.raises(ValueError, match="string"):
        build_helper(parent, _packet(), OWNER, REVISION)


def test_link_only_source_and_restrictions_survive(tmp_path):
    parent = _parent(tmp_path)
    parent.labels.extend(["human-gate", "do-not-claim", "tenant-secret"])
    parent.links.update(SOURCE)
    for name in SOURCE:
        del parent.meta[name]
    helper = build_helper(parent, _packet(), OWNER, REVISION)
    assert {key: helper.meta[key] for key in SOURCE} == SOURCE
    assert {"human-gate", "do-not-claim", "tenant-secret"}.issubset(helper.tags)


@pytest.mark.parametrize("field", ["description", "criteria"])
def test_parent_contract_change_before_locked_creation_refuses(tmp_path, field):
    from skcoord.card_store import explicit_creation_request_digest

    parent = _parent(tmp_path)
    helper = build_helper(parent, _packet(), OWNER, REVISION)
    digest = explicit_creation_request_digest(helper.model_dump(mode="json"))
    store = CardStore(tmp_path)
    if field == "description":
        store.append_event(PARENT, "describe", OWNER, description="Additional source restriction")
    else:
        store.append_event(PARENT, "amend_criteria", OWNER, criteria=["Stricter acceptance"])
    with pytest.raises(ValueError, match="contract"):
        Board(tmp_path).create_helper_task(helper, digest, OWNER, PARENT, REVISION)
    assert store.fold(helper.id) is None


def test_help_names_read_command():
    result = CliRunner().invoke(_main(), ["coord", "--help"])
    assert "coord show <id>" in result.output
    assert "coord describe <id>" not in result.output
