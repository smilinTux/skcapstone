"""Publish one strictly host-local fleet worker liveness snapshot.

Pane evidence is read only through an explicit tmux socket path (the
``--tmux-socket`` argument or the ``SKFLEET_TMUX_SOCKET`` environment
variable). The socket must live in a namespace the publisher shares, such as
the user runtime directory below ``/run/user/$UID``; the packaged unit sets
it explicitly and keeps ``PrivateTmp=yes``. Any missing, non-socket, or
unreachable path fails closed: no snapshot is written and the last published
evidence stays intact.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket as socket_module
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from skcoord.card_store import CardStore

_SESSION_RE = re.compile(r"^(?:codex|glm|qwen|kimi|esc)-auto-([0-9a-f]{8})$")
_UNIT_RE = re.compile(r"^skfleet-worker-(?:codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$")
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
_TMUX_SOCKET_ENV = "SKFLEET_TMUX_SOCKET"


def _resolve_tmux_socket(explicit: str | None) -> Path:
    """Return the validated explicit tmux socket path or fail closed.

    Args:
        explicit: Socket path from the command line, if any.

    Returns:
        Absolute path to an existing unix socket file.

    Raises:
        RuntimeError: No explicit socket was configured, or the configured
            path is missing or is not a socket file. A missing socket is
            never treated as an empty process view, and an existing regular
            file is rejected so ``tmux -S`` cannot silently spawn a server
            and manufacture a false authoritative-looking socket.
    """
    raw = (explicit or os.environ.get(_TMUX_SOCKET_ENV) or "").strip()
    if not raw:
        raise RuntimeError(
            f"explicit tmux socket required: pass --tmux-socket or set {_TMUX_SOCKET_ENV}"
        )
    socket_path = Path(raw).expanduser()
    if not socket_path.is_absolute():
        raise RuntimeError("explicit tmux socket path must be absolute")
    try:
        mode = socket_path.stat().st_mode
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"explicit tmux socket {socket_path} is not present; "
            "refusing to publish without authoritative pane evidence"
        ) from exc
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(
            f"explicit tmux socket {socket_path} is not a socket; "
            "refusing to publish without authoritative pane evidence"
        )
    return socket_path


def _probe(command: list[str], *, runner: Callable[..., object]) -> str:
    """Run one read-only local process probe and return its stdout."""
    completed = runner(command, capture_output=True, text=True, timeout=10)
    if getattr(completed, "returncode", 1) != 0:
        raise RuntimeError(f"{command[0]} liveness probe failed")
    return str(getattr(completed, "stdout", ""))


def _claim_identity(store: object, card_id: str) -> dict[str, str] | None:
    """Return the exact current claim identity for one observed worker."""
    folded = store.fold(card_id)
    if isinstance(folded, dict):
        owner = str(folded.get("owner") or "")
        meta = folded.get("meta") or {}
    else:
        owner = str(getattr(folded, "owner", "") or "")
        meta = getattr(folded, "meta", {}) or {}
    revision = str(meta.get("_claim_revision") or "")
    if not owner or not revision:
        return None
    return {"card_id": card_id, "owner": owner, "claim_revision": revision}


def publish_host_snapshot(
    *,
    home: Path,
    host: str,
    tmux_socket: str | None = None,
    store: object | None = None,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> Path:
    """Atomically publish current host-local workers without dispatching work.

    Args:
        home: SKCapstone sovereign home directory.
        host: Exact local host name used as the snapshot identity.
        tmux_socket: Explicit namespace-safe tmux socket path; defaults to
            the ``SKFLEET_TMUX_SOCKET`` environment variable. There is no
            implicit default-socket discovery, which a private ``/tmp``
            namespace can shadow into a false empty view.
        store: Optional CardStore-compatible reader for tests.
        runner: Read-only subprocess runner for local process probes.
        now: Clock used for the snapshot timestamp.

    Returns:
        Path to the atomically replaced host snapshot.

    Raises:
        RuntimeError: Authoritative pane evidence could not be obtained or a
            local runtime probe failed, leaving old evidence intact.
        ValueError: The host identity is unsafe or malformed.
    """
    host = host.strip().lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("invalid fleet liveness host")
    socket_path = _resolve_tmux_socket(tmux_socket)
    sessions = _probe(
        ["tmux", "-S", str(socket_path), "ls", "-F", "#{session_name}"],
        runner=runner,
    )
    units = _probe(
        [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--state=running",
            "--no-legend",
            "--plain",
        ],
        runner=runner,
    )
    cards = {
        match.group(1)
        for line in sessions.splitlines()
        if (match := _SESSION_RE.fullmatch(line.strip()))
    }
    cards.update(
        match.group(1)
        for line in units.splitlines()
        if line.split() and (match := _UNIT_RE.fullmatch(line.split()[0]))
    )
    card_store = store or CardStore(home)
    workers = []
    for card_id in sorted(cards):
        try:
            identity = _claim_identity(card_store, card_id)
        except (OSError, TypeError, ValueError):
            identity = None
        if identity:
            workers.append(identity)

    target = home / "evidence" / "fleet-live" / f"{host}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": host,
        "ts": now(),
        "cards": sorted(cards),
        "workers": workers,
        "lanes": {},
        "tmux_socket": str(socket_path),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=f".{host}.", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> int:
    """Publish one host-local snapshot and exit without dispatch authority."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--host", default=socket_module.gethostname())
    parser.add_argument(
        "--tmux-socket",
        default=None,
        help=f"Explicit namespace-safe tmux socket (default: ${_TMUX_SOCKET_ENV})",
    )
    args = parser.parse_args()
    try:
        publish_host_snapshot(home=args.home, host=args.host, tmux_socket=args.tmux_socket)
    except (RuntimeError, ValueError) as exc:
        print(f"fleet-live-publisher: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
