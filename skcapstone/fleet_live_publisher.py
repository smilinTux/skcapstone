"""Publish one strictly host-local fleet worker liveness snapshot.

Pane evidence uses explicitly configured real tmux sockets. The packaged unit
shares the host /tmp namespace and names the established default socket.
Its opt-in process/socket audit requires every own-user tmux server to be
reachable, including runtime sockets. An absent default socket is empty only
when a successful own-user process view proves no tmux process exists.
Custom missing sockets, denied probes and unlinked live servers fail closed.
CLI failures publish separate non-authoritative systemd diagnostics.

Lane capacity is not measured here. The fleet rotation dispatcher
(its ``publish_live`` step) owns the lane table (target, busy, free
per lane): its targets are estate configuration in its own unit environment
and its codex ``free`` is bounded by live gateway route capacity, none of
which this strictly host-local oneshot can observe. This publisher therefore
carries the dispatcher's most recent lane table forward unchanged, stamped
with the time it was measured (``lanes_ts``), and publishes the explicit
marker ``"lanes": "unknown"`` when no sufficiently fresh measurement exists.
It never writes an empty lane map: ``{}`` reads as "zero free capacity" to
every consumer that sums ``free``, and an unmeasured host must never look
like a saturated one.
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

#: Explicit "no lane measurement available" marker. Deliberately not a dict:
#: the reader (the dispatcher's ``reporting_capacity``) skips a snapshot
#: whose ``lanes`` is not a dict, so an unmeasured host drops out of the
#: capacity map entirely instead of reporting capacity 0.
LANES_UNKNOWN = "unknown"

#: How long a dispatcher lane measurement stays worth republishing. Mirrors
#: the dispatcher's ``LIVE_FRESH`` fence (a report older than this says
#: nothing about now). Past this age the carried table is dropped
#: and the snapshot says ``unknown`` rather than laundering stale capacity
#: under a fresh snapshot timestamp.
_LANE_CAPACITY_FRESH = 30 * 60


def _carried_lane_capacity(target: Path, now_ts: float) -> tuple[dict | str, float | None]:
    """Return the dispatcher's lane table to carry forward, or the unknown marker.

    Args:
        target: The host snapshot path about to be replaced.
        now_ts: The timestamp the new snapshot will carry.

    Returns:
        ``(lanes, lanes_ts)`` where ``lanes`` is the prior snapshot's
        non-empty lane dict and ``lanes_ts`` the time it was measured, when
        that measurement is present, plausibly timestamped, and no older than
        ``_LANE_CAPACITY_FRESH``; otherwise ``(LANES_UNKNOWN, None)``. The
        measurement time is the prior snapshot's own ``lanes_ts`` when it has
        one (a snapshot this publisher wrote), else the prior ``ts`` (a
        snapshot the dispatcher wrote), so repeated publisher runs never
        refresh the provenance of a table they did not measure.
    """
    try:
        prior = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LANES_UNKNOWN, None
    if not isinstance(prior, dict):
        return LANES_UNKNOWN, None
    lanes = prior.get("lanes")
    if not isinstance(lanes, dict) or not lanes:
        return LANES_UNKNOWN, None
    try:
        lanes_ts = float(prior.get("lanes_ts") or prior.get("ts") or 0)
    except (TypeError, ValueError):
        return LANES_UNKNOWN, None
    if not 0 < lanes_ts <= now_ts or now_ts - lanes_ts > _LANE_CAPACITY_FRESH:
        return LANES_UNKNOWN, None
    return lanes, lanes_ts


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


def _tmux_server_pids(runner: Callable[..., object]) -> set[int]:
    """Read the current user's server processes, refusing a failed process view."""
    output = _probe(["ps", "-u", str(os.getuid()), "-o", "pid=,comm="], runner=runner)
    servers = set()
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            raise RuntimeError("malformed tmux process evidence")
        if fields[1] == "tmux: server":
            servers.add(int(fields[0]))
    return servers


def _audited_tmux_sessions(primary: Path, runner: Callable[..., object]) -> tuple[str, list[str]]:
    """Join every live server to a real socket, never assume an orphan is empty."""
    before = _tmux_server_pids(runner)
    candidates = {primary} if primary.exists() else set()
    for directory, pattern in (
        (Path(f"/tmp/tmux-{os.getuid()}"), "*"),
        (Path(f"/run/user/{os.getuid()}/skfleet"), "tmux*.sock"),
    ):
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.match(pattern) and stat.S_ISSOCK(entry.stat().st_mode):
                candidates.add(entry)
    observed = set()
    sessions = []
    sockets = []
    for path in sorted(candidates):
        pid = _probe(
            ["tmux", "-S", str(path), "display-message", "-p", "#{pid}"], runner=runner
        ).strip()
        if not pid.isdigit():
            raise RuntimeError(f"invalid tmux server identity at {path}")
        names = _probe(["tmux", "-S", str(path), "ls", "-F", "#{session_name}"], runner=runner)
        observed.add(int(pid))
        sessions.extend(names.splitlines())
        sockets.append(str(path))
    after = _tmux_server_pids(runner)
    if before != after or observed != after:
        raise RuntimeError(
            f"tmux server coverage incomplete: processes={sorted(after)} "
            f"observed={sorted(observed)}"
        )
    return "\n".join(sessions), sockets


