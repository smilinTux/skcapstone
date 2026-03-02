"""
Sync pipeline engine — comms path alignment for Syncthing ↔ consciousness loop.

Verifies that the inbox/outbox directories used by the consciousness loop
inotify watcher and the SKComm Syncthing transport are aligned under the
Syncthing-synced comms root, and reports pipeline health.

Pipeline:
    Syncthing → {shared_root}/sync/comms/inbox/{peer}/*.skc.json
             ↓  inotify  (ConsciousnessLoop._INBOX_DIR = "sync/comms/inbox")
    ConsciousnessLoop.process_envelope()
             ↓  skcomm.send()  — must route to SyncthingTransport with
                                 comms_root = {shared_root}/sync/comms
    SyncthingTransport → {shared_root}/sync/comms/outbox/{peer}/*.skc.json
             ↓  Syncthing propagates to peer
    Peer inbox

Path alignment requirement
--------------------------
The SKComm Syncthing transport must be configured with::

    comms_root: ~/.skcapstone/sync/comms

in ~/.skcomm/config.yml so its outbox and inbox paths match the paths the
consciousness loop watches via inotify.  This module provides
:func:`verify_pipeline_paths` to detect mismatches and
:func:`get_sync_pipeline_status` for the daemon health loop.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.sync_engine")

# Relative paths under SHARED_ROOT — must stay in sync with
# _INBOX_DIR in consciousness_loop.py ("sync/comms/inbox")
COMMS_SUBPATH = "sync/comms"
INBOX_SUBPATH = "sync/comms/inbox"
OUTBOX_SUBPATH = "sync/comms/outbox"
ENVELOPE_SUFFIX = ".skc.json"

# Character allowlist for peer/recipient names (mirrors _sanitize_peer_name)
_PEER_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer(name: str) -> str:
    """Sanitize a peer name for use as a filesystem subdirectory.

    Args:
        name: Raw peer / recipient string.

    Returns:
        Filesystem-safe name, max 64 chars, defaults to "unknown".
    """
    if not name or not isinstance(name, str):
        return "unknown"
    cleaned = name.replace("\x00", "").replace("/", "").replace("\\", "")
    cleaned = _PEER_SAFE_RE.sub("", cleaned).strip(".")
    return cleaned[:64] or "unknown"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_comms_root(shared_root: Path) -> Path:
    """Return the comms root directory for the sync pipeline.

    This is the directory that Syncthing syncs, containing inbox/ and outbox/.

    Args:
        shared_root: The agent SHARED_ROOT (``~/.skcapstone`` by default).

    Returns:
        ``{shared_root}/sync/comms``
    """
    return Path(shared_root).expanduser() / COMMS_SUBPATH


def get_inbox_dir(shared_root: Path) -> Path:
    """Return the inbox directory watched by the consciousness loop inotify.

    Args:
        shared_root: The agent SHARED_ROOT.

    Returns:
        ``{shared_root}/sync/comms/inbox``
    """
    return Path(shared_root).expanduser() / INBOX_SUBPATH


def get_outbox_dir(shared_root: Path) -> Path:
    """Return the outbox directory where responses are written for Syncthing.

    Args:
        shared_root: The agent SHARED_ROOT.

    Returns:
        ``{shared_root}/sync/comms/outbox``
    """
    return Path(shared_root).expanduser() / OUTBOX_SUBPATH


def ensure_comms_dirs(shared_root: Path) -> None:
    """Create inbox, outbox, and archive directories if they do not exist.

    Safe to call on every daemon startup.

    Args:
        shared_root: The agent SHARED_ROOT.
    """
    root = get_comms_root(shared_root)
    for sub in ("inbox", "outbox", "archive"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    logger.debug("Comms dirs ensured under %s", root)


# ---------------------------------------------------------------------------
# Pipeline verification
# ---------------------------------------------------------------------------


def verify_pipeline_paths(shared_root: Path, skcomm=None) -> dict:
    """Verify inbox/outbox path alignment for the full sync pipeline.

    Checks:

    1. The consciousness loop's watched inbox dir exists.
    2. The outbox dir exists so responses can be written and synced.
    3. (Optional) The active SKComm Syncthing transport's ``comms_root``
       matches ``{shared_root}/sync/comms``.

    Args:
        shared_root: The agent SHARED_ROOT.
        skcomm: Optional :class:`SKComm` instance to inspect transport config.

    Returns:
        Dict with keys:

        - ``inbox_ok`` (bool) — inbox dir exists.
        - ``outbox_ok`` (bool) — outbox dir exists.
        - ``transport_aligned`` (bool | None) — transport comms_root matches
          (None if skcomm not provided or check failed).
        - ``inbox_path`` (str) — absolute inbox path.
        - ``outbox_path`` (str) — absolute outbox path.
        - ``expected_comms_root`` (str) — the expected comms root.
        - ``issues`` (list[str]) — human-readable problem descriptions.
    """
    inbox = get_inbox_dir(shared_root)
    outbox = get_outbox_dir(shared_root)
    expected_root = get_comms_root(shared_root)
    issues: list[str] = []

    inbox_ok = inbox.exists()
    if not inbox_ok:
        issues.append(f"Inbox dir missing: {inbox}")

    outbox_ok = outbox.exists()
    if not outbox_ok:
        issues.append(f"Outbox dir missing: {outbox}")

    transport_aligned: Optional[bool] = None
    if skcomm is not None:
        try:
            transport_aligned = _check_transport_alignment(skcomm, expected_root, issues)
        except Exception as exc:
            logger.debug("Transport alignment check failed: %s", exc)
            issues.append(f"Transport alignment check error: {exc}")

    return {
        "inbox_ok": inbox_ok,
        "outbox_ok": outbox_ok,
        "transport_aligned": transport_aligned,
        "inbox_path": str(inbox),
        "outbox_path": str(outbox),
        "expected_comms_root": str(expected_root),
        "issues": issues,
    }


def _check_transport_alignment(skcomm, expected_root: Path, issues: list[str]) -> bool:
    """Walk SKComm router transports and check the Syncthing transport's root.

    Args:
        skcomm: SKComm instance (has ``router`` or ``_router`` attribute).
        expected_root: The expected comms root path.
        issues: Mutable list; problem strings are appended here.

    Returns:
        ``True`` if aligned (or no Syncthing transport registered),
        ``False`` on mismatch.
    """
    router = getattr(skcomm, "router", None) or getattr(skcomm, "_router", None)
    if router is None:
        return True  # can't check — assume ok

    transports = getattr(router, "transports", [])
    aligned = True
    for transport in transports:
        if getattr(transport, "name", "") != "syncthing":
            continue
        actual_root = getattr(transport, "_root", None)
        if actual_root is None:
            continue
        if Path(actual_root).resolve() != expected_root.resolve():
            issues.append(
                f"SyncthingTransport comms_root mismatch — "
                f"expected {expected_root}, got {actual_root}. "
                f"Set comms_root: {expected_root} in ~/.skcomm/config.yml"
            )
            aligned = False
    return aligned


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------


def get_sync_pipeline_status(shared_root: Path) -> dict:
    """Collect comms pipeline health: inbox/outbox file counts and path checks.

    Suitable for inclusion in the daemon health snapshot.

    Args:
        shared_root: The agent SHARED_ROOT.

    Returns:
        Dict with keys:

        - ``inbox_files`` (int) — pending inbox envelope count.
        - ``outbox_files`` (int) — pending outbox envelope count.
        - ``inbox_peers`` (list[str]) — peers with pending inbox files.
        - ``outbox_peers`` (list[str]) — peers with pending outbox files.
        - ``inbox_path`` (str), ``outbox_path`` (str) — absolute paths.
        - ``inbox_exists`` (bool), ``outbox_exists`` (bool).
        - ``checked_at`` (str) — ISO-8601 UTC timestamp.
    """
    inbox = get_inbox_dir(shared_root)
    outbox = get_outbox_dir(shared_root)

    def _count(base: Path) -> tuple[int, list[str]]:
        if not base.exists():
            return 0, []
        total = 0
        peers: list[str] = []
        try:
            for peer_dir in base.iterdir():
                if not peer_dir.is_dir():
                    continue
                count = len(list(peer_dir.glob(f"*{ENVELOPE_SUFFIX}")))
                if count:
                    total += count
                    peers.append(peer_dir.name)
        except OSError as exc:
            logger.debug("Error counting envelopes in %s: %s", base, exc)
        return total, peers

    inbox_count, inbox_peers = _count(inbox)
    outbox_count, outbox_peers = _count(outbox)

    return {
        "inbox_files": inbox_count,
        "outbox_files": outbox_count,
        "inbox_peers": inbox_peers,
        "outbox_peers": outbox_peers,
        "inbox_path": str(inbox),
        "outbox_path": str(outbox),
        "inbox_exists": inbox.exists(),
        "outbox_exists": outbox.exists(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Outbox write helper
# ---------------------------------------------------------------------------


def write_outbox_envelope(
    shared_root: Path,
    recipient: str,
    envelope: dict,
    sign: bool = False,
    pgp_key_id: Optional[str] = None,
) -> Path:
    """Write a response envelope to the outbox for Syncthing to sync.

    Uses atomic write (tmp → rename) to prevent Syncthing from picking up
    partial files.  Optionally attaches a PGP detached signature in the
    ``payload.signature`` field via CapAuth.

    Args:
        shared_root: The agent SHARED_ROOT.
        recipient: Recipient agent name (used as subdirectory; sanitized).
        envelope: Envelope dict to serialize as JSON.
        sign: Whether to attempt PGP signing via CapAuth.
        pgp_key_id: Optional PGP key ID.  Uses the default CapAuth key if
            ``None``.

    Returns:
        :class:`Path` to the written ``.skc.json`` file.
    """
    peer = _sanitize_peer(recipient)
    outbox_peer = get_outbox_dir(shared_root) / peer
    outbox_peer.mkdir(parents=True, exist_ok=True)

    if sign:
        try:
            _sign_envelope(envelope, pgp_key_id)
        except Exception as exc:
            logger.warning("PGP signing failed — writing unsigned envelope: %s", exc)

    envelope_id = (
        envelope.get("envelope_id")
        or envelope.get("message_id")
        or str(int(time.time() * 1000))
    )
    filename = f"{envelope_id}{ENVELOPE_SUFFIX}"
    target = outbox_peer / filename
    tmp = outbox_peer / f".{filename}.tmp"

    payload_bytes = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
    tmp.write_bytes(payload_bytes)
    tmp.rename(target)

    logger.info("Outbox: wrote %s → %s", envelope_id[:8], target)
    return target


def _sign_envelope(envelope: dict, key_id: Optional[str] = None) -> None:
    """Attach a PGP detached signature to the envelope payload in-place.

    Signs the ``payload.content`` (or ``payload.message``) field and stores
    the armored ASCII signature in ``payload.signature``.

    Args:
        envelope: Envelope dict.  Modified in-place.
        key_id: Optional PGP key ID.  Uses the default CapAuth key if ``None``.
    """
    from capauth.crypto import get_backend

    payload = envelope.get("payload", envelope)
    content = payload.get("content") or payload.get("message") or ""
    if not content:
        return

    backend = get_backend()
    content_bytes = content.encode("utf-8") if isinstance(content, str) else content
    kwargs: dict = {}
    if key_id:
        kwargs["key_id"] = key_id
    signature = backend.sign(data=content_bytes, **kwargs)
    payload["signature"] = signature
    logger.debug("Envelope signed (key_id=%s)", key_id or "default")
