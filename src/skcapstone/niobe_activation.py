"""Fail-closed validation for an estate's Niobe transition record.

The record authorizes a dispatcher that launches agent runs, so every check
here is an authorization boundary. What changed for estate portability is
*where* the expected values come from, not how strictly they are enforced:
the operator and the product scope now come from the estate's own
configuration (:mod:`skcapstone.estate`), and the host comes from the machine
actually executing rather than from a literal in this file.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .estate import EstateConfigError, EstateProfile, load_estate_profile, local_host

SCHEMA = "skfleet.niobe-activation/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CARD_ID = re.compile(r"[0-9a-z][0-9a-z._-]{0,63}")
ALLOWED_ACTIONS = frozenset({"claim", "release", "launch", "stop", "reassign"})
FORBIDDEN_ACTIONS = frozenset({"merge", "deploy", "application_actuation", "external_dispatch"})
LIVE_UNIT = "skfleet-niobe-live.timer"
ROLLBACK_ACTION = "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer"


class ActivationError(ValueError):
    """The transition record cannot authorize live Niobe mutation."""


def verify_card_fence(home: Path | str, card_id: str, card_revision: str) -> None:
    """Prove the activation names a real card at exactly the stated revision.

    This replaces the previous single hardcoded card id. The question is no
    longer "is this the one card we shipped" but "does the card this record
    references exist in this estate, and is its content still the revision the
    record was minted against". A stale card fails, a missing card fails, and
    a card id that could escape the cards directory fails.

    Args:
        home: The estate home holding ``cards/<card_id>/core.json``.
        card_id: The card the activation references.
        card_revision: The sha256 the activation was minted against.

    Raises:
        ActivationError: On a malformed id or revision, a missing card, or a
            card whose current content no longer hashes to ``card_revision``.
    """
    if not _CARD_ID.fullmatch(card_id):
        raise ActivationError("activation card id is malformed")
    if not _SHA256.fullmatch(card_revision):
        raise ActivationError("activation card fence is invalid")
    core = Path(home) / "cards" / card_id / "core.json"
    if not core.is_file():
        raise ActivationError("activation card is missing")
    if hashlib.sha256(core.read_bytes()).hexdigest() != card_revision:
        raise ActivationError("activation card revision is stale")


@dataclass(frozen=True)
class NiobeActivation:
    """One parsed Niobe activation record.

    Attributes:
        decision_id: Identifier of the operator decision that minted this.
        card_id: The approval card this activation references.
        card_revision: The sha256 of that card's core at minting time.
        host: The single host this activation authorizes.
        live_unit: The systemd timer this activation turns live.
        product_scope: Products the activated seat may act on.
        allowed_actions: The bounded fleet action set.
        expires_at: Timezone-aware expiry.
        authorized_by: The operator who authorized the transition.
        rollback_owner: Who owns the rollback.
        rollback_action: The exact rollback the decision committed to.
    """

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

    def validate(
        self,
        *,
        home: Path | str,
        estate: EstateProfile,
        host: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Refuse unless every fence holds for this estate on this machine.

        Args:
            home: Estate home, used to prove the referenced card exists.
            estate: The estate's own authority facts (operator, product scope).
            host: The running host. ``None`` asks the machine itself, which is
                the production path; tests inject a value.
            now: Clock override for expiry, for tests.

        Raises:
            ActivationError: On any failed fence. Nothing here degrades to a
                warning.
        """
        # local_host() normalises only the MACHINE (it strips any domain
        # suffix). The declared host is compared after strip/lower only, so a
        # record naming "chiap08.somewhere.else" does not match a machine
        # named chiap08.
        machine = local_host(host)
        if self.host.strip().lower() != machine:
            raise ActivationError(
                f"activation authorizes host {self.host.strip().lower() or '(unset)'} "
                f"but this machine is {machine}"
            )
        verify_card_fence(home, self.card_id, self.card_revision)
        if self.live_unit != LIVE_UNIT:
            raise ActivationError("activation live unit is invalid")
        if self.product_scope != estate.product_scope:
            raise ActivationError("activation product scope is not this estate's lifecycle scope")
        if self.allowed_actions != ALLOWED_ACTIONS:
            raise ActivationError("activation actions are not the bounded Niobe fleet set")
        if self.expires_at.tzinfo is None:
            raise ActivationError("activation expiry must include a timezone")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if self.expires_at.astimezone(timezone.utc) <= current:
            raise ActivationError("activation has expired")
        if self.authorized_by.strip().lower() != estate.operator:
            raise ActivationError(
                f"activation requires {estate.operator} authorization for this estate"
            )
        if (
            not self.decision_id.strip()
            or not self.rollback_owner.strip()
            or not self.rollback_action.strip()
        ):
            raise ActivationError("decision and rollback fields are required")
        if self.rollback_action != ROLLBACK_ACTION:
            raise ActivationError("activation rollback action is invalid")


def _timestamp(value: object) -> datetime:
    """Parse a required timezone-aware ISO timestamp, failing closed."""
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
    value: Mapping[str, object],
    *,
    home: Path | str,
    estate: EstateProfile | None = None,
    host: str | None = None,
    now: datetime | None = None,
) -> NiobeActivation:
    """Parse and fully validate one activation record.

    Args:
        value: The decoded activation JSON.
        home: The estate home the record lives in. Required: the card fence
            cannot be proved without it, and an unfenced parse would be a
            weaker gate than the hardcoded one it replaces.
        estate: Estate authority facts. Loaded from ``home`` when omitted.
        host: The running host. ``None`` asks the machine itself.
        now: Clock override for expiry, for tests.

    Returns:
        The validated activation.

    Raises:
        ActivationError: On any structural or authorization failure, including
            an estate whose own configuration cannot name an operator.
    """
    if value.get("schema") != SCHEMA or value.get("state") != "active":
        raise ActivationError("activation is not an active v1 operator decision")
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
    if estate is None:
        try:
            estate = load_estate_profile(home)
        except EstateConfigError as exc:
            raise ActivationError(f"estate cannot authorize activation: {exc}") from exc
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
    activation.validate(home=home, estate=estate, host=host, now=now)
    return activation
