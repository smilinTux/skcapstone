"""Unified Consent Plane, Phase 2: persist consent as a durable SPE event.

Design docs:
  - ``skcapstone/docs/specs/2026-08-13-unified-consent-plane-arch.md``
    section 3: "Policy in capauth. Record in skcoord. One operator identity."
    A PEP that receives a PDP allow for a human-consent action persists the
    audit obligation it already receives (:mod:`skdashboard.queue_authz`
    now surfaces it, see ``_default_decide_fn``) as a ``consent.granted``
    event in the store that owns the object. No new store, ever.
  - ``skcapstone/docs/specs/2026-08-14-signed-provenance-envelope-arch.md``
    and its ratified standard, ``sk-standards/standards/
    PROVENANCE_AND_MUTATION_STANDARD.md`` section 1: the envelope this
    module builds (``spe``, ``actor.*``, ``ts``, ``action``, ``target``,
    ``prior``, ``sig.*``) IS the "SPE writer envelope" a consent event must
    carry, so this is a composition of that standard, not a parallel
    provenance shape.

Two front doors write through here today: the dashboard queue-AI PEP
(``dashboard.py::_queue_run``, CardStore-backed) and the change-management
validate/schedule/arm PEPs (``dashboard.py::api_change_*``, ITILManager-backed).
Both stores already own an append-only, per-writer event log
(``skcoord.card_store.CardStore.append_event`` /
``skcoord.itil.ITILManager._append_event``); this module is the one adapter
each front door calls, not a second write path.

Actor resolution: prefers a capauth-verified operator session (the
device-bound, individually revocable identity ``capauth.pairing.
mint_operator_session``/``verify_operator_session`` just landed, presented on
the ``x-operator-token`` header) over the self-asserted ``X-SK-Actor`` header
skdashboard has used since before this card. Neither path is broken by the
other: ``X-SK-Actor`` keeps driving every OTHER actor-facing field in the
existing PEPs exactly as before (``_change_actor``, ``_queue_run``'s
``requester``); this module only resolves the consent envelope's own
``actor.id``, and an unresolved identity is recorded as the literal
``"unattributed"`` (the convention in ``operator_seat/fleet_adapter.py::
resolved_writer_identity``), never a synthesized placeholder.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

#: Envelope wire-format version tag (PROVENANCE_AND_MUTATION_STANDARD.md
#: section 1). Never redefined in place; a shape change mints "spe2".
SPE_VERSION = "spe1"

#: The one registered action verb every consent event carries. The privileged
#: action actually being consented to (agentrun.queue, change.validate, ...)
#: rides in the ``capability`` field alongside it, never overloaded onto this.
CONSENT_ACTION = "consent.granted"

#: Fallback suite id (skcomms.crypto_suites.DEFAULT_SIG_SUITE, softly
#: imported so this module has no hard skcomms dependency - mirrors
#: skcapstone.fleet.signing._default_suite_id's own fallback copy).
_FALLBACK_SUITE_ID = "ed25519-v1"

_HOSTNAME = socket.gethostname()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Actor resolution
# --------------------------------------------------------------------------- #
def resolve_consent_actor(request: Any) -> dict:
    """Resolve the consent envelope's ``actor.id`` for one HTTP request.

    Order of preference, matching the card instructions ("prefer a verified
    subject over the self-asserted X-SK-Actor header where you can, but do
    not break the existing header path"):

    1. A verified capauth operator session on the ``x-operator-token``
       header (``capauth.pairing.verify_operator_session``): the strongest
       identity primitive in the fleet (an HS256 JWT bound to an approved,
       individually revocable device fingerprint). Its subject is
       ``canonical_subject(f"device:{device_fp}")`` - the exact
       ``operator:<fp>`` -> ``device:<fp>`` normalization
       ``capauth.canonical_subject`` documents.
    2. The self-asserted ``X-SK-Actor`` header, canonicalized. Most free-text
       values here (``"operator"``, ``"dashboard"``) do not match the fqid
       grammar and canonicalization fails - that is expected and honest: an
       unverified claim degrades to "unattributed" rather than being
       recorded as if it were a real identity.
    3. ``"unattributed"``.

    Never raises: every capauth call is wrapped, and any failure degrades to
    the next tier rather than blocking the write. Provenance is best-effort
    (same posture as ``fleet_adapter.py::resolved_writer_identity``).

    Returns:
        dict: ``{"id": str, "role": "human", "verified": bool, "session": Optional[str]}``.
        ``verified`` is True only for the operator-session path.
    """
    token = request.headers.get("x-operator-token")
    if token:
        try:
            from capauth import canonical_subject
            from capauth.pairing import verify_operator_session

            session = verify_operator_session(token)
            return {
                "id": canonical_subject(f"device:{session.device_fp}"),
                "role": "human",
                "verified": True,
                "session": f"opsess:{session.jti}",
            }
        except Exception:  # noqa: BLE001
            pass  # Falls through to the header path; never raises or blocks.

    header_actor = request.headers.get("x-sk-actor")
    if header_actor:
        try:
            from capauth import canonical_subject

            return {
                "id": canonical_subject(header_actor),
                "role": "human",
                "verified": False,
                "session": None,
            }
        except Exception:  # noqa: BLE001
            pass

    return {"id": "unattributed", "role": "human", "verified": False, "session": None}


# --------------------------------------------------------------------------- #
# Envelope construction + best-effort permissive signing
# --------------------------------------------------------------------------- #
def _spe_canonical_bytes(envelope: dict) -> bytes:
    """Deterministic bytes of an SPE envelope with ``sig.value`` blanked.

    Generalizes ``skcapstone.fleet.signing.canonical_bytes``'s construction
    (sorted keys, one slot blanked, ``:`` / ``,`` separators) to this
    envelope's own ``sig.value`` slot rather than a fleet spec's
    ``writer.signature`` - PROVENANCE_AND_MUTATION_STANDARD.md section 5
    names ``fleet/signing.py::canonical_bytes`` as the reference
    construction, not a fleet-specific shape requirement.
    """
    body = json.loads(json.dumps(envelope, sort_keys=True, default=str))
    sig = dict(body.get("sig") or {})
    sig["value"] = None
    body["sig"] = sig
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _suite_id() -> str:
    try:
        from skcapstone.fleet.signing import SUITE_ID

        return SUITE_ID
    except Exception:  # noqa: BLE001
        return _FALLBACK_SUITE_ID


def _maybe_sign(envelope: dict) -> dict:
    """Best-effort permissive signing: sign when a capauth key is present,
    leave ``sig.value`` null otherwise.

    No off/permissive/enforce mode flag here - this mirrors the GTD P2
    posture the SPE spec ratified (section 7): "permissive: sign when a key
    is present, count when not." Never raises; signing is best-effort at
    write time and must never block a consent write.
    """
    envelope["sig"] = {"suite_id": _suite_id(), "value": None}
    try:
        from skcapstone.fleet.signing import capauth_signer

        signer = capauth_signer()
    except Exception:  # noqa: BLE001
        signer = None
    if signer is None:
        return envelope
    try:
        envelope["sig"]["value"] = signer(_spe_canonical_bytes(envelope))
    except Exception:  # noqa: BLE001
        envelope["sig"]["value"] = None
    return envelope


def build_consent_event(
    *,
    actor: dict,
    capability: str,
    resource_store: str,
    resource_kind: str,
    resource_id: str,
    decision: dict,
    prior: Optional[str] = None,
    ts: Optional[str] = None,
) -> dict:
    """Build one ``consent.granted`` SPE envelope.

    ``decision`` is the ``{"ok", "reason", "via", "obligations"}`` dict
    :func:`skdashboard.queue_authz.authorize_capability` /
    ``authorize_queue`` now return - the PDP AUDIT obligation this event
    exists to stop discarding (Unified Consent Plane Phase 2). The PDP's own
    obligation records ride under ``decision.obligations`` in the envelope
    payload, verbatim, alongside the coarser ``ok``/``reason``/``via`` summary
    every gate (token, pdp, both, and the dev loopback-open carve-out) can
    always supply.

    Args:
        actor: The dict :func:`resolve_consent_actor` returns.
        capability: The capauth capability string that was authorized (e.g.
            ``"agentrun.queue"``, ``"change.validate"``).
        resource_store: The store namespace owning the target
            (``"cards"``, ``"itil.changes"``).
        resource_kind: The target's kind within that store.
        resource_id: The target's resolved id.
        decision: The authz gate's result dict.
        prior: The state ref the writer observed before this event (SHOULD,
            PROVENANCE_AND_MUTATION_STANDARD.md section 1) - a coarse prior
            event count is enough to detect a concurrent change at
            verify/fold time; not treated as load-bearing here.
        ts: Override for the event timestamp (tests only; defaults to now).

    Returns:
        dict: The complete, signed-if-possible envelope, ready to append.
    """
    envelope: dict = {
        "spe": SPE_VERSION,
        "actor": {
            "id": actor.get("id", "unattributed"),
            "role": actor.get("role", "human"),
            "node": _HOSTNAME,
            "session": actor.get("session") or f"http:{uuid.uuid4().hex}",
        },
        "ts": ts or _now_iso(),
        "action": CONSENT_ACTION,
        "target": {"store": resource_store, "kind": resource_kind, "id": resource_id},
        "prior": prior,
        "capability": capability,
        "decision": {
            "ok": bool(decision.get("ok")),
            "reason": decision.get("reason"),
            "via": decision.get("via"),
            "obligations": decision.get("obligations") or [],
        },
    }
    return _maybe_sign(envelope)


# --------------------------------------------------------------------------- #
# Persistence: one adapter per front-door store (no new store)
# --------------------------------------------------------------------------- #
def record_card_consent(home, card_id: str, *, actor: dict, capability: str, decision: dict) -> dict:
    """Append a ``consent.granted`` event to a CardStore card's own event log.

    The queue-AI front door's adapter (``dashboard.py::_queue_run``, target
    the card the queue action is filed against). Uses
    ``skcapstone.card_store.CardStore.append_event`` - the store's existing
    public write path, no new store.
    """
    from skcapstone.card_store import CardStore

    store = CardStore(home)
    try:
        prior = str(len(store._read_events(card_id)))
    except Exception:  # noqa: BLE001
        prior = None
    event = build_consent_event(
        actor=actor,
        capability=capability,
        resource_store="cards",
        resource_kind="card",
        resource_id=card_id,
        decision=decision,
        prior=prior,
    )
    # CardStore.append_event's own signature is (card_id, action, agent,
    # **payload); "action" is bound positionally, so it must not also arrive
    # as a payload key ("action" is the one envelope field that collides -
    # every other key merges through append_event's own event.update(payload)
    # untouched). append_event re-adds "action" itself from the positional
    # arg, so the stored record is unaffected.
    payload = {k: v for k, v in event.items() if k != "action"}
    store.append_event(card_id, CONSENT_ACTION, actor.get("id", "unattributed"), **payload)
    return event


def consent_history_for_card(home, card_id: str) -> list[dict]:
    """All ``consent.granted`` events for one card, folded in deterministic
    ``(ts, writer, seq)`` order - answers "who consented to this action and
    when" straight from the card's own event log."""
    from skcapstone.card_store import CardStore

    store = CardStore(home)
    return [e for e in store._read_events(card_id) if e.get("action") == CONSENT_ACTION]


