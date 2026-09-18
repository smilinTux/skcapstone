"""Cross-host seat lease: one arbiter host, one flock, one winner.

Spec ``docs/superpowers/sdd/2026-09-18-seat-lease``, Task 1. The estate's
existing claim fence is ``fcntl.flock`` on a card directory under
``~/.skcapstone``, and it provably does not exclude across hosts: that tree
is a per-host local ext4 volume replicated asynchronously by Syncthing, so
the "same" card has a different inode on every machine and each host only
ever flocks its own copy. Measured on 2026-09-18:

    chiap01 card 000068c8  inode 46946198
    chiap08 card 000068c8  inode 54037090

A lease file that lives on exactly ONE host does not have that problem: one
kernel arbitrates for everyone who reaches it. This module always operates
against a caller-supplied lease host over ``ssh``, wrapping the whole
read-modify-write in a single remote Python process so no half-done state
is ever observable between two callers.

Two layers:

``_locked_apply`` is the actual critical section. It opens a lock file,
takes ``fcntl.flock(LOCK_EX)`` (a real ``flock(2)``, contended correctly
across processes AND across threads within one process, unlike POSIX
``fcntl()`` record locks), and only then reads, decides, and writes the
lease file, before releasing the lock. This is the function that runs on
the lease host: the public API below ships it there as one ``python3 -c``
argv, inside one ``ssh`` call, so "read the lease, decide it is expired,
write mine" is one indivisible remote operation instead of two ``ssh``
round trips a second caller could interleave between.

Clock skew: expiry is computed from ``time.time()`` called INSIDE
``_locked_apply``, which runs on the lease host, not on the caller's
machine. A caller with a fast or slow clock cannot make a live lease look
expired (or a fresh one look stale) because the caller's clock is never
consulted; the one machine every party already has to reach is also the
one whose clock arbitrates. This is deliberate: computing "now" locally and
shipping a timestamp over ``ssh`` would let clock skew reintroduce the
double-dispatch this primitive exists to prevent.

``acquire``/``renew``/``release``/``current_holder`` are the public API a
caller on a different host uses. Each builds one ``ssh`` command (the
injectable ``actuation.Runner``, so tests never shell out to a real host),
parses the JSON line ``_locked_apply`` printed, and degrades any transport
failure (nonzero exit, an exception, unparsable output) to a failed
``LeaseResult`` rather than raising: a caller asking "can I run the seats"
across a flaky link should get a usable answer, not a stack trace.

This module enables nothing by itself. Nothing here schedules a call,
picks a lease host, or wires into the seat cycle; Task 2 does that,
deliberately, behind its own default-off switch.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .actuation import Runner, default_runner
from .paths import valid_name

#: Where the lease and its guarding lock file live, ON THE LEASE HOST,
#: expanded against THAT host's home directory (by the remote process, not
#: the caller). Callers may override either for testing or to point at a
#: different resource; production callers (Task 2) are expected to use the
#: defaults so every seat-lease caller contends for the same file.
DEFAULT_LEASE_PATH = "~/.skcapstone/fleet/seat-lease.json"
DEFAULT_LOCK_PATH = "~/.skcapstone/fleet/seat-lease.lock"


@dataclass(frozen=True)
class LeaseResult:
    """The answer to "did this operation win, and what is the lease state now".

    ``holder`` and ``remaining_seconds`` are populated whenever they are
    knowable, including on a *failed* acquire/renew: a caller told "no,
    chiap08 holds it for another 4 minutes" has received a useful answer,
    not an error. ``reason`` is always a short human-readable word or
    phrase, never blank, so an operator reading a log line never has to
    guess why an operation did what it did.
    """

    ok: bool
    holder: str | None
    remaining_seconds: float | None
    reason: str


#: Imported and called by name inside the remote ``python3 -c`` process
#: this module ships over ``ssh``. Assumes ``skcapstone`` is installed on
#: the lease host, the same assumption ``staged_rollout.py``'s own
#: ``_RECORD_SNIPPET`` and ``_PREVIOUS_MANIFEST_SNIPPET`` already make for
#: remote calls in this fleet.
_REMOTE_SNIPPET = (
    "import json, sys; "
    "from skcapstone.fleet.seat_lease import _locked_apply; "
    "print(json.dumps(_locked_apply(sys.argv[1], sys.argv[2], float(sys.argv[3]), "
    "sys.argv[4], sys.argv[5])))"
)


def _ssh(host: str, remote_command: str) -> list[str]:
    return ["ssh", host, "bash", "-lc", remote_command]


# --------------------------------------------------------------------------
# layer 1: the real critical section (runs ON the lease host)
# --------------------------------------------------------------------------


def _read_lease(lease_file: Path) -> dict[str, Any] | None:
    try:
        with lease_file.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "holder" not in data or "expires_at" not in data:
        return None
    return data


def _write_lease(lease_file: Path, holder: str, expires_at: float) -> None:
    lease_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = lease_file.with_suffix(lease_file.suffix + ".tmp")
    tmp.write_text(json.dumps({"holder": holder, "expires_at": expires_at}), encoding="utf-8")
    os.replace(tmp, lease_file)


def _clear_lease(lease_file: Path) -> None:
    try:
        lease_file.unlink()
    except FileNotFoundError:
        pass


def _decide(op: str, holder: str, ttl_seconds: float, lease_file: Path) -> dict[str, Any]:
    """The actual state machine. Pure given ``time.time()``; no I/O of its
    own beyond the one read and (sometimes) one write ``_locked_apply``
    already has the lock held for.
    """
    now = time.time()
    data = _read_lease(lease_file)
    live = data is not None and float(data["expires_at"]) > now
    current = data["holder"] if live else None
    remaining = max(0.0, float(data["expires_at"]) - now) if live else None

    if op == "acquire":
        if live and current != holder:
            return {
                "ok": False,
                "holder": current,
                "remaining_seconds": remaining,
                "reason": "held by another holder",
            }
        _write_lease(lease_file, holder, now + ttl_seconds)
        return {
            "ok": True,
            "holder": holder,
            "remaining_seconds": ttl_seconds,
            "reason": "acquired",
        }

    if op == "renew":
        if not live:
            return {
                "ok": False,
                "holder": None,
                "remaining_seconds": None,
                "reason": "no live lease",
            }
        if current != holder:
            return {
                "ok": False,
                "holder": current,
                "remaining_seconds": remaining,
                "reason": "not the current holder",
            }
        _write_lease(lease_file, holder, now + ttl_seconds)
        return {
            "ok": True,
            "holder": holder,
            "remaining_seconds": ttl_seconds,
            "reason": "renewed",
        }

    if op == "release":
        if live and current == holder:
            _clear_lease(lease_file)
        return {"ok": True, "holder": None, "remaining_seconds": None, "reason": "released"}

    if op == "current_holder":
        return {
            "ok": True,
            "holder": current,
            "remaining_seconds": remaining,
            "reason": "held" if live else "empty",
        }

    return {"ok": False, "holder": None, "remaining_seconds": None, "reason": f"unknown op: {op}"}


def _locked_apply(
    op: str, holder: str, ttl_seconds: float, lease_path: str, lock_path: str
) -> dict:
    """Take the flock, run ``_decide``, release the flock. One indivisible
    critical section: this is what makes "read, decide, write" atomic
    across every caller that reaches this host, whether that caller is
    ``ssh``-remote or (as in the direct unit tests below) this same
    process.
    """
    lock_file = Path(lock_path).expanduser()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return _decide(op, holder, ttl_seconds, Path(lease_path).expanduser())
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# layer 2: the public ssh-wrapped API (runs on the CALLER's host)
# --------------------------------------------------------------------------


def _require_name(name: str, what: str) -> None:
    if not valid_name(name):
        raise ValueError(f"not a valid {what}: {name!r}")


def _call(
    host: str,
    op: str,
    holder: str,
    ttl_seconds: float,
    *,
    runner: Runner,
    lease_path: str,
    lock_path: str,
) -> LeaseResult:
    remote_command = (
        f"python3 -c {shlex.quote(_REMOTE_SNIPPET)} "
        f"{shlex.quote(op)} {shlex.quote(holder)} {shlex.quote(str(ttl_seconds))} "
        f"{shlex.quote(lease_path)} {shlex.quote(lock_path)}"
    )
    try:
        result = runner(_ssh(host, remote_command))
    except Exception as exc:  # pragma: no cover - defensive, mirrors actuation.py
        return LeaseResult(ok=False, holder=None, remaining_seconds=None, reason=str(exc))
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "seat lease call failed").strip()
        return LeaseResult(ok=False, holder=None, remaining_seconds=None, reason=reason)
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return LeaseResult(
            ok=False, holder=None, remaining_seconds=None, reason="malformed lease response"
        )
    return LeaseResult(
        ok=bool(payload.get("ok")),
        holder=payload.get("holder"),
        remaining_seconds=payload.get("remaining_seconds"),
        reason=str(payload.get("reason", "")),
    )


def acquire(
    host: str,
    holder: str,
    ttl_seconds: float,
    *,
    runner: Runner = default_runner,
    lease_path: str = DEFAULT_LEASE_PATH,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> LeaseResult:
    """Try to take the lease on ``host`` for ``holder``, good for ``ttl_seconds``.

    Succeeds when the lease is unheld, expired, or already held by
    ``holder`` (a live re-acquire simply extends it). Fails, without
    raising, when a DIFFERENT holder's lease is still live -- that is a
    normal result an operator or a caller can act on, not an error.
    """
    _require_name(host, "host name")
    _require_name(holder, "holder name")
    return _call(
        host,
        "acquire",
        holder,
        ttl_seconds,
        runner=runner,
        lease_path=lease_path,
        lock_path=lock_path,
    )


def renew(
    host: str,
    holder: str,
    ttl_seconds: float,
    *,
    runner: Runner = default_runner,
    lease_path: str = DEFAULT_LEASE_PATH,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> LeaseResult:
    """Extend ``holder``'s lease on ``host``. Fails when ``holder`` is not
    the current LIVE holder, including when the lease already expired: a
    former holder that lapsed must go back through ``acquire`` like anyone
    else, never renew its way back in.
    """
    _require_name(host, "host name")
    _require_name(holder, "holder name")
    return _call(
        host,
        "renew",
        holder,
        ttl_seconds,
        runner=runner,
        lease_path=lease_path,
        lock_path=lock_path,
    )


def release(
    host: str,
    holder: str,
    *,
    runner: Runner = default_runner,
    lease_path: str = DEFAULT_LEASE_PATH,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> None:
    """Give up ``holder``'s lease on ``host``, if it is the live holder.

    A no-op (not an error) when ``holder`` does not hold a live lease,
    matching the void return: there is nothing useful to report back.
    """
    _require_name(host, "host name")
    _require_name(holder, "holder name")
    _call(host, "release", holder, 0.0, runner=runner, lease_path=lease_path, lock_path=lock_path)
    return None


def current_holder(
    host: str,
    *,
    runner: Runner = default_runner,
    lease_path: str = DEFAULT_LEASE_PATH,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> str | None:
    """Who holds the live lease on ``host`` right now, or ``None``."""
    _require_name(host, "host name")
    result = _call(
        host, "current_holder", "", 0.0, runner=runner, lease_path=lease_path, lock_path=lock_path
    )
    return result.holder
