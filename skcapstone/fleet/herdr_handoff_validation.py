"""Bounded input and exact card authority for the local Herdr adapter."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock, explicit_creation_request_digest

from ..jarvis_emergency import authorize_coord_mutation
from ..seat_boundaries import Action
from ..source_binding import source_binding_meta

_PACKET = {"assignment_id", "helper_id", "claim_revision", "target", "route", "expected_output"}
_TARGET = {"pane_id", "agent_name", "agent_kind", "cwd"}
_IDENTITY = ("pane_id", "workspace_id", "tab_id", "terminal_id", "agent_name", "agent_kind", "cwd")
_RECEIPT = {
    "assignment_id",
    "packet_sha256",
    "helper_id",
    "claim_revision",
    "base_revision",
    "cwd",
}
_ROUTES = ("sk-s", "sk-m", "sk-l", "sk-xl")


class HandoffError(ValueError):
    """Authority, identity, delivery or custody could not be established."""


def _text(value, name, maximum=4096):
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise HandoffError(f"invalid {name}")
    return value


def _identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", _text(value, "assignment_id")):
        raise HandoffError("invalid assignment_id")
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HandoffError("duplicate JSON key")
        result[key] = value
    return result


def _regular(path, limit=32768):
    """Read one bounded unchanged regular file without following symlinks."""
    path = Path(path).absolute()

    def open_file():
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            return os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        finally:
            os.close(directory)

    with os.fdopen(open_file(), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise HandoffError("regular file required")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())

    def identity(s):
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    with os.fdopen(open_file(), "rb") as current:
        current_info = os.fstat(current.fileno())
    if identity(before) != identity(after) or identity(after) != identity(current_info):
        raise HandoffError("file changed during read")
    if len(data) > limit:
        raise HandoffError("bounded input exceeded")
    return data


def load_json(path: Path):
    """Load strict bounded JSON from a regular file, rejecting duplicate keys."""
    try:
        return json.loads(_regular(path), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffError("cannot read strict bounded JSON") from exc


def _source(card):
    values = {}
    for key in ("repository", "base_ref", "base_revision"):
        candidates = []
        for mapping in (card.meta, card.links):
            value = mapping.get(key)
            if value is not None:
                value = _text(value, key)
                candidates.append(value.lower() if key == "base_revision" else value)
        if not candidates or len(set(candidates)) != 1:
            raise HandoffError("source binding missing or conflicting")
        values[key] = candidates[0]
    return source_binding_meta(["source-only"], **values)


def _git(workspace, *args):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise HandoffError("workspace Git proof failed")
    return result.stdout.strip()


def _bounded_copy(value):
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode()) > 32768:
        raise HandoffError("bounded packet or receipt exceeded")
    return json.loads(encoded)


@contextmanager
def authority(home, actor, packet, *, delivery=False):
    cards = CardStore(home)
    helper = cards.fold(packet["helper_id"])
    parent_id = helper.meta.get("helper_parent_id") if helper else None
    if (
        not isinstance(parent_id, str)
        or not re.fullmatch(r"[0-9a-f]{8}", parent_id)
        or parent_id == packet["helper_id"]
    ):
        raise HandoffError("card is not an owner helper")
    with card_mutation_lock(home, parent_id), card_mutation_lock(home, packet["helper_id"]):
        helper, parent = cards.fold(packet["helper_id"]), cards.fold(parent_id)
        for card in (helper, parent):
            if (
                card is None
                or not card.owner
                or card.archived
                or card.meta.get("voided")
                or card.meta.get("claim_conflicts")
                or card.status.value not in {"ready", "doing"}
                or set(card.labels) & {"do-not-claim", "integration_hold", "integration-hold"}
            ):
                raise HandoffError("current active unconflicted custody required")
        if (
            helper.meta.get("_claim_revision") != packet["claim_revision"]
            or helper.meta.get("helper_parent_id") != parent.id
            or helper.meta.get("helper_parent_claim_revision")
            != parent.meta.get("_claim_revision")
        ):
            raise HandoffError("claim or parent generation changed")
        if actor not in ({parent.owner} if delivery else {parent.owner, helper.owner}):
            raise HandoffError("current parent or helper owner required")
        if delivery:
            authorize_coord_mutation(actor, Action.LAUNCH, helper.id, None, None)
        digest = explicit_creation_request_digest(
            {
                "title": parent.title,
                "description": parent.description,
                "acceptance_criteria": list(parent.acceptance_criteria),
            }
        )
        if helper.meta.get("helper_parent_contract_sha256") != digest:
            raise HandoffError("parent contract changed")
        required = {label for label in parent.labels if not label.startswith("parent-")}
        if any(
            label.lower() == "review" or label.lower().startswith("seat-")
            for label in helper.labels
        ):
            raise HandoffError("governed review seats are not source helpers")
        if not (required | {"owner-helper", "source-only", f"parent-{parent.id}"}).issubset(
            helper.labels
        ):
            raise HandoffError("helper restrictions changed")
        if set(helper.dependencies) != set(parent.dependencies):
            raise HandoffError("helper dependencies changed")
        for dependency in helper.dependencies:
            dep = cards.fold(dependency)
            if dep is None or dep.status.value != "done" or dep.meta.get("voided"):
                raise HandoffError("unfinished helper dependency")
        source = _source(helper)
        if source != _source(parent):
            raise HandoffError("parent source changed")
        if packet["expected_output"] != helper.meta.get("helper_objective"):
            raise HandoffError("expected output differs from helper objective")
        allowed = helper.meta.get("helper_allowed_paths")
        if not isinstance(allowed, list) or len(allowed) > 64:
            raise HandoffError("invalid helper allowed paths")
        for value in allowed:
            path = Path(_text(value, "allowed path"))
            if (
                path.is_absolute()
                or str(path) != value
                or value == "."
                or any(c in value for c in "*?[]")
                or any(p in {"..", ".git"} for p in path.parts)
            ):
                raise HandoffError("unsafe helper allowed path")
        for card in (helper, parent):
            for key, expected in (
                ("route", packet["route"]),
                ("logical_route", packet["route"]),
                ("agent_kind", packet["target"]["agent_kind"]),
            ):
                for fields in (card.meta, card.links):
                    if fields.get(key) is not None and fields[key] != expected:
                        raise HandoffError("explicit route or agent-kind restriction differs")
        contract = {
            "helper_owner": helper.owner,
            "parent_owner": parent.owner,
            "parent_id": parent.id,
            "parent_claim_revision": parent.meta["_claim_revision"],
            "parent_contract_sha256": digest,
            **source,
            "allowed_paths": allowed,
            "parent_labels": sorted(parent.labels),
            "parent_dependencies": sorted(parent.dependencies),
            "helper_contract": {
                "title": helper.title,
                "description": helper.description,
                "criteria": helper.acceptance_criteria,
                "labels": sorted(helper.labels),
                "dependencies": sorted(helper.dependencies),
                "verification": helper.meta.get("helper_verification"),
                "coordinator_host": helper.meta.get("helper_coordinator_host"),
            },
        }
        yield contract
