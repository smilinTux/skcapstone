"""Publish one strictly host-local fleet worker liveness snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from skcoord.card_store import CardStore

_SESSION_RE = re.compile(r"^(?:codex|glm|qwen|kimi|esc)-auto-([0-9a-f]{8})$")
_UNIT_RE = re.compile(r"^skfleet-worker-(?:codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$")
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")


def _probe(
    command: list[str],
    *,
    runner: Callable[..., object],
    empty_errors: tuple[str, ...] = (),
) -> str:
    """Run one read-only local process probe and return its stdout."""
    completed = runner(command, capture_output=True, text=True, timeout=10)
    stderr = str(getattr(completed, "stderr", "")).lower()
    if getattr(completed, "returncode", 1) != 0 and not any(
        marker in stderr for marker in empty_errors
    ):
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
    store: object | None = None,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> Path:
    """Atomically publish current host-local workers without dispatching work.

    Args:
        home: SKCapstone sovereign home directory.
        host: Exact local host name used as the snapshot identity.
        store: Optional CardStore-compatible reader for tests.
        runner: Read-only subprocess runner for local process probes.
        now: Clock used for the snapshot timestamp.

    Returns:
        Path to the atomically replaced host snapshot.

    Raises:
        RuntimeError: A local runtime probe failed, leaving old evidence intact.
        ValueError: The host identity is unsafe or malformed.
    """
    host = host.strip().lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("invalid fleet liveness host")
    sessions = _probe(
        ["tmux", "ls", "-F", "#{session_name}"],
        runner=runner,
        empty_errors=("no server running", "no sessions", "failed to connect"),
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
    parser.add_argument("--host", default=socket.gethostname())
    args = parser.parse_args()
    publish_host_snapshot(home=args.home, host=args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
