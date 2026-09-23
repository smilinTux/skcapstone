"""Governed elastic reviewer seat profile loading and validation.

This module is the source of truth adapter between the shipped
``data/reviewer-profiles.json`` registry and the fleet scaling logic. It is
deliberately read-only: it loads, validates, and exposes profiles but never
launches, claims, or mutates anything. Deployment is out of scope until an
exact deployment card exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "skfleet.reviewer-profiles/v1"
ROUTING_SEAT = "link"
READ_ONLY_TOOLS = frozenset({"git", "python", "pytest", "ruff", "sha256sum"})
_SEAT_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]*$")


class ReviewerProfileError(ValueError):
    """The reviewer profile registry violates the governed seat contract."""


@dataclass(frozen=True)
class ReviewerSeatProfile:
    """One immutable reviewer seat identity with its governed boundaries."""

    identity: str
    role: str
    activation_state: str
    card_label: str
    cadence_seconds: int
    timeout_seconds: int
    model_route: str
    model_profile: str
    mailbox: Mapping[str, Any]
    owns: tuple[str, ...]
    denies: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    recurring_responsibility: str

    def can_review(self) -> bool:
        """Return True when this seat is an active bounded reviewer."""
        return self.activation_state == "active_bounded" and "independent_review" in self.owns

    def tool_allowed(self, tool: str) -> bool:
        """Return True only for tools on this seat's read-only allowlist."""
        return tool in self.tool_allowlist and tool in READ_ONLY_TOOLS


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewerProfileError(f"{name} must be an object")
    return value


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewerProfileError(f"{name} must be a non-empty string")
    return value


def _parse_seat(identity: str, row: Mapping[str, Any]) -> ReviewerSeatProfile:
    parsed = ReviewerSeatProfile(
        identity=identity,
        role=_require_str(row.get("role"), f"seat {identity} role"),
        activation_state=_require_str(
            row.get("activation_state"), f"seat {identity} activation_state"
        ),
        card_label=_require_str(row.get("card_label"), f"seat {identity} card_label"),
        cadence_seconds=int(row.get("cadence_seconds", 0)),
        timeout_seconds=int(row.get("timeout_seconds", 0)),
        model_route=_require_str(row.get("model_route"), f"seat {identity} model_route"),
        model_profile=_require_str(
            row.get("model_profile"), f"seat {identity} model_profile"
        ),
        mailbox=dict(_require_mapping(row.get("mailbox"), f"seat {identity} mailbox")),
        owns=tuple(
            _require_str(item, f"seat {identity} owns entry")
            for item in row.get("owns", [])
        ),
        denies=tuple(
            _require_str(item, f"seat {identity} denies entry")
            for item in row.get("denies", [])
        ),
        tool_allowlist=tuple(
            _require_str(item, f"seat {identity} tool_allowlist entry")
            for item in row.get("tool_allowlist", [])
        ),
        recurring_responsibility=_require_str(
            row.get("recurring_responsibility"), f"seat {identity} recurring_responsibility"
        ),
    )
    if parsed.cadence_seconds <= 0 or parsed.timeout_seconds <= 0:
        raise ReviewerProfileError(f"seat {identity} cadence and timeout must be positive")
    if not _SEAT_NAME.fullmatch(parsed.identity):
        raise ReviewerProfileError(f"seat identity {parsed.identity!r} is not a valid seat name")
    if not parsed.card_label.startswith("seat-"):
        raise ReviewerProfileError(f"seat {identity} card_label must start with 'seat-'")
    if parsed.mailbox.get("mail_is_authority") is not False:
        raise ReviewerProfileError(f"seat {identity} mailbox must not be an authority")
    if parsed.mailbox.get("write") is True and parsed.mailbox.get("automatic_ack") is True:
        raise ReviewerProfileError(f"seat {identity} mailbox must not automatically ack")
    if not parsed.tool_allowlist or set(parsed.tool_allowlist) - READ_ONLY_TOOLS:
        raise ReviewerProfileError(
            f"seat {identity} tool allowlist must be a non-empty subset of the "
            "read-only tool set"
        )
    if ROUTING_SEAT in parsed.owns:
        raise ReviewerProfileError(f"seat {identity} must not own routing actions")
    if "reviewer_assignment" not in parsed.denies:
        raise ReviewerProfileError(
            f"seat {identity} must deny reviewer_assignment to keep Link the sole router"
        )
    if "self_review" not in parsed.denies:
        raise ReviewerProfileError(f"seat {identity} must deny self_review")
    return parsed