def record_change_consent(mgr, change_id: str, *, actor: dict, capability: str, decision: dict) -> dict:
    """Append a ``consent.granted`` event to an ITIL change's own event log.

    The change-management front doors' adapter (validate/schedule/arm). Uses
    ``skcoord.itil.ITILManager._append_event`` - the same internal write path
    ``dashboard.py`` already calls directly for ``validation``/``pr_link``
    events (see ``api_change_validate``), no new store.

    ``change_id`` must already be the RESOLVED record id (``_resolve_id``'s
    output, called ``rid`` at each call site) - the same id every other
    ``mgr._append_event``/``mgr._read_events`` call in ``dashboard.py`` uses,
    never the raw path param a redirect stub might still be keyed under.
    """
    try:
        prior = str(len(mgr._read_events(mgr.changes_dir, change_id)))
    except Exception:  # noqa: BLE001
        prior = None
    event = build_consent_event(
        actor=actor,
        capability=capability,
        resource_store="itil.changes",
        resource_kind="change",
        resource_id=change_id,
        decision=decision,
        prior=prior,
    )
    mgr._append_event(
        mgr.changes_dir, change_id, actor.get("id", "unattributed"), CONSENT_ACTION, **event
    )
    return event


def consent_history_for_change(mgr, change_id: str) -> list[dict]:
    """All ``consent.granted`` events for one change, in fold order - answers
    "who consented to this action and when" from the change's own event log."""
    return [
        e for e in mgr._read_events(mgr.changes_dir, change_id) if e.get("kind") == CONSENT_ACTION
    ]


__all__ = [
    "SPE_VERSION",
    "CONSENT_ACTION",
    "resolve_consent_actor",
    "build_consent_event",
    "record_card_consent",
    "consent_history_for_card",
    "record_change_consent",
    "consent_history_for_change",
]
