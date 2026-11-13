"""Which CAB votes and changes may authorize an actuation (card bada5c86).

Shared by every actuation gate: the ATLAS dispatcher (``operator_seat/dispatch``),
the governed CMDB apply gate (``operator_seat/cmdb_adapter``) and the trustee
rotate guard (``trustee_actuation``). It decides from the votes and the change's
own shape, never from the fold's ``approved`` status alone, and it does not depend
on which skcoord release is installed: before bada5c86 the skcoord fold counted a
bare ``agent="human"`` vote, any vote by the change's creator, and provenance
fields that any caller could write.

A human approval qualifies only when the vote stores the signed CapAuth
authorization envelope it was cast under and that envelope verifies against a
PINNED human approver key. The pin is the operator config file ``APPROVERS_FILE``
(``~/.config/skcapstone/cab-approvers.json``), the same file skcoord's fold reads;
its format is documented in skcoord's ``cab_authorization`` module (one
``approvers`` entry per human: ``identity``, ``fingerprint``, ``roles``,
``backend`` and ``public_key`` or ``public_key_file``). ``CAPAUTH_HOME`` and the
caller's own CapAuth profile play no part. No pin file means no human approval.

On an skcoord release that predates the ``authorization`` field, the envelope is
read straight from the vote file, so the rule is the same on every version.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

#: The pinned human approver keys. Tests point this elsewhere.
APPROVERS_FILE = Path("~/.config/skcapstone/cab-approvers.json")

HUMAN_APPROVER_ROLES = frozenset({"owner", "approver"})
#: Identities that never approve: the operator seat runs as ``SKAGENT=operator`` and
#: writes its ledger as ``atlas``; device seats are machines.
MACHINE_VOTERS = frozenset({"operator", "atlas"})
MACHINE_VOTER_PREFIXES = ("device:", "operator:")
#: Labels that name no one (what a caller writes when it has no identity).
PLACEHOLDER_VOTERS = frozenset({"", "human", "unattributed", "unknown"})

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")

#: How long a human CAB approval stays usable, measured from its signed grant's
#: ``issued_at`` against the real clock. Every actuation gate applies it: the ATLAS
#: dispatcher (configurable up to the cap), the governed CMDB apply gate and the
#: trustee guard (both at the default).
DEFAULT_MAX_APPROVAL_AGE_SECONDS = 86400.0
MAX_ALLOWED_APPROVAL_AGE_SECONDS = 604800.0  # seven days


def identity_key(identity: Any) -> str:
    """Spelling-independent key: whitespace stripped, case folded local part, no ``capauth:``.

    ``chef``, ``"chef "``, ``Chef`` and ``capauth:chef@skworld.io`` are one voter.
    """
    key = str(identity or "").strip().casefold()
    if key.startswith("capauth:"):
        key = key[len("capauth:") :]
    return key.split("@", 1)[0].strip()


def is_machine_identity(identity: Any) -> bool:
    """The operator seat, a device seat, or a placeholder: never a human approver."""
    key = identity_key(identity)
    return (
        key in MACHINE_VOTERS
        or key in PLACEHOLDER_VOTERS
        or key.startswith(MACHINE_VOTER_PREFIXES)
    )


def _fpr(value: Any) -> str:
    return str(value or "").replace(" ", "").upper()


def load_approvers(path: Path | None = None) -> dict[str, dict]:
    """Pinned approvers keyed by upper-case fingerprint; ``{}`` when absent or invalid."""
    target = Path(path or APPROVERS_FILE).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        approvers = {}
        for entry in data.get("approvers", []):
            armor = entry.get("public_key")
            if not armor and entry.get("public_key_file"):
                armor = Path(entry["public_key_file"]).expanduser().read_text(encoding="utf-8")
            fpr = _fpr(entry.get("fingerprint"))
            if fpr and armor and entry.get("identity"):
                approvers[fpr] = {
                    "identity": str(entry["identity"]),
                    "roles": frozenset(entry.get("roles") or HUMAN_APPROVER_ROLES),
                    "backend": str(entry.get("backend") or "pgpy"),
                    "public_key": armor,
                }
        return approvers
    except Exception:  # noqa: BLE001 - an unreadable pin pins nobody
        return {}


def pinned_approver(fingerprint: str, path: Path | None = None) -> dict | None:
    """The pinned approver entry for *fingerprint*, or None."""
    return load_approvers(path).get(_fpr(fingerprint))


@functools.lru_cache(maxsize=256)
def signature_ok(backend: str, fingerprint: str, data: bytes, signature: str, armor: str) -> bool:
    """Whether *signature* over *data* verifies with *armor*, whose fingerprint is pinned."""
    try:
        from capauth.crypto import get_backend
        from capauth.models import CryptoBackendType

        crypto = get_backend(CryptoBackendType(backend))
        if _fpr(crypto.fingerprint_from_armor(armor)) != fingerprint:
            return False
        return bool(crypto.verify(data, signature, armor))
    except Exception:  # noqa: BLE001 - anything that cannot verify does not verify
        return False


def _file_binds(itil: Any, change_id: str, vote: Any) -> bool:
    """Whether ``<change_id>-<voter>.json`` holds this change id and this voter."""
    try:
        path = Path(itil.cab_dir) / f"{change_id}-{vote.agent}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no readable file under the bound name
        return False
    return raw.get("change_id") == change_id and raw.get("agent") == vote.agent


def votes_for(itil: Any, change_id: str) -> list:
    """The CAB votes bound to *change_id*, the change being gated (from the caller).

    A vote counts only when its change id is *change_id* and the file named for this
    change and its voter (``<change_id>-<voter>.json``) records that change and that
    voter. A vote file copied byte for byte under another change's or another voter's
    name therefore drops out, on every skcoord release (older ones load vote files by
    name and never check the change id inside). Raises when the store is unreadable,
    like ``get_cab_votes``.
    """
    if not change_id:
        return []
    return [
        v
        for v in itil.get_cab_votes(change_id)
        if getattr(v, "change_id", None) == change_id and _file_binds(itil, change_id, v)
    ]


def _issued_at(envelope: Any) -> datetime | None:
    """A grant's signed ``issued_at`` as an aware datetime, or None."""
    if not isinstance(envelope, dict):
        return None
    try:
        issued = datetime.fromisoformat(str(envelope.get("issued_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return issued if issued.tzinfo is not None else None


def current_votes(itil: Any, change_id: str, approvers: dict | None = None) -> list:
    """Each voter's current vote on *change_id*: one slot per human (card bada5c86).

    Starts from ``votes_for`` (bound to the change and file name). Voters compare by
    ``identity_key``. For a voter holding at least one verified signed vote, only the
    signed vote with the latest signed ``issued_at`` counts (a rejection wins a tie)
    and that voter's unsigned votes are dropped, so an older grant written back cannot
    undo a later decision. Voters with no signed vote keep their votes as they are.
    """
    pins = load_approvers() if approvers is None else approvers
    signed: dict[str, tuple[tuple[datetime, bool], Any]] = {}
    unsigned = []
    for vote in votes_for(itil, change_id):
        envelope = stored_envelope(itil, vote)
        issued = _issued_at(envelope)
        if issued is None or not verify_vote_envelope(vote, envelope, pins, change_id=change_id):
            unsigned.append(vote)
            continue
        key = identity_key(vote.agent)
        rank = (issued, _decision(vote) == "rejected")
        if key not in signed or rank > signed[key][0]:
            signed[key] = (rank, vote)
    return [vote for _rank, vote in signed.values()] + [
        vote for vote in unsigned if identity_key(vote.agent) not in signed
    ]


def _decision(vote: Any) -> str:
    return str(getattr(vote.decision, "value", vote.decision))


def stored_envelope(itil: Any, vote: Any) -> dict | None:
    """The signed grant stored with *vote*, from the model or the raw vote file."""
    envelope = getattr(vote, "authorization", None)
    if isinstance(envelope, dict):
        return envelope
    try:
        path = Path(itil.cab_dir) / f"{vote.change_id}-{vote.agent}.json"
        envelope = json.loads(path.read_text(encoding="utf-8")).get("authorization")
    except Exception:  # noqa: BLE001 - no readable file, no envelope
        return None
    return envelope if isinstance(envelope, dict) else None


def verify_vote_envelope(
    vote: Any, envelope: Any, approvers: dict | None = None, *, change_id: str | None = None
) -> bool:
    """Whether *envelope* is a pinned approver's valid grant for exactly this vote.

    *change_id* is the change being gated, from the caller: the vote and the grant
    must both bind it, so a grant copied from another change's vote file does not
    verify. Lifetime and single use are enforced when the grant is redeemed; a grant
    bound to one change and one decision authorizes nothing else, so they are not
    re-checked here (the actuation gates apply an approval age from ``issued_at``).
    """
    from .operator_authorization import AuthorizationEnvelope, authorization_id

    if change_id is not None and getattr(vote, "change_id", None) != change_id:
        return False

    try:
        env = AuthorizationEnvelope.model_validate(envelope)
    except Exception:  # noqa: BLE001 - not an envelope
        return False
    if set(envelope) != set(AuthorizationEnvelope.model_fields):
        return False
    decision = getattr(vote.decision, "value", vote.decision)
    fingerprint = _fpr(env.issuer_fingerprint)
    if (
        env.schema_version != "atlas-authorization/v1"
        or env.action != f"itil.cab.vote.{decision}"
        or env.change_id != vote.change_id
        or (change_id is not None and env.change_id != change_id)
        or env.issuer_role not in HUMAN_APPROVER_ROLES
        or env.issuer_role != getattr(vote, "subject_role", "")
        or fingerprint != _fpr(getattr(vote, "subject_fingerprint", ""))
        or env.authorization_id != authorization_id(env)
        or env.authorization_id != getattr(vote, "authorization_id", "")
        or identity_key(env.issuer) != identity_key(vote.agent)
        or not _NONCE_RE.fullmatch(env.nonce)
        or not env.signature
    ):
        return False
    pinned = (load_approvers() if approvers is None else approvers).get(fingerprint)
    if (
        pinned is None
        or env.issuer_role not in pinned["roles"]
        or identity_key(pinned["identity"]) != identity_key(env.issuer)
    ):
        return False
    return signature_ok(
        pinned["backend"], fingerprint, env.signing_bytes(), env.signature, pinned["public_key"]
    )


def _approved(vote: Any) -> bool:
    return getattr(vote.decision, "value", vote.decision) == "approved"


def _rejected(vote: Any) -> bool:
    """A rejection that blocks: by a named voter. A placeholder's veto names no one."""
    return (
        getattr(vote.decision, "value", vote.decision) == "rejected"
        and identity_key(vote.agent) not in PLACEHOLDER_VOTERS
    )


def qualifying_human_approval(
    itil: Any, vote: Any, change: Any, approvers: dict | None = None
) -> bool:
    """Whether *vote* is a human approval an actuation gate may act on.

    An APPROVE whose stored signed grant verifies against a pinned approver key
    (``verify_vote_envelope``), by a voter that names a person (not a placeholder,
    the operator seat or a device) and is not the change's drafter, creator or
    managed-by owner. Historical unprovenanced votes never qualify here: they carry
    no grant, and they are older than any usable approval age.
    """
    if not _approved(vote) or is_machine_identity(vote.agent):
        return False
    owners = {
        identity_key(x)
        for x in (
            getattr(change, "prepared_by", None),
            getattr(change, "created_by", None),
            getattr(change, "managed_by", None),
        )
        if x
    }
    if identity_key(vote.agent) in owners:
        return False
    return verify_vote_envelope(
        vote, stored_envelope(itil, vote), approvers, change_id=getattr(change, "id", None) or ""
    )


def auto_approved(change: Any, votes: list) -> bool:
    """Whether *change* is approved with no human behind it, derived positively.

    True only for a ``standard`` change, or one with the auto-normal shape (normal,
    tagged ``auto-normal``, created by the operator, not high risk, a rollback plan
    and no rejection). Never inferred from the fold's ``approved`` status or from
    the absence of a human vote: an older skcoord fold approves a normal change on a
    bare ``human`` vote.
    """
    kind = getattr(change.change_type, "value", change.change_type)
    if kind == "standard":
        return True
    return (
        kind == "normal"
        and "auto-normal" in (change.tags or [])
        and change.created_by == "operator"
        and getattr(change.risk, "value", change.risk) != "high"
        and bool((change.rollback_plan or "").strip())
        and not any(_rejected(v) for v in votes)
    )


def human_approval_times(itil: Any, change: Any) -> list[str]:
    """When each qualifying human approval was granted; ``[]`` means none.

    The time is the signed grant's ``issued_at``, not the vote file's ``decided_at``:
    the grant's time is covered by the signature, while ``decided_at`` is plain text
    that anyone able to write the vote file could re-date to dodge the approval age
    limit. A ``standard`` or auto-normal change is auto-approved whatever its votes
    say, and any named rejection means no human approval. Fails closed on an
    unreadable vote store.
    """
    approvers = load_approvers()
    try:
        votes = current_votes(itil, change.id, approvers)
    except Exception:  # noqa: BLE001 - unreadable votes are no approval
        return []
    if auto_approved(change, votes) or any(_rejected(v) for v in votes):
        return []
    return [
        str(stored_envelope(itil, v)["issued_at"])
        for v in votes
        if qualifying_human_approval(itil, v, change, approvers)
    ]


def approval_basis(itil: Any, change: Any) -> str | None:
    """``"human"``, ``"auto"`` or None: why *change* may be acted on, if at all.

    Requires the fold to read ``approved`` (or ``scheduled``) as well, but never
    trusts it alone.
    """
    if getattr(change.status, "value", change.status) not in ("approved", "scheduled"):
        return None
    try:
        votes = current_votes(itil, change.id)
    except Exception:  # noqa: BLE001 - unreadable votes are no approval
        return None
    if auto_approved(change, votes):
        return "auto"
    if human_approval_times(itil, change):
        return "human"
    return None


def record_signed_vote(itil: Any, envelope: dict, *, conditions: str = "") -> Any:
    """Record the vote a verified signed grant authorizes, storing the grant with it.

    The caller has already verified *envelope* (signature against the pin, lifetime,
    single use). The voter, role, fingerprint and authorization id all come from the
    envelope, never from the caller. On an skcoord release without the
    ``authorization`` field the envelope is written into the vote file, where
    ``stored_envelope`` reads it.
    """
    import inspect

    refuse_rollback(itil, envelope)
    decision = str(envelope["action"]).rsplit(".", 1)[-1]
    kwargs = dict(
        agent=envelope["issuer"],
        decision=decision,
        conditions=conditions,
        subject=envelope["issuer"],
        subject_role=envelope["issuer_role"],
        subject_fingerprint=envelope["issuer_fingerprint"],
        authorization_id=envelope["authorization_id"],
    )
    if _accepts_authorization(itil.submit_cab_vote, inspect):
        return itil.submit_cab_vote(envelope["change_id"], authorization=envelope, **kwargs)
    vote = itil.submit_cab_vote(envelope["change_id"], **kwargs)
    path = Path(itil.cab_dir) / f"{envelope['change_id']}-{vote.agent}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["authorization"] = envelope
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    return vote


def _accepts_authorization(submit: Any, inspect: Any) -> bool:
    """Whether this skcoord's ``submit_cab_vote`` stores ``authorization=``.

    Named in the signature, or (behind a ``**kwargs`` wrapper) a vote model that has
    the field. Recording a signed grant as unsigned and patching the file afterwards
    would be refused by a release that guards signed slots.
    """
    params = inspect.signature(submit).parameters
    if "authorization" in params:
        return True
    if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return False
    try:
        from skcoord.itil import CABDecision
    except ImportError:
        return False
    return "authorization" in CABDecision.model_fields


def refuse_rollback(itil: Any, envelope: dict) -> None:
    """Raise ``ValueError`` unless *envelope* is strictly newer than its voter's signed vote.

    When the issuer (by ``identity_key``, across every file name) already holds a
    verified signed vote on the change, the new grant's signed ``issued_at`` must be
    strictly later than every one held. Otherwise a replayed older grant (an approval
    since revoked by a signed rejection) would roll the decision back. skcoord refuses
    this from the bada5c86 release on; checking here keeps every skcapstone write path
    safe on older releases.
    """
    change_id = str(envelope.get("change_id") or "")
    key = identity_key(envelope.get("issuer"))
    try:
        votes = votes_for(itil, change_id)
    except Exception:  # noqa: BLE001 - nothing readable, nothing held
        return
    held = []
    for vote in votes:
        if identity_key(vote.agent) != key:
            continue
        stored = stored_envelope(itil, vote)
        if verify_vote_envelope(vote, stored, change_id=change_id):
            held.append(_issued_at(stored))
    if not held:
        return
    issued = _issued_at(envelope)
    if issued is None or any(t is None or issued <= t for t in held):
        raise ValueError(
            f"refusing to replace the authenticated CAB vote of {envelope.get('issuer')!r} on "
            f"{change_id}: the new grant is not issued after the one held"
        )


def refuse_unsigned_overwrite(itil: Any, change_id: str, voter: str) -> None:
    """Raise ``ValueError`` when *voter* already holds a signed vote on *change_id*.

    Vote files are keyed by voter, so an unsigned vote would silently replace a
    signed one. skcoord refuses this from the bada5c86 release on; checking here
    too keeps the CLI and MCP surfaces safe on older releases.
    """
    try:
        votes = votes_for(itil, change_id)
    except Exception:  # noqa: BLE001 - nothing readable, nothing to protect
        return
    for vote in votes:
        if identity_key(vote.agent) != identity_key(voter):
            continue
        if verify_vote_envelope(vote, stored_envelope(itil, vote), change_id=change_id):
            raise ValueError(
                f"refusing to replace the authenticated CAB vote of {vote.agent!r} on "
                f"{change_id} with an unauthenticated one"
            )


def verify_grant_for(
    envelope: dict, *, change_id: str, decision: str, approvers: dict | None = None
) -> bool:
    """Whether *envelope* is a pinned approver's valid grant for this change and decision."""
    from types import SimpleNamespace

    try:
        probe = SimpleNamespace(
            change_id=change_id,
            agent=envelope["issuer"],
            decision=decision,
            subject_role=envelope["issuer_role"],
            subject_fingerprint=envelope["issuer_fingerprint"],
            authorization_id=envelope["authorization_id"],
        )
    except (KeyError, TypeError):
        return False
    if is_machine_identity(probe.agent):
        return False
    return verify_vote_envelope(probe, envelope, approvers, change_id=change_id)