def load_reviewer_profiles(source: Path | None = None) -> dict[str, Any]:
    """Load and fail closed on the shipped reviewer profile registry.

    Args:
        source: Optional explicit registry path. Defaults to the packaged
            ``data/reviewer-profiles.json`` shipped with skcapstone.

    Returns:
        The parsed registry document.

    Raises:
        ReviewerProfileError: The registry is missing, malformed, or violates
            the governed reviewer seat contract.
    """
    if source is not None:
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ReviewerProfileError(f"reviewer profile registry unreadable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ReviewerProfileError(
                f"reviewer profile registry is not valid JSON: {exc}"
            ) from exc
    else:
        resource = files("skcapstone").joinpath("data/reviewer-profiles.json")
        try:
            document = json.loads(resource.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewerProfileError(f"packaged reviewer profiles unreadable: {exc}") from exc
    _validate_registry(document)
    return document


def _validate_registry(document: Any) -> None:
    registry = _require_mapping(document, "registry")
    if registry.get("schema") != SCHEMA:
        raise ReviewerProfileError("unsupported reviewer profile schema")
    if registry.get("routing_seat") != ROUTING_SEAT:
        raise ReviewerProfileError("link must remain the sole routing seat")
    reviewing = registry.get("reviewing_seats")
    if not isinstance(reviewing, list) or not reviewing:
        raise ReviewerProfileError("reviewing_seats must be a non-empty list")
    if ROUTING_SEAT in reviewing:
        raise ReviewerProfileError("the routing seat must never appear as a reviewing seat")
    seats = _require_mapping(registry.get("seats"), "seats")
    if set(seats) != set(reviewing):
        raise ReviewerProfileError("seats must exactly match reviewing_seats")
    identities = set()
    labels = set()
    mailboxes = set()
    for identity, row in seats.items():
        profile = _parse_seat(identity, _require_mapping(row, f"seat {identity}"))
        identities.add(profile.identity)
        labels.add(profile.card_label)
        mailboxes.add(str(profile.mailbox.get("address", "")))
    if len(identities) != len(seats) or len(labels) != len(seats):
        raise ReviewerProfileError("reviewer seat identities and card labels must be unique")
    if len(mailboxes) != len(seats) or "" in mailboxes:
        raise ReviewerProfileError("each reviewing seat must have a distinct non-empty mailbox")
    reserved = _require_mapping(registry.get("reserved_capacity"), "reserved_capacity")
    missing = set(reviewing) - set(reserved)
    if missing:
        raise ReviewerProfileError(f"reserved capacity missing for seats: {sorted(missing)}")
    for seat, value in reserved.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ReviewerProfileError(f"reserved capacity for {seat} must be a non-negative int")
    recovery = _require_mapping(registry.get("recovery"), "recovery")
    for key in ("max_claims_per_seat",):
        value = recovery.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ReviewerProfileError(f"recovery.{key} must be a positive int")


def reviewer_seat_profiles(source: Path | None = None) -> dict[str, ReviewerSeatProfile]:
    """Return the validated reviewing seat profiles keyed by identity."""
    registry = load_reviewer_profiles(source)
    return {
        identity: _parse_seat(identity, row)
        for identity, row in registry["seats"].items()
    }
