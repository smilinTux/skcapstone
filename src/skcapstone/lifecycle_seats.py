"""Canonical source-backed lifecycle seat profiles."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from .estate import estate_rotation_hosts, local_host

LIFECYCLE_SEATS = frozenset({"link", "mero", "seraph", "niobe", "atlas"})
PROFILE_FILENAME = "config/seat-role.json"
STARTUP_FILENAME = "config/lifecycle-startup.md"

#: The chi fleet exactly as it was when it was a literal in skfleet-rotate.py.
#: Used only when neither the estate nor SKFLEET_ROTATION_HOSTS declares a
#: roster, matching that script's own `_resolve_rotation_hosts` default.
_DEFAULT_ROTATION_HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def load_lifecycle_seat_profiles() -> dict[str, Any]:
    """Load and fail closed on the shipped lifecycle seat contract."""

    path = files("skcapstone").joinpath("data/lifecycle-seat-profiles.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "skfleet.lifecycle-seat-profiles/v1":
        raise ValueError("unsupported lifecycle seat profile schema")
    if set(value.get("seats", {})) != LIFECYCLE_SEATS:
        raise ValueError("lifecycle seat set does not match the canonical five seats")
    if value.get("default_model_route") != "sk-codex-mid":
        raise ValueError("lifecycle seats must default to sk-codex-mid")
    if value.get("default_model_profile") != "gpt-5.6-luna":
        raise ValueError("lifecycle seats must default to gpt-5.6-luna")
    if value.get("model_escalation_policy") != {
        "scope": "card",
        "mode": "opt_in",
        "automatic": False,
    }:
        raise ValueError("model escalation must be card-scoped and opt-in")
    if value.get("jarvis", {}).get("recurring_lifecycle") is not False:
        raise ValueError("Jarvis must remain outside recurring lifecycle work")
    return value


def load_seat_control_plane(active_host: str | None = None) -> dict[str, Any]:
    """Load the packaged five-seat control record, bound to one active host.

    The packaged file is a TEMPLATE: it ships the seat roster with an empty
    ``active_host`` and empty host lists, and this function fills both in
    from the local machine. It used to hardcode ``chiap08``, which meant the
    only estate this record could ever describe was the one it was written
    on, and a second estate got a control plane naming a host that is not
    even on its network.

    ``active_host`` stays in the record (and therefore in the synced
    coordination tree) on purpose: it is an estate-wide ELECTION, not
    host-local truth. Exactly one host per estate runs the five seats, and a
    per-host answer would let two hosts both claim the seat and dispatch the
    same cards twice. See :mod:`skcapstone.estate` for the full rule.

    Args:
        active_host: The host this estate elects to run the five seats.
            ``None`` derives it from the local machine.

    Returns:
        The resolved control record, with every seat pinned to the active
        host.

    Raises:
        ValueError: The packaged template is not schema 1, does not carry
            exactly the five lifecycle seats, or the resolved host is empty.
    """

    path = files("skcapstone").joinpath("data/seat-control-plane.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("unsupported seat control plane schema")
    if set(value.get("seats", {})) != LIFECYCLE_SEATS:
        raise ValueError("seat control plane must contain exactly five lifecycle seats")
    host = local_host(active_host)
    if not host:
        raise ValueError("seat control plane requires a non-empty active host")
    value["active_host"] = host
    value["seats"] = {seat: [host] for seat in sorted(LIFECYCLE_SEATS)}
    return value


def _rotation_hosts(home: Path | str | None = None) -> tuple[str, ...]:
    """Resolve the estate's rotation hosts, mirroring skfleet-rotate.py.

    Resolution is most explicit first: ``SKFLEET_ROTATION_HOSTS`` for a host
    bootstrapping before its estate record has synced, then the estate's
    declared roster, then the chi fleet default. This is the same order
    ``_resolve_rotation_hosts`` uses in the shipped script, so the placement
    manifest never names a host the dispatcher itself would refuse to rotate
    onto.

    Args:
        home: The estate home consulted for a declared roster.

    Returns:
        The resolved rotation hosts, lowercased.
    """
    raw = str(os.environ.get("SKFLEET_ROTATION_HOSTS", "") or "").strip()
    if raw:
        return tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    declared = estate_rotation_hosts(home)
    if declared:
        return tuple(str(part).strip().lower() for part in declared)
    return _DEFAULT_ROTATION_HOSTS


def generate_seat_placement_manifest(
    active_host: str | None = None, home: Path | str | None = None
) -> dict[str, Any]:
    """Build a schema-1 seat placement manifest covering every lifecycle seat.

    Nothing in the repository writes ``seat-placement.json``: it has been a
    hand-maintained file that the dispatcher
    (``scripts/fleet/skfleet-rotate.py::_load_seat_placement``) fails closed
    on when it is missing, malformed, or simply forgotten. This generates it
    from the same canonical roster, ``LIFECYCLE_SEATS``, that every other
    lifecycle-seat consumer already reads.

    Every seat maps to a list holding exactly ONE host, deliberately. The
    manifest format the dispatcher reads supports several hosts per seat, but
    that capacity stays unused: the CardStore claim fence does not exclude a
    concurrent second host (``~/.skcapstone`` is per-host local storage
    replicated by Syncthing, so ``fcntl.flock`` cannot reach across
    machines), so ``active_host`` in the seat control plane is the only
    cross-host exclusion this estate has. Listing two hosts for one seat
    would let both dispatch the same cards twice.

    Args:
        active_host: The host every seat is provisioned to. ``None`` derives
            it from the local machine.
        home: The estate home consulted for its declared rotation hosts.
            Defaults to the sovereign home.

    Returns:
        The generated manifest: ``{"schema_version": 1, "seats": {...}}``.

    Raises:
        ValueError: The resolved host is empty, or is not one of the
            estate's rotation hosts.
    """
    host = local_host(active_host)
    if not host:
        raise ValueError("seat placement manifest requires a non-empty active host")
    hosts = _rotation_hosts(home)
    if host not in hosts:
        raise ValueError(f"active host is not one of the estate's rotation hosts: {host}")
    return {
        "schema_version": 1,
        "seats": {seat: [host] for seat in sorted(LIFECYCLE_SEATS)},
    }


def write_seat_placement_manifest(
    path: Path | str, active_host: str | None = None, home: Path | str | None = None
) -> dict[str, Any]:
    """Generate the seat placement manifest and write it atomically.

    Args:
        path: Destination file path, for example
            ``~/.skcapstone/coordination/seat-placement.json``.
        active_host: Passed through to :func:`generate_seat_placement_manifest`.
        home: Passed through to :func:`generate_seat_placement_manifest`.

    Returns:
        The manifest that was written.
    """
    manifest = generate_seat_placement_manifest(active_host=active_host, home=home)
    _atomic_write(Path(path), _canonical_bytes(manifest))
    return manifest


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def converge_lifecycle_seats(home: Path, rollback_dir: Path) -> dict[str, Any]:
    """Idempotently install canonical profiles and control data with rollback."""

    home = Path(home)
    rollback_dir = Path(rollback_dir)
    profiles = load_lifecycle_seat_profiles()
    control = load_seat_control_plane()
    targets: list[tuple[Path, bytes]] = [
        (home / "coordination/seat-control-plane.json", _canonical_bytes(control))
    ]
    for seat in sorted(LIFECYCLE_SEATS):
        agent_home = home / "agents" / seat
        identity_path = agent_home / "identity/identity.json"
        if not identity_path.is_file():
            raise ValueError(f"missing sovereign identity for lifecycle seat: {seat}")
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if str(identity.get("name", "")).strip().lower() != seat:
            raise ValueError(f"lifecycle identity does not match agent home: {seat}")
        seat_profile = profiles["seats"][seat]
        document = {
            "schema": "sk.lifecycle-seat/v1",
            "seat": seat,
            "model_route": profiles["default_model_route"],
            "model_profile": profiles["default_model_profile"],
            "model_escalation_policy": profiles["model_escalation_policy"],
            "product_scope": profiles["product_scope"],
            "role": seat_profile["role"],
            "activation_state": seat_profile["activation_state"],
            "owns": seat_profile["owns"],
            "does_not_own": seat_profile["denies"],
            "mailbox": {"read": True, "acknowledge": True, "write": True},
            "mail_protocol": {
                "startup_hello": True,
                "startup_recipient": "all",
                "poll_interval_seconds": 300,
                "read_direct_and_all": True,
                "look_for_help_and_handoffs": True,
                "automatic_ack": False,
                "mail_is_authority": False,
            },
            "lifecycle_beat": {
                "enabled": True,
                "mode": "per_cycle",
                "interval_seconds": 300,
                "includes": ["seat", "host", "mailbox_poll_at", "status"],
                "stale_is_observation": True,
            },
            "safe_retirement": profiles["safe_retirement"],
            "card_label": seat_profile["card_label"],
            "timeout_seconds": seat_profile["timeout_seconds"],
        }
        startup = (
            f"# {seat.title()} lifecycle startup\n\n"
            "Scope: SKCapstone, SKDashboard, and SKWorld only.\n\n"
            f"Send `SEAT-HELLO-{seat}` to `all` at startup. Read the `{seat}` mailbox, "
            "including direct and `all` traffic, every five minutes. Look for help, "
            "handoffs, dependencies, and reviewer conflicts. Reply only within the "
            "seat role. Mail is not authority and must not be acknowledged before action.\n\n"
            "Emit one bounded lifecycle beat per cycle and retire safely while "
            "preserving receipts.\n"
        ).encode()
        targets.extend(
            [
                (agent_home / PROFILE_FILENAME, _canonical_bytes(document)),
                (agent_home / STARTUP_FILENAME, startup),
            ]
        )

    manifest: dict[str, Any] = {"schema": "skfleet.lifecycle-seat-rollback/v1", "files": []}
    for target, payload in targets:
        relative = target.relative_to(home).as_posix()
        prior = target.read_bytes() if target.exists() else None
        backup = rollback_dir / relative
        if prior is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(backup, prior)
        if prior != payload:
            _atomic_write(target, payload)
        manifest["files"].append(
            {
                "path": relative,
                "created": prior is None,
                "before_sha256": sha256(prior).hexdigest() if prior is not None else None,
                "after_sha256": sha256(payload).hexdigest(),
            }
        )
    manifest_bytes = _canonical_bytes(manifest)
    _atomic_write(rollback_dir / "manifest.json", manifest_bytes)
    return {**manifest, "sha256": sha256(manifest_bytes).hexdigest()}


def rollback_lifecycle_seats(home: Path, rollback_dir: Path) -> None:
    """Restore exactly the files captured by ``converge_lifecycle_seats``."""

    home = Path(home)
    rollback_dir = Path(rollback_dir)
    manifest = json.loads((rollback_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "skfleet.lifecycle-seat-rollback/v1":
        raise ValueError("unsupported lifecycle rollback manifest")
    for entry in manifest["files"]:
        target = home / entry["path"]
        if sha256(target.read_bytes()).hexdigest() != entry["after_sha256"]:
            raise ValueError(f"refusing rollback of modified file: {entry['path']}")
        if entry["created"]:
            target.unlink()
        else:
            backup = rollback_dir / entry["path"]
            if sha256(backup.read_bytes()).hexdigest() != entry["before_sha256"]:
                raise ValueError(f"rollback backup hash mismatch: {entry['path']}")
            _atomic_write(target, backup.read_bytes())


def main(argv: list[str] | None = None) -> int:
    """Converge or roll back the five lifecycle profiles."""

    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("converge", "rollback"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--rollback-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == "converge":
        print(json.dumps(converge_lifecycle_seats(args.home, args.rollback_dir), sort_keys=True))
    else:
        rollback_lifecycle_seats(args.home, args.rollback_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
