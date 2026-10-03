"""Append-only per-writer journal for the unified GTD store (SPE P1.1).

Card ``3d927cda``, sprint ``83482526``, epic ``373a33ca``. The GTD store keeps
its state in flat JSON lists and nothing else, so a mutation left no record:
nothing said who moved an item, out of which list, or when. Reopen (P1.2) and
attribution (P2) both need that record first.

The shape is lifted from ``skcoord.card_store``, which has run it since the
July-13 ITIL refactor and survives Syncthing by construction:

* one file per writer, ``journal/<agent>@<host>.jsonl``, so two processes never
  append to the same file and a sync merge is a union, never a conflict;
* appends only, guarded by ``flock`` for the read-seq-then-append window;
* a deterministic read order of ``(ts, writer, seq)``.

Every event carries the item's POST-state and the list it landed in, so the
fold is simply "drop this id everywhere, then place the item where the event
says". That makes :func:`fold` reproduce the live lists exactly, and it makes a
reversal just one more event rather than an edit to an old one.

Writer identity here is the ``SKAGENT`` name. Resolving it through
``capauth.resolve_agent_identity`` and signing the envelope is P2/P3 work; P1
deliberately carries no crypto dependency so the incident fix ships alone.

Scope: this journals the skcapstone GTD write paths (the MCP handlers and the
CLI that calls them). Adapters writing straight into ``skos.gtd_ingest`` do not
appear here yet; enumerating every writer is P4.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_DIRNAME = "journal"

_HOSTNAME = socket.gethostname()

# Mutations worth a line. A read never writes one.
ACTIONS = ("capture", "clarify", "move", "done", "reopen")


def _now_iso() -> str:
    """UTC now as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def writer_name() -> str:
    """The acting agent, per the standard SKAGENT precedence."""
    for var in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return "unknown"


def writer_id(writer: str | None = None) -> str:
    """The per-writer file stem, ``<agent>@<host>``."""
    safe = (writer or writer_name()).replace("/", "-").replace("@", "-")
    return f"{safe}@{_HOSTNAME}"


def journal_dir() -> Path:
    """Return (and create) the journal directory beside the GTD lists."""
    from .mcp_tools.gtd_tools import _gtd_dir

    d = _gtd_dir() / JOURNAL_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def append(
    action: str,
    item_id: str,
    item: dict,
    to: str,
    src: str | None = None,
    writer: str | None = None,
) -> dict:
    """Append one mutation event to this writer's own log.

    Args:
        action: One of :data:`ACTIONS`.
        item_id: The GTD item's id.
        item: The item's state AFTER the mutation.
        to: The store list the item now lives in (``archive`` included).
        src: The list it came from, if any. ``None`` for a fresh capture.
        writer: Override the acting agent (defaults to :func:`writer_name`).

    Returns:
        dict: the event as written.

    Callers append AFTER the store write has succeeded and while still holding
    the store lock, so the journal never claims a mutation that did not land.
    """
    path = journal_dir() / f"{writer_id(writer)}.jsonl"
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            seq = sum(1 for _ in fh)
            event = {
                "event_id": uuid.uuid4().hex,
                "ts": _now_iso(),
                "writer": writer or writer_name(),
                "node": _HOSTNAME,
                "seq": seq,
                "action": action,
                "item_id": item_id,
                "from": src,
                "to": to,
                "item": item,
            }
            # SPE P2: attribute, then sign. Both are permissive; see _envelope.
            try:
                _envelope(event, writer)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Envelope construction failed, writing bare event: %s", exc)
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(event, default=str) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return event


# ── SPE P2: attribution + permissive signing ─────────────────────────────
#
# The posture is PERMISSIVE and that is the design, not a shortcut. Provenance
# exists to make self-correction possible, so a capture must never fail because
# a key is missing, a keyring is locked, or capauth is not installed. Every
# degradation is RECORDED (actor.resolved / actor.degraded, absent sig) so
# `gtd verify` can report it, rather than swallowed.

SUITE_ID = "capauth-pgp-v1"


def _resolve_identity():
    """The capauth-resolved identity for this seat. Raises on failure.

    Split out so tests can force the failure path, and so the caller owns the
    permissive policy rather than burying it here.
    """
    from capauth import resolve_agent_identity

    return resolve_agent_identity()


def _signer():
    """A callable signing bytes for this seat, or None when unavailable."""
    from .fleet.signing import capauth_signer

    return capauth_signer()


def _verifier():
    """A callable verifying (bytes, signature) against the local roster, or None."""
    from .fleet.signing import capauth_verifier

    return capauth_verifier()


