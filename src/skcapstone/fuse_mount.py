"""
FUSE Mount — Sovereign Virtual Filesystem.

Exposes the sovereign agent's data (memories, identity, inbox, outbox,
coordination tasks) as a mountable POSIX filesystem via FUSE.

Virtual directory layout::

    /
    ├── memories/
    │   ├── short/          — short-term memory files (.md)
    │   ├── mid/            — mid-term memory files (.md)
    │   └── long/           — long-term memory files (.md)
    ├── documents/          — SKSeal signed documents
    ├── identity/
    │   ├── card.json       — CapAuth identity card
    │   └── fingerprint.txt — PGP fingerprint
    ├── inbox/              — SKComm incoming messages (read-only)
    ├── outbox/             — Write here to send via SKComm
    └── coordination/       — Task board files (.json)

Writing to ``/outbox/<agent_name>.msg`` enqueues a message via SKComm.

Dependencies (optional):
    pip install skcapstone[fuse]  # pulls in fusepy
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("skcapstone.fuse")

# ---------------------------------------------------------------------------
# Layer name mapping: virtual dir slug → MemoryLayer value
# ---------------------------------------------------------------------------

_LAYER_SLUG_TO_VALUE: Dict[str, str] = {
    "short": "short-term",
    "mid": "mid-term",
    "long": "long-term",
}

_LAYER_VALUE_TO_SLUG: Dict[str, str] = {v: k for k, v in _LAYER_SLUG_TO_VALUE.items()}

# ---------------------------------------------------------------------------
# Virtual filesystem path constants
# ---------------------------------------------------------------------------

_MEMORIES_DIR = "memories"
_DOCUMENTS_DIR = "documents"
_IDENTITY_DIR = "identity"
_INBOX_DIR = "inbox"
_OUTBOX_DIR = "outbox"
_COORDINATION_DIR = "coordination"

_TOP_LEVEL_DIRS = [
    _MEMORIES_DIR,
    _DOCUMENTS_DIR,
    _IDENTITY_DIR,
    _INBOX_DIR,
    _OUTBOX_DIR,
    _COORDINATION_DIR,
]

_MEMORY_SUBDIRS = list(_LAYER_SLUG_TO_VALUE.keys())  # short, mid, long

_IDENTITY_FILES = ["card.json", "fingerprint.txt"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ts() -> float:
    """Return the current Unix timestamp as a float.

    Returns:
        Current UTC time as a float POSIX timestamp.
    """
    return datetime.now(timezone.utc).timestamp()


def _dir_stat(nlink: int = 2) -> Dict[str, Any]:
    """Build a stat dict for a virtual directory.

    Args:
        nlink: Number of hard links (default: 2).

    Returns:
        Stat dictionary suitable for FUSE Operations.getattr().
    """
    ts = _now_ts()
    return {
        "st_mode": stat.S_IFDIR | 0o555,
        "st_nlink": nlink,
        "st_uid": os.getuid(),
        "st_gid": os.getgid(),
        "st_size": 0,
        "st_atime": ts,
        "st_mtime": ts,
        "st_ctime": ts,
    }


def _file_stat(size: int, writable: bool = False) -> Dict[str, Any]:
    """Build a stat dict for a virtual file.

    Args:
        size: File size in bytes.
        writable: Whether the file is writable (e.g., outbox files).

    Returns:
        Stat dictionary suitable for FUSE Operations.getattr().
    """
    ts = _now_ts()
    mode = stat.S_IFREG | (0o644 if writable else 0o444)
    return {
        "st_mode": mode,
        "st_nlink": 1,
        "st_uid": os.getuid(),
        "st_gid": os.getgid(),
        "st_size": size,
        "st_atime": ts,
        "st_mtime": ts,
        "st_ctime": ts,
    }


# ---------------------------------------------------------------------------
# Content generators
# ---------------------------------------------------------------------------


def _memory_to_markdown(memory: Dict[str, Any]) -> bytes:
    """Render a memory dict as a Markdown document.

    Args:
        memory: Parsed JSON dict of a MemoryEntry.

    Returns:
        UTF-8 encoded Markdown bytes.
    """
    lines: List[str] = []
    lines.append(f"# Memory: {memory.get('memory_id', 'unknown')}")
    lines.append("")

    created = memory.get("created_at", "")
    if created:
        lines.append(f"**Created:** {created}")

    layer = memory.get("layer", "")
    if layer:
        lines.append(f"**Layer:** {layer}")

    importance = memory.get("importance")
    if importance is not None:
        lines.append(f"**Importance:** {importance:.2f}")

    tags = memory.get("tags", [])
    if tags:
        lines.append(f"**Tags:** {', '.join(tags)}")

    soul = memory.get("soul_context")
    if soul:
        lines.append(f"**Soul:** {soul}")

    source = memory.get("source", "")
    if source:
        lines.append(f"**Source:** {source}")

    lines.append("")
    lines.append("## Content")
    lines.append("")
    lines.append(memory.get("content", ""))

    metadata = memory.get("metadata") or {}
    if metadata:
        lines.append("")
        lines.append("## Metadata")
        lines.append("")
        for k, v in metadata.items():
            lines.append(f"- **{k}:** {v}")

    return "\n".join(lines).encode("utf-8")


def _load_memory_file(memory_dir: Path, layer_value: str, memory_id: str) -> Optional[bytes]:
    """Load a memory JSON file and render it as Markdown.

    Args:
        memory_dir: Root memory directory (``~/.skcapstone/memory``).
        layer_value: MemoryLayer value string (e.g. ``short-term``).
        memory_id: Memory ID without extension.

    Returns:
        UTF-8 encoded Markdown bytes, or None if not found/invalid.
    """
    path = memory_dir / layer_value / f"{memory_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _memory_to_markdown(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load memory %s: %s", path, exc)
        return None


def _list_memory_ids(memory_dir: Path, layer_value: str) -> List[str]:
    """List all memory IDs for a given layer.

    Args:
        memory_dir: Root memory directory.
        layer_value: MemoryLayer value string.

    Returns:
        List of memory IDs (without .json extension), sorted.
    """
    layer_dir = memory_dir / layer_value
    if not layer_dir.exists():
        return []
    return sorted(p.stem for p in layer_dir.glob("*.json"))


def _build_identity_card(agent_home: Path) -> bytes:
    """Build a JSON identity card from the CapAuth profile.

    Falls back to manifest data if CapAuth is unavailable.

    Args:
        agent_home: Agent home directory.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    # Try CapAuth profile
    capauth_profile = Path("~/.capauth/profile.json").expanduser()
    if capauth_profile.exists():
        try:
            data = json.loads(capauth_profile.read_text(encoding="utf-8"))
            card: Dict[str, Any] = {
                "name": data.get("name", "unknown"),
                "email": data.get("email", ""),
                "fingerprint": data.get("fingerprint", ""),
                "created_at": data.get("created_at", ""),
                "source": "capauth",
            }
            return json.dumps(card, indent=2).encode("utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    # Fall back to manifest
    manifest_path = agent_home / "manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            card = {
                "name": data.get("name", "unknown"),
                "fingerprint": data.get("identity", {}).get("fingerprint", ""),
                "created_at": data.get("created_at", ""),
                "source": "manifest",
            }
            return json.dumps(card, indent=2).encode("utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    return json.dumps({"name": "unknown", "fingerprint": "", "source": "fallback"}).encode("utf-8")


def _build_fingerprint_txt(agent_home: Path) -> bytes:
    """Extract the PGP fingerprint as plain text.

    Args:
        agent_home: Agent home directory.

    Returns:
        UTF-8 encoded fingerprint bytes (newline-terminated).
    """
    # Try CapAuth profile
    capauth_profile = Path("~/.capauth/profile.json").expanduser()
    if capauth_profile.exists():
        try:
            data = json.loads(capauth_profile.read_text(encoding="utf-8"))
            fp = data.get("fingerprint", "")
            if fp:
                return (fp + "\n").encode("utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    # Try manifest
    manifest_path = agent_home / "manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            fp = data.get("identity", {}).get("fingerprint", "")
            if fp:
                return (fp + "\n").encode("utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    return b"(no fingerprint)\n"


def _list_inbox(agent_home: Path) -> List[str]:
    """List files in the SKComm inbox.

    Args:
        agent_home: Agent home directory.

    Returns:
        Sorted list of inbox filenames.
    """
    inbox_dir = agent_home / "comms" / "inbox"
    if not inbox_dir.exists():
        return []
    return sorted(p.name for p in inbox_dir.iterdir() if p.is_file())


def _read_inbox_file(agent_home: Path, filename: str) -> Optional[bytes]:
    """Read a message from the SKComm inbox.

    Args:
        agent_home: Agent home directory.
        filename: Name of the inbox file.

    Returns:
        File contents as bytes, or None if not found.
    """
    path = agent_home / "comms" / "inbox" / filename
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _list_documents(agent_home: Path) -> List[str]:
    """List signed documents in the sovereign documents directory.

    Args:
        agent_home: Agent home directory.

    Returns:
        Sorted list of document filenames.
    """
    docs_dir = agent_home / "documents"
    if not docs_dir.exists():
        return []
    return sorted(p.name for p in docs_dir.iterdir() if p.is_file())


def _read_document(agent_home: Path, filename: str) -> Optional[bytes]:
    """Read a signed document.

    Args:
        agent_home: Agent home directory.
        filename: Name of the document file.

    Returns:
        File contents as bytes, or None if not found.
    """
    path = agent_home / "documents" / filename
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _list_coordination_tasks(agent_home: Path) -> List[str]:
    """List coordination task files.

    Args:
        agent_home: Agent home directory.

    Returns:
        Sorted list of task JSON filenames.
    """
    tasks_dir = agent_home / "coordination" / "tasks"
    if not tasks_dir.exists():
        return []
    return sorted(p.name for p in tasks_dir.glob("*.json"))


def _read_coordination_task(agent_home: Path, filename: str) -> Optional[bytes]:
    """Read a coordination task JSON file.

    Args:
        agent_home: Agent home directory.
        filename: Name of the task JSON file.

    Returns:
        File contents as bytes, or None if not found.
    """
    path = agent_home / "coordination" / "tasks" / filename
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# SKComm send helper
# ---------------------------------------------------------------------------


def _send_via_skcomm(agent_home: Path, recipient: str, message: str) -> bool:
    """Send a message via SKComm by writing to the outbox directory.

    Attempts to use the skcapstone CLI for delivery. Falls back to writing
    an envelope JSON file in the outbox directory.

    Args:
        agent_home: Agent home directory.
        recipient: Recipient agent name.
        message: Message content to deliver.

    Returns:
        True if the message was queued successfully.
    """
    # Try skcapstone comm send CLI
    try:
        result = subprocess.run(
            ["skcapstone", "comm", "send", recipient, "--message", message],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Sent message to %s via skcapstone CLI", recipient)
            return True
        logger.debug("skcapstone CLI send failed: %s", result.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("skcapstone CLI unavailable: %s", exc)

    # Fallback: write envelope JSON to outbox
    outbox_dir = agent_home / "comms" / "outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    envelope = {
        "recipient": recipient,
        "message": message,
        "queued_at": ts,
        "delivered": False,
    }
    envelope_name = f"{recipient}_{int(time.time())}.json"
    envelope_path = outbox_dir / envelope_name
    try:
        envelope_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        logger.info("Queued message to %s at %s", recipient, envelope_path)
        return True
    except OSError as exc:
        logger.error("Failed to queue message to %s: %s", recipient, exc)
        return False


# ---------------------------------------------------------------------------
# Path parser
# ---------------------------------------------------------------------------


def _parse_path(path: str) -> Tuple[str, ...]:
    """Parse a virtual FS path into clean components.

    Args:
        path: POSIX path string (e.g. ``/memories/short/abc123.md``).

    Returns:
        Tuple of path components with empty strings removed.
    """
    return tuple(p for p in path.strip("/").split("/") if p)


# ---------------------------------------------------------------------------
# SovereignFS
# ---------------------------------------------------------------------------


class SovereignFS:
    """FUSE Operations implementation for the sovereign virtual filesystem.

    Exposes agent memories, identity, inbox, outbox, and coordination tasks
    as a read-mostly virtual filesystem. Writing to ``/outbox/<agent>.msg``
    delivers a message via SKComm.

    This class is designed to be used with ``fusepy``:

    .. code-block:: python

        import fuse
        fs = SovereignFS(agent_home=Path("~/.skcapstone").expanduser())
        fuse.FUSE(fs, mount_point, nothreads=True, foreground=True)

    Args:
        agent_home: Sovereign agent home directory.
    """

    def __init__(self, agent_home: Path) -> None:
        self._home = agent_home
        self._memory_dir = agent_home / "memory"
        # Buffer for outbox writes: maps virtual path → bytes written so far
        self._outbox_buffers: Dict[str, bytes] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _memory_content(self, layer_slug: str, filename: str) -> Optional[bytes]:
        """Resolve and render a memory file.

        Args:
            layer_slug: Virtual layer slug (``short``, ``mid``, or ``long``).
            filename: Filename (``<id>.md``).

        Returns:
            Markdown bytes, or None if not found.
        """
        if filename.endswith(".md"):
            memory_id = filename[:-3]
        else:
            memory_id = filename
        layer_value = _LAYER_SLUG_TO_VALUE.get(layer_slug)
        if not layer_value:
            return None
        return _load_memory_file(self._memory_dir, layer_value, memory_id)

    def _resolve_file_content(self, parts: Tuple[str, ...]) -> Optional[bytes]:
        """Resolve path components to file content.

        Args:
            parts: Parsed path components.

        Returns:
            File content as bytes, or None if the path is not a file.
        """
        if not parts:
            return None

        top = parts[0]

        # /memories/short|mid|long/<id>.md
        if top == _MEMORIES_DIR and len(parts) == 3:
            return self._memory_content(parts[1], parts[2])

        # /identity/card.json or /identity/fingerprint.txt
        if top == _IDENTITY_DIR and len(parts) == 2:
            if parts[1] == "card.json":
                return _build_identity_card(self._home)
            if parts[1] == "fingerprint.txt":
                return _build_fingerprint_txt(self._home)

        # /inbox/<filename>
        if top == _INBOX_DIR and len(parts) == 2:
            return _read_inbox_file(self._home, parts[1])

        # /documents/<filename>
        if top == _DOCUMENTS_DIR and len(parts) == 2:
            return _read_document(self._home, parts[1])

        # /coordination/<task>.json
        if top == _COORDINATION_DIR and len(parts) == 2:
            return _read_coordination_task(self._home, parts[1])

        # /outbox/<agent>.msg — reads back from in-memory buffer
        if top == _OUTBOX_DIR and len(parts) == 2:
            path_key = "/" + "/".join(parts)
            return self._outbox_buffers.get(path_key, b"")

        return None

    def _is_dir(self, parts: Tuple[str, ...]) -> bool:
        """Check if a set of path components resolves to a virtual directory.

        Args:
            parts: Parsed path components.

        Returns:
            True if this path is a known virtual directory.
        """
        if not parts:
            return True  # root

        top = parts[0]

        if len(parts) == 1:
            return top in _TOP_LEVEL_DIRS

        if top == _MEMORIES_DIR and len(parts) == 2:
            return parts[1] in _MEMORY_SUBDIRS

        return False

    def _is_file(self, parts: Tuple[str, ...]) -> bool:
        """Check if path components resolve to a readable virtual file.

        Args:
            parts: Parsed path components.

        Returns:
            True if the path is a valid virtual file.
        """
        return self._resolve_file_content(parts) is not None

    def _file_size(self, parts: Tuple[str, ...]) -> int:
        """Return the byte size of a virtual file.

        Args:
            parts: Parsed path components.

        Returns:
            Size in bytes (0 if content is unavailable).
        """
        content = self._resolve_file_content(parts)
        return len(content) if content is not None else 0

    # ------------------------------------------------------------------
    # FUSE Operations
    # ------------------------------------------------------------------

    def getattr(self, path: str, fh: Optional[int] = None) -> Dict[str, Any]:
        """Return stat-like attributes for a path.

        Args:
            path: Virtual filesystem path.
            fh: Open file handle (unused).

        Returns:
            Stat attribute dictionary.

        Raises:
            OSError: With ``errno.ENOENT`` if the path does not exist.
        """
        parts = _parse_path(path)

        if self._is_dir(parts):
            nlink = 2 + len(_MEMORY_SUBDIRS) if parts and parts[0] == _MEMORIES_DIR and len(parts) == 1 else 2
            return _dir_stat(nlink=nlink)

        if self._is_file(parts):
            size = self._file_size(parts)
            writable = bool(parts) and parts[0] == _OUTBOX_DIR
            return _file_stat(size=size, writable=writable)

        raise OSError(errno.ENOENT, "No such file or directory", path)

    def readdir(self, path: str, fh: Optional[int]) -> List[str]:
        """Return directory listing for a virtual path.

        Args:
            path: Virtual filesystem path.
            fh: Open file handle (unused).

        Returns:
            List of entry names including ``.`` and ``..``.

        Raises:
            OSError: With ``errno.ENOENT`` if the path is not a directory.
        """
        parts = _parse_path(path)
        entries = [".", ".."]

        if not parts:
            # Root
            entries.extend(_TOP_LEVEL_DIRS)
            return entries

        top = parts[0]

        if top == _MEMORIES_DIR and len(parts) == 1:
            entries.extend(_MEMORY_SUBDIRS)
            return entries

        if top == _MEMORIES_DIR and len(parts) == 2:
            slug = parts[1]
            layer_value = _LAYER_SLUG_TO_VALUE.get(slug)
            if layer_value:
                ids = _list_memory_ids(self._memory_dir, layer_value)
                entries.extend(f"{mid}.md" for mid in ids)
            return entries

        if top == _IDENTITY_DIR and len(parts) == 1:
            entries.extend(_IDENTITY_FILES)
            return entries

        if top == _INBOX_DIR and len(parts) == 1:
            entries.extend(_list_inbox(self._home))
            return entries

        if top == _OUTBOX_DIR and len(parts) == 1:
            # List any buffered outbox files
            prefix = f"/{_OUTBOX_DIR}/"
            entries.extend(
                k[len(prefix):]
                for k in self._outbox_buffers
                if k.startswith(prefix)
            )
            return entries

        if top == _DOCUMENTS_DIR and len(parts) == 1:
            entries.extend(_list_documents(self._home))
            return entries

        if top == _COORDINATION_DIR and len(parts) == 1:
            entries.extend(_list_coordination_tasks(self._home))
            return entries

        raise OSError(errno.ENOENT, "No such file or directory", path)

    def open(self, path: str, flags: int) -> int:
        """Open a virtual file.

        Only read and write flags are honoured; outbox files accept writes.

        Args:
            path: Virtual filesystem path.
            flags: Open flags bitmask (os.O_RDONLY, os.O_WRONLY, os.O_RDWR, etc.).

        Returns:
            Always 0 (no per-fd state needed).

        Raises:
            OSError: With appropriate errno if the path is not accessible.
        """
        parts = _parse_path(path)

        is_write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        is_outbox = bool(parts) and parts[0] == _OUTBOX_DIR

        if is_write:
            if not is_outbox:
                raise OSError(errno.EACCES, "Read-only filesystem", path)
            # Initialize outbox buffer
            self._outbox_buffers[path] = b""
            return 0

        if not self._is_file(parts):
            raise OSError(errno.ENOENT, "No such file or directory", path)

        return 0

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """Read bytes from a virtual file.

        Args:
            path: Virtual filesystem path.
            size: Maximum number of bytes to return.
            offset: Byte offset to start reading from.
            fh: Open file handle (unused).

        Returns:
            Bytes slice from the file content.

        Raises:
            OSError: With ``errno.ENOENT`` if the path is not a file.
        """
        parts = _parse_path(path)
        content = self._resolve_file_content(parts)
        if content is None:
            raise OSError(errno.ENOENT, "No such file or directory", path)
        return content[offset : offset + size]

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """Write bytes to an outbox file, buffering until flush.

        Only ``/outbox/<agent_name>.msg`` paths are writable. On the first
        write the buffer is initialised; subsequent writes append.

        Args:
            path: Virtual filesystem path (must be under ``/outbox/``).
            data: Bytes to write.
            offset: Byte offset (used to detect new vs. appended writes).
            fh: Open file handle (unused).

        Returns:
            Number of bytes written.

        Raises:
            OSError: With ``errno.EACCES`` if the path is not in ``/outbox/``.
        """
        parts = _parse_path(path)
        if not parts or parts[0] != _OUTBOX_DIR:
            raise OSError(errno.EACCES, "Read-only filesystem", path)

        if path not in self._outbox_buffers or offset == 0:
            self._outbox_buffers[path] = b""

        buf = self._outbox_buffers.get(path, b"")
        self._outbox_buffers[path] = buf[:offset] + data
        return len(data)

    def create(self, path: str, mode: int, fi: Optional[Any] = None) -> int:
        """Create a new outbox file.

        Only ``/outbox/<agent_name>.msg`` paths may be created.

        Args:
            path: Virtual filesystem path.
            mode: File permission mode (stored but not enforced in virtual FS).
            fi: FUSE file info structure (unused).

        Returns:
            Always 0.

        Raises:
            OSError: With ``errno.EACCES`` if the path is not under ``/outbox/``.
        """
        parts = _parse_path(path)
        if not parts or parts[0] != _OUTBOX_DIR:
            raise OSError(errno.EACCES, "Read-only filesystem", path)

        self._outbox_buffers[path] = b""
        return 0

    def flush(self, path: str, fh: int) -> int:
        """Flush an outbox file buffer, delivering the message via SKComm.

        Called when an outbox file handle is closed. The accumulated buffer
        is interpreted as the message body; the filename (without ``.msg``)
        is used as the recipient agent name.

        Args:
            path: Virtual filesystem path.
            fh: Open file handle (unused).

        Returns:
            Always 0.
        """
        parts = _parse_path(path)
        if not parts or parts[0] != _OUTBOX_DIR:
            return 0

        filename = parts[-1]
        # Strip .msg suffix to get the recipient name
        recipient = filename[:-4] if filename.endswith(".msg") else filename

        message_bytes = self._outbox_buffers.get(path, b"")
        if not message_bytes:
            return 0

        try:
            message = message_bytes.decode("utf-8").strip()
        except UnicodeDecodeError:
            logger.warning("Outbox message for %s is not valid UTF-8", recipient)
            return 0

        if message:
            _send_via_skcomm(self._home, recipient, message)

        # Clear buffer after sending
        self._outbox_buffers.pop(path, None)
        return 0

    def release(self, path: str, fh: int) -> int:
        """Release a file handle, flushing outbox if needed.

        Args:
            path: Virtual filesystem path.
            fh: Open file handle.

        Returns:
            Always 0.
        """
        # Flush any remaining outbox data
        self.flush(path, fh)
        return 0

    def truncate(self, path: str, length: int, fh: Optional[int] = None) -> None:
        """Truncate a file in the outbox buffer.

        Args:
            path: Virtual filesystem path.
            length: Target length in bytes.
            fh: Open file handle (unused).

        Raises:
            OSError: With ``errno.EACCES`` if the path is not under ``/outbox/``.
        """
        parts = _parse_path(path)
        if not parts or parts[0] != _OUTBOX_DIR:
            raise OSError(errno.EACCES, "Read-only filesystem", path)

        buf = self._outbox_buffers.get(path, b"")
        self._outbox_buffers[path] = buf[:length]

    # Pass-through stubs for operations that the kernel may call
    def chmod(self, path: str, mode: int) -> int:
        """Ignore chmod on the virtual filesystem."""
        return 0

    def chown(self, path: str, uid: int, gid: int) -> int:
        """Ignore chown on the virtual filesystem."""
        return 0

    def utimens(self, path: str, times: Optional[Tuple[float, float]] = None) -> int:
        """Ignore utimens on the virtual filesystem."""
        return 0


# ---------------------------------------------------------------------------
# FUSEDaemon — lifecycle manager
# ---------------------------------------------------------------------------


class FUSEDaemon:
    """Lifecycle manager for the SovereignFS FUSE mount.

    Handles mounting, unmounting, and status checks for the sovereign
    virtual filesystem.

    Args:
        mount_point: Directory to mount the filesystem at.
            Defaults to ``~/.sovereign/mount/``.
        agent_home: Agent home directory.
            Defaults to ``~/.skcapstone``.
    """

    _PID_FILE = "fuse.pid"
    _STATE_FILE = "fuse_state.json"

    def __init__(
        self,
        mount_point: Optional[Path] = None,
        agent_home: Optional[Path] = None,
    ) -> None:
        self._mount_point = (
            mount_point or Path("~/.sovereign/mount")
        ).expanduser()
        self._agent_home = (agent_home or Path("~/.skcapstone")).expanduser()
        self._state_dir = self._agent_home / "fuse"

    def _state_file(self) -> Path:
        """Path to the FUSE daemon state file.

        Returns:
            Absolute path to the JSON state file.
        """
        return self._state_dir / self._STATE_FILE

    def _pid_file(self) -> Path:
        """Path to the FUSE daemon PID file.

        Returns:
            Absolute path to the PID file.
        """
        return self._state_dir / self._PID_FILE

    def _write_state(self, mounted: bool, pid: Optional[int] = None) -> None:
        """Persist the FUSE daemon state to disk.

        Args:
            mounted: Whether the filesystem is currently mounted.
            pid: Process ID of the FUSE daemon (if any).
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "mounted": mounted,
            "mount_point": str(self._mount_point),
            "agent_home": str(self._agent_home),
            "pid": pid,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_file().write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _read_state(self) -> Optional[Dict[str, Any]]:
        """Read the FUSE daemon state from disk.

        Returns:
            State dictionary, or None if missing or corrupt.
        """
        path = self._state_file()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _is_mounted(self) -> bool:
        """Check whether the mount point is currently active.

        Uses ``/proc/mounts`` on Linux for reliable detection.

        Returns:
            True if the mount point appears to be mounted.
        """
        mount_str = str(self._mount_point)

        # Linux: parse /proc/mounts
        proc_mounts = Path("/proc/mounts")
        if proc_mounts.exists():
            try:
                for line in proc_mounts.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == mount_str:
                        return True
            except OSError:
                pass
            return False

        # macOS / other: use mount command
        try:
            result = subprocess.run(
                ["mount"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return mount_str in result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def start(self, foreground: bool = False) -> bool:
        """Mount the sovereign virtual filesystem.

        Attempts to import ``fuse`` (fusepy) and mount the SovereignFS
        filesystem at the configured mount point. If ``foreground=False``
        the mount runs as a daemon process.

        Args:
            foreground: If True, mount in the foreground (blocks until unmounted).
                Useful for debugging.

        Returns:
            True if the mount was initiated successfully.
        """
        try:
            import fuse as _fuse  # type: ignore[import]
        except ImportError:
            logger.error(
                "fusepy is not installed. Install with: pip install skcapstone[fuse]"
            )
            return False

        if self._is_mounted():
            logger.info("Already mounted at %s", self._mount_point)
            return True

        self._mount_point.mkdir(parents=True, exist_ok=True)
        self._state_dir.mkdir(parents=True, exist_ok=True)

        if foreground:
            logger.info(
                "Mounting sovereign filesystem at %s (foreground)", self._mount_point
            )
            try:
                fs = SovereignFS(agent_home=self._agent_home)
                self._write_state(mounted=True, pid=os.getpid())
                _fuse.FUSE(
                    fs,
                    str(self._mount_point),
                    nothreads=True,
                    foreground=True,
                    allow_other=False,
                )
                return True
            except Exception as exc:
                logger.error("Failed to mount filesystem: %s", exc)
                self._write_state(mounted=False)
                return False
        else:
            # Background mount: re-exec this function in a child process
            logger.info(
                "Mounting sovereign filesystem at %s (background)", self._mount_point
            )
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "from skcapstone.fuse_mount import FUSEDaemon; "
                            f"FUSEDaemon("
                            f"  mount_point=Path({str(self._mount_point)!r}), "
                            f"  agent_home=Path({str(self._agent_home)!r})"
                            f").start(foreground=True)"
                        ),
                    ],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._write_state(mounted=True, pid=proc.pid)
                self._pid_file().write_text(str(proc.pid), encoding="utf-8")
                logger.info("FUSE daemon started with pid %d", proc.pid)
                return True
            except OSError as exc:
                logger.error("Failed to start FUSE daemon: %s", exc)
                self._write_state(mounted=False)
                return False

    def stop(self) -> bool:
        """Unmount the sovereign virtual filesystem.

        Attempts ``fusermount -u`` (Linux) or ``umount`` (macOS).

        Returns:
            True if the filesystem was successfully unmounted.
        """
        if not self._is_mounted():
            logger.info("Not mounted at %s", self._mount_point)
            self._write_state(mounted=False)
            return True

        mount_str = str(self._mount_point)

        # Linux: fusermount
        for cmd in (["fusermount", "-u", mount_str], ["umount", mount_str]):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    logger.info("Unmounted %s", mount_str)
                    self._write_state(mounted=False)
                    return True
                logger.debug(
                    "%s failed (rc=%d): %s",
                    " ".join(cmd), result.returncode, result.stderr.strip(),
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                logger.debug("Unmount command %s failed: %s", cmd, exc)

        logger.error("Could not unmount %s — try: fusermount -u %s", mount_str, mount_str)
        return False

    def status(self) -> Dict[str, Any]:
        """Return the current FUSE daemon status.

        Returns:
            Dictionary with keys:
                - ``mounted`` (bool): Whether the FS is currently mounted.
                - ``mount_point`` (str): Mount point path.
                - ``agent_home`` (str): Agent home path.
                - ``pid`` (int | None): Daemon process ID, if known.
                - ``updated_at`` (str | None): Last state update timestamp.
        """
        state = self._read_state() or {}
        mounted = self._is_mounted()

        return {
            "mounted": mounted,
            "mount_point": str(self._mount_point),
            "agent_home": str(self._agent_home),
            "pid": state.get("pid"),
            "updated_at": state.get("updated_at"),
        }
