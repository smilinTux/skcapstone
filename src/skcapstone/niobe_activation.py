"""Fail-closed validation for the one Casey-gated Niobe transition record."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

SCHEMA = "skfleet.niobe-activation/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
PRODUCTS = frozenset({"skcapstone", "skdashboard", "skworld"})
ALLOWED_ACTIONS = frozenset({"claim", "release", "launch", "stop", "reassign"})
FORBIDDEN_ACTIONS = frozenset({"merge", "deploy", "application_actuation", "external_dispatch"})
CARD_ID = "c4e7a9b2"
LIVE_UNIT = "skfleet-niobe-live.timer"
ROLLBACK_ACTION = "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer"


class ActivationError(ValueError):
    """The transition record cannot authorize live Niobe mutation."""


@dataclass(frozen=True)
class NiobeActivation:
    decision_id: str
    card_id: str
    card_revision: str
    host: str
    live_unit: str
    product_scope: frozenset[str]
    allowed_actions: frozenset[str]
    expires_at: datetime
    authorized_by: str
    rollback_owner: str
    rollback_action: str

    def validate(self, *, now: datetime | None = None) -> None:
        if self.host != "chiap08":
            raise ActivationError("activation host is not chiap08")
        if self.card_id != CARD_ID or not _SHA256.fullmatch(self.card_revision):
            raise ActivationError("activation card fence is invalid")
        if self.live_unit != LIVE_UNIT:
            raise ActivationError("activation live unit is invalid")
        if self.product_scope != PRODUCTS:
            raise ActivationError("activation product scope is not the three-product lifecycle")
        if self.allowed_actions != ALLOWED_ACTIONS:
            raise ActivationError("activation actions are not the bounded Niobe fleet set")
        if self.expires_at.tzinfo is None:
            raise ActivationError("activation expiry must include a timezone")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.expires_at.astimezone(timezone.utc) <= current:
            raise ActivationError("activation has expired")
        if self.authorized_by.strip().lower() != "casey":
            raise ActivationError("activation requires Casey authorization")
        if (
            not self.decision_id.strip()
            or not self.rollback_owner.strip()
            or not self.rollback_action.strip()
        ):
            raise ActivationError("decision and rollback fields are required")
        if self.rollback_action != ROLLBACK_ACTION:
            raise ActivationError("activation rollback action is invalid")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ActivationError("activation expiry is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationError("activation expiry is malformed") from exc
    if parsed.tzinfo is None:
        raise ActivationError("activation expiry must include a timezone")
    return parsed


def parse_activation(
    value: Mapping[str, object], *, now: datetime | None = None
) -> NiobeActivation:
    if value.get("schema") != SCHEMA or value.get("state") != "active":
        raise ActivationError("activation is not an active v1 Casey decision")
    if value.get("seat") != "niobe" or value.get("card_label") != "seat-niobe":
        raise ActivationError("activation seat is not Niobe")
    product_scope = frozenset(str(item).strip().lower() for item in value.get("product_scope", []))
    allowed = frozenset(str(item).strip().lower() for item in value.get("allowed_actions", []))
    denied = frozenset(str(item).strip().lower() for item in value.get("denied_actions", []))
    if not FORBIDDEN_ACTIONS.issubset(denied):
        raise ActivationError("activation does not explicitly deny forbidden actions")
    rollback = value.get("rollback")
    if not isinstance(rollback, dict):
        raise ActivationError("rollback record is required")
    activation = NiobeActivation(
        decision_id=str(value.get("decision_id") or ""),
        card_id=str(value.get("card_id") or ""),
        card_revision=str(value.get("card_revision") or ""),
        host=str(value.get("host") or ""),
        live_unit=str(value.get("live_unit") or ""),
        product_scope=product_scope,
        allowed_actions=allowed,
        expires_at=_timestamp(value.get("expires_at")),
        authorized_by=str(value.get("authorized_by") or ""),
        rollback_owner=str(rollback.get("owner") or ""),
        rollback_action=str(rollback.get("action") or ""),
    )
    activation.validate(now=now)
    return activation