def publish_host_snapshot(
    *,
    home: Path,
    host: str,
    tmux_socket: str | None = None,
    allow_absent_default: bool = False,
    audit_tmux_servers: bool = False,
    store: object | None = None,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> Path:
    """Atomically publish current host-local workers without dispatching work.

    Args:
        home: SKCapstone sovereign home directory.
        host: Exact local host name used as the snapshot identity.
        tmux_socket: Explicit socket path, defaulting to SKFLEET_TMUX_SOCKET.
            The packaged unit shares the producer host namespace.
        allow_absent_default: Permit an absent shared default socket only after
            a successful own-user process probe proves no tmux process exists.
            Requires the host shared /tmp namespace, as configured by the unit.
        audit_tmux_servers: Require all live own-user tmux servers to map to
            real default-directory or runtime-directory sockets.
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
    try:
        socket_path = _resolve_tmux_socket(tmux_socket)
    except RuntimeError as exc:
        raw = (tmux_socket or os.environ.get(_TMUX_SOCKET_ENV) or "").strip()
        socket_path = Path(raw).expanduser()
        if (
            not allow_absent_default
            or not isinstance(exc.__cause__, FileNotFoundError)
            or socket_path != Path(f"/tmp/tmux-{os.getuid()}/default")
        ):
            raise
        processes = _probe(["ps", "-u", str(os.getuid()), "-o", "comm="], runner=runner)
        if any(line.strip().startswith("tmux") for line in processes.splitlines()):
            raise RuntimeError("default socket absent but tmux processes exist") from exc
        sessions = ""
    else:
        sessions = _probe(
            ["tmux", "-S", str(socket_path), "ls", "-F", "#{session_name}"],
            runner=runner,
        )
    tmux_sockets = [str(socket_path)] if socket_path.exists() else []
    if audit_tmux_servers:
        sessions, tmux_sockets = _audited_tmux_sessions(socket_path, runner)
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
    timestamp = now()
    lane_target = home / "evidence" / "fleet-lanes" / f"{host}.json"
    lanes, lanes_ts = _carried_lane_capacity(
        lane_target if lane_target.exists() else target, timestamp
    )
    payload = {
        "host": host,
        "complete": True,
        "ts": timestamp,
        "cards": sorted(cards),
        "workers": workers,
        "lanes": lanes,
        "tmux_socket": str(socket_path),
        "tmux_sessions": sessions.splitlines(),
        "tmux_sockets": tmux_sockets,
        "systemd_units": [
            line.split()[0]
            for line in units.splitlines()
            if line.split() and line.split()[0].startswith("skfleet-worker-")
        ],
    }
    if lanes_ts is not None:
        payload["lanes_ts"] = lanes_ts
    return _write_snapshot(target, payload)


def _write_snapshot(target: Path, payload: dict) -> Path:
    """Atomically replace one observation without exposing partial JSON."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=f".{target.stem}.", delete=False
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


def publish_systemd_observation(
    *,
    home: Path,
    host: str,
    store: object | None = None,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
) -> Path:
    """Publish partial diagnostics outside the authoritative report directory.

    Successful systemd enumeration proves only the observed unit set, never
    absence of legacy tmux workers. Failed enumeration is unknown, not empty.
    """
    host = host.strip().lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("invalid fleet liveness host")
    cards = None
    workers = None
    try:
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
    except (RuntimeError, OSError, subprocess.SubprocessError):
        pass
    else:
        cards = sorted(
            {
                match.group(1)
                for line in units.splitlines()
                if line.split() and (match := _UNIT_RE.fullmatch(line.split()[0]))
            }
        )
        workers = []
        card_store = store or CardStore(home)
        for card in cards:
            try:
                identity = _claim_identity(card_store, card)
            except (OSError, TypeError, ValueError):
                identity = None
            if identity:
                workers.append(identity)
    return _write_snapshot(
        home / "evidence" / "fleet-live-diagnostics" / f"{host}.json",
        {
            "host": host,
            "ts": now(),
            "complete": False,
            "tmux": "unknown",
            "systemd": "unknown" if cards is None else "observed",
            "cards": cards,
            "workers": workers,
        },
    )


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
        publish_host_snapshot(
            home=args.home,
            host=args.host,
            tmux_socket=args.tmux_socket,
            allow_absent_default=os.environ.get("SKFLEET_ALLOW_ABSENT_DEFAULT_TMUX") == "1",
            audit_tmux_servers=os.environ.get("SKFLEET_AUDIT_TMUX_SERVERS") == "1",
        )
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"fleet-live-publisher: {exc}", file=sys.stderr)
        try:
            diagnostic = publish_systemd_observation(home=args.home, host=args.host)
            print(f"fleet-live-publisher: partial diagnostics: {diagnostic}", file=sys.stderr)
        except (ValueError, OSError) as diagnostic_error:
            print(f"fleet-live-publisher: diagnostics failed: {diagnostic_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