def canonical_event_bytes(event: dict) -> bytes:
    """Deterministic bytes an event's signature covers.

    The ``sig`` slot is excluded from its own input (otherwise nothing could
    ever verify), and everything else is included, so altering any field
    invalidates the signature. Same rule as ``fleet.signing.canonical_bytes``,
    which blanks the slot rather than dropping it; here the slot is top-level.
    """
    body = {k: v for k, v in event.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _actor_block(writer: str | None) -> dict:
    """Attribution for the acting seat, degrading rather than raising."""
    name = writer or writer_name()
    try:
        ident = _resolve_identity()
    except Exception as exc:  # noqa: BLE001
        logger.warning("capauth identity resolve failed, attributing unsigned: %s", exc)
        return {
            "agent": name,
            "capauth_uri": None,
            "fqid": None,
            "fingerprint": None,
            "node": _HOSTNAME,
            "resolved": False,
            "degraded": f"{type(exc).__name__}: {exc}",
        }
    return {
        "agent": getattr(ident, "agent", name) or name,
        "capauth_uri": getattr(ident, "capauth_uri", None),
        "fqid": getattr(ident, "fqid", None),
        "fingerprint": getattr(ident, "fingerprint", None),
        "node": _HOSTNAME,
        "resolved": True,
    }


def _envelope(event: dict, writer: str | None = None) -> dict:
    """Attach the SPE envelope to an event in place: actor, then signature.

    Signing is attempted always and is allowed to fail: no signer configured,
    a locked key, or a backend error all leave the event attributed but
    unsigned. Enforcement is P4's job, at a boundary, never at capture time.
    """
    event["actor"] = _actor_block(writer)
    if not event["actor"].get("resolved"):
        # A signature asserts "this seat, whose identity is X, signed this". If
        # the identity never resolved there is no X to assert, so the honest
        # envelope is attributed-but-unsigned rather than a signature floating
        # free of a claim. The key alone is not the claim.
        return event
    try:
        sign = _signer()
        if sign is None:
            return event
        event["sig"] = {
            "suite_id": SUITE_ID,
            "signature": sign(canonical_event_bytes(event)),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("GTD event signing failed, leaving it unsigned: %s", exc)
        event.pop("sig", None)
    return event


# Verification states. `unverifiable` is deliberately NOT `invalid`: no local
# trust roster means we cannot judge, and reporting that as a bad signature
# would cry wolf on every node that has not run the key ceremony.
VERIFY_STATES = ("verified", "unsigned", "invalid", "unverifiable", "pre-spe")


def verify() -> dict:
    """Classify every journal event and count the states.

    Returns:
        dict: ``{"total", "counts": {state: n}, "verifier_available": bool,
        "problems": [...]}``. ``problems`` lists the invalid events only, so
        the common case stays small.
    """
    verifier = None
    try:
        verifier = _verifier()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Verifier unavailable: %s", exc)

    counts = {state: 0 for state in VERIFY_STATES}
    problems: list[dict] = []
    events = read_all()
    for e in events:
        if not e.get("actor"):
            counts["pre-spe"] += 1
            continue
        sig = (e.get("sig") or {}).get("signature")
        if not sig:
            counts["unsigned"] += 1
            continue
        if verifier is None:
            counts["unverifiable"] += 1
            continue
        try:
            ok = verifier(canonical_event_bytes(e), sig)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning("Verifier error on %s: %s", e.get("event_id"), exc)
        if ok:
            counts["verified"] += 1
        else:
            counts["invalid"] += 1
            problems.append(
                {
                    "event_id": e.get("event_id"),
                    "ts": e.get("ts"),
                    "writer": e.get("writer"),
                    "action": e.get("action"),
                    "item_id": e.get("item_id"),
                }
            )
    return {
        "total": len(events),
        "counts": counts,
        "verifier_available": verifier is not None,
        "problems": problems,
    }


def read_all() -> list[dict]:
    """Every event across all writers, in ``(ts, writer, seq)`` order.

    A line that will not parse is skipped, not fatal: a torn tail on one
    writer's file must not make the whole history unreadable.
    """
    out: list[dict] = []
    d = journal_dir()
    for f in sorted(d.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("Skipping unreadable journal %s: %s", f.name, exc)
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed journal line in %s", f.name)
    out.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
    return out


def fold(events: list[dict] | None = None) -> dict[str, list[dict]]:
    """Replay the journal into ``{list_name: [items]}``.

    Each event is "this id is now HERE, in this state", so replaying is a
    remove-everywhere then append. The result matches the live store for any
    history whose mutations all went through the journal.
    """
    from .mcp_tools.gtd_tools import GTD_FILES

    state: dict[str, list[dict]] = {name: [] for name in GTD_FILES}
    for e in read_all() if events is None else events:
        item_id = e.get("item_id")
        if not item_id:
            continue
        for items in state.values():
            for idx, it in enumerate(items):
                if it.get("id") == item_id:
                    items.pop(idx)
                    break
        dest = e.get("to")
        if dest in state and isinstance(e.get("item"), dict):
            state[dest].append(e["item"])
    return state


def last_event_for(item_id: str) -> dict | None:
    """The most recent event touching one item, or None."""
    for e in reversed(read_all()):
        if e.get("item_id") == item_id:
            return e
    return None
