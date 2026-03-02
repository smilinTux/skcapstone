"""
Peer Directory — transport address map for the sovereignty mesh.

Maps agent names to their SKComm transport addresses (Syncthing outbox
paths, WebRTC fingerprints, Tailscale IPs, etc.).

Separate from PeerRecord (PGP identity in peers.py) — this module owns
the *routing* layer, not the trust/cryptography layer.

Storage: {skcapstone_home}/peers/directory.yaml

Entry format:
    lumina:
      address: /home/user/.skcapstone/sync/comms/outbox/lumina
      transport: syncthing
      fingerprint: ABCD1234...
      last_seen: 2026-03-02T...
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from . import SHARED_ROOT

logger = logging.getLogger("skcapstone.peer_directory")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class DirectoryEntry(BaseModel):
    """A single peer's transport routing entry.

    Attributes:
        name: Normalized peer name (lowercase).
        address: Transport address — Syncthing outbox path, Tailscale IP,
            WebRTC fingerprint URI, or other transport-specific locator.
        transport: Transport type label (syncthing, webrtc, tailscale, file).
        fingerprint: Optional PGP fingerprint for cross-referencing.
        last_seen: ISO-8601 UTC timestamp of last known activity.
    """

    name: str
    address: str
    transport: str = "syncthing"
    fingerprint: str = ""
    last_seen: Optional[str] = None


# ---------------------------------------------------------------------------
# PeerDirectory
# ---------------------------------------------------------------------------


class PeerDirectory:
    """Transport address directory for the sovereign agent mesh.

    Maps agent names → transport addresses. Separate from the PGP identity
    peer store (peers.py); this module owns routing, not trust.

    Storage: {home}/peers/directory.yaml

    Args:
        home: skcapstone home directory. Defaults to SHARED_ROOT (~/.skcapstone).
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self._home = home or Path(SHARED_ROOT).expanduser()
        self._path = self._home / "peers" / "directory.yaml"
        self._entries: dict[str, DirectoryEntry] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self) -> dict[str, DirectoryEntry]:
        """Read the directory from disk.

        Safe to call multiple times — idempotent re-load.

        Returns:
            Dict mapping normalized name -> DirectoryEntry.
        """
        if not self._path.exists():
            self._entries = {}
            self._loaded = True
            return {}

        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            entries: dict[str, DirectoryEntry] = {}
            for name, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    entries[str(name).lower()] = DirectoryEntry.model_validate(
                        {"name": str(name).lower(), **entry}
                    )
                except Exception as exc:
                    logger.debug("Skipping malformed entry '%s': %s", name, exc)
            self._entries = entries
        except Exception as exc:
            logger.warning("Failed to load peer directory: %s", exc)
            self._entries = {}

        self._loaded = True
        return dict(self._entries)

    def resolve(self, name: str) -> Optional[str]:
        """Get the transport address for a named peer.

        Args:
            name: Peer name (case-insensitive).

        Returns:
            Transport address string, or None if the peer is not in the
            directory.
        """
        self._ensure_loaded()
        entry = self._entries.get(name.lower())
        return entry.address if entry else None

    def add_peer(
        self,
        name: str,
        address: str,
        transport: str = "syncthing",
        fingerprint: str = "",
    ) -> DirectoryEntry:
        """Add or update a peer's transport entry.

        If the peer already exists it is overwritten. Persists atomically.

        Args:
            name: Peer display name (normalised to lowercase as the key).
            address: Transport address (path, IP, URI, etc.).
            transport: Transport type — syncthing, webrtc, tailscale, file.
            fingerprint: Optional PGP fingerprint for cross-referencing.

        Returns:
            The created or updated DirectoryEntry.
        """
        self._ensure_loaded()
        entry = DirectoryEntry(
            name=name.lower(),
            address=address,
            transport=transport,
            fingerprint=fingerprint,
            last_seen=datetime.now(timezone.utc).isoformat(),
        )
        self._entries[name.lower()] = entry
        self._save()
        logger.info("Directory: added '%s' → %s (%s)", name, address, transport)
        return entry

    def remove_peer(self, name: str) -> bool:
        """Remove a peer from the directory.

        Args:
            name: Peer name to remove (case-insensitive).

        Returns:
            True if the peer was found and removed, False otherwise.
        """
        self._ensure_loaded()
        key = name.lower()
        if key not in self._entries:
            return False
        del self._entries[key]
        self._save()
        logger.info("Directory: removed '%s'", name)
        return True

    def list_peers(self) -> list[DirectoryEntry]:
        """Return all known peers, sorted by name.

        Returns:
            List of DirectoryEntry sorted alphabetically.
        """
        self._ensure_loaded()
        return sorted(self._entries.values(), key=lambda e: e.name)

    def update_last_seen(self, name: str) -> None:
        """Touch the last_seen timestamp for a peer (in-place + save).

        Called by the consciousness loop whenever a message arrives from
        a known peer, so the directory stays current without a full re-add.

        Args:
            name: Peer name (case-insensitive). No-op if peer is unknown.
        """
        self._ensure_loaded()
        key = name.lower()
        if key not in self._entries:
            return
        self._entries[key].last_seen = datetime.now(timezone.utc).isoformat()
        self._save()

    def auto_discover(
        self,
        heartbeats_dir: Optional[Path] = None,
    ) -> list[DirectoryEntry]:
        """Discover peers from heartbeat files and Syncthing outbox dirs.

        Scans two sources and adds any *new* peers (existing entries are
        never overwritten):

        1. ``{home}/heartbeats/*.json`` — live heartbeat files published by
           each agent via HeartbeatBeacon.
        2. ``{home}/sync/comms/outbox/`` — one sub-directory per peer that
           Syncthing keeps in sync.

        Syncthing outbox path is used as the default address because that
        is where SKComm writes messages for the peer.

        Args:
            heartbeats_dir: Override for the heartbeats directory.  Defaults
                to ``{home}/heartbeats``.

        Returns:
            List of newly-added DirectoryEntry objects (empty if all peers
            were already known).
        """
        self._ensure_loaded()
        added: list[DirectoryEntry] = []

        hb_dir = heartbeats_dir or (self._home / "heartbeats")

        # 1. Scan heartbeat files
        if hb_dir.exists():
            for hb_file in sorted(hb_dir.glob("*.json")):
                if hb_file.name.endswith(".tmp"):
                    continue
                agent_name = hb_file.stem.lower()
                if agent_name in self._entries:
                    # Still update last_seen from heartbeat timestamp
                    try:
                        data = json.loads(hb_file.read_text(encoding="utf-8"))
                        ts = data.get("timestamp", "")
                        if ts:
                            self._entries[agent_name].last_seen = ts
                    except Exception:
                        pass
                    continue

                try:
                    data = json.loads(hb_file.read_text(encoding="utf-8"))
                    # Default Syncthing outbox path for this peer
                    outbox = self._home / "sync" / "comms" / "outbox" / agent_name
                    entry = DirectoryEntry(
                        name=agent_name,
                        address=str(outbox),
                        transport="syncthing",
                        fingerprint=data.get("fingerprint", ""),
                        last_seen=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    )
                    self._entries[agent_name] = entry
                    added.append(entry)
                    logger.info("Auto-discovered '%s' from heartbeat", agent_name)
                except Exception as exc:
                    logger.debug("Cannot parse heartbeat %s: %s", hb_file.name, exc)

        # 2. Scan Syncthing outbox dirs
        outbox_root = self._home / "sync" / "comms" / "outbox"
        if outbox_root.exists():
            for peer_dir in sorted(outbox_root.iterdir()):
                if not peer_dir.is_dir():
                    continue
                agent_name = peer_dir.name.lower()
                if agent_name in self._entries:
                    continue
                entry = DirectoryEntry(
                    name=agent_name,
                    address=str(peer_dir),
                    transport="syncthing",
                )
                self._entries[agent_name] = entry
                added.append(entry)
                logger.info("Auto-discovered '%s' from outbox", agent_name)

        if added:
            self._save()

        return added

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load from disk on first access."""
        if not self._loaded:
            self.load()

    def _save(self) -> None:
        """Atomically serialize the directory to YAML."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, dict] = {}
        for name, entry in sorted(self._entries.items()):
            row: dict = {
                "address": entry.address,
                "transport": entry.transport,
            }
            if entry.fingerprint:
                row["fingerprint"] = entry.fingerprint
            if entry.last_seen:
                row["last_seen"] = entry.last_seen
            data[name] = row

        tmp = self._path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
        tmp.rename(self._path)
