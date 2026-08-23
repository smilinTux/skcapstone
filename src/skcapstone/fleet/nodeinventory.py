"""Read-only node inventory: what is actually installed and enabled here.

Epic 3bbf39ea, card 39e8a061 (parent cd5ef08b). The profile layer compares a
node's declared profile against what the node really has. This module is the
"really has" half, and it is the input for authoring the profile manifests
(card 1d300b19) as well as for the drift diff (profile_doctor.py).

OBSERVE ONLY. This module has no actuation verbs and never gains any: it
reads unit files and package metadata, and that is the whole contract. The
drift report it feeds is report-only by design, so an inventory bug can
produce a wrong *finding*, never a wrong *change*.

Every command goes through the injectable Runner from actuation.py, so tests
never shell out, and every failure degrades to an empty result exactly as
actuation.systemd_state() degrades to unknown. A node that cannot be
inventoried reports nothing, which the diff renders as "unknown", never as
"everything is missing, remove it all".
"""

from __future__ import annotations

from datetime import datetime, timezone

from .actuation import Runner, default_runner

#: SK ecosystem packages that do not carry the ``sk`` prefix.
SK_PACKAGE_EXTRAS = frozenset({"capauth", "cloud9"})

#: Timeout-bounded read commands, one per scope.
_USER_UNITS_CMD = ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend"]
_SYSTEM_UNITS_CMD = ["systemctl", "list-unit-files", "--state=enabled", "--no-legend"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_sk_package(name: str) -> bool:
    """True when a distribution belongs to the SK namespace.

    The ``sk`` prefix plus the two ecosystem packages that predate the
    convention. Matching is case-insensitive because distribution metadata
    is not consistent about it.
    """
    lowered = name.strip().lower()
    return lowered.startswith("sk") or lowered in SK_PACKAGE_EXTRAS


def _parse_unit_files(stdout: str) -> dict[str, str]:
    """Parse ``list-unit-files --no-legend`` into {unit: state}.

    Lines look like ``comfyui.service   enabled   enabled``: unit, state,
    and an optional vendor preset column. Anything that does not split into
    at least two fields is skipped rather than guessed at.
    """
    units: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        units[parts[0]] = parts[1]
    return dict(sorted(units.items()))


def enabled_units(*, user: bool = True, runner: Runner | None = None) -> dict[str, str]:
    """Enabled unit files in one systemd scope, sorted by unit name.

    Args:
        user: True for the ``--user`` scope, False for the system scope.
        runner: Injected command runner; defaults to actuation.default_runner.

    Returns:
        {unit name: enablement state}, empty when the scope cannot be read.
    """
    run = runner if runner is not None else default_runner
    cmd = _USER_UNITS_CMD if user else _SYSTEM_UNITS_CMD
    try:
        out = run(list(cmd))
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    return _parse_unit_files(out.stdout or "")


def sk_packages() -> dict[str, str]:
    """Installed SK-namespace distributions and their versions, sorted.

    Reads installed package metadata in-process; there is no command to run
    and nothing to inject. Degrades to {} when the metadata store is
    unreadable. When a name appears more than once (the same distribution
    visible on two sys.path entries), the first version wins, so the result
    stays deterministic instead of depending on path order ties.
    """
    try:
        from importlib.metadata import distributions
    except Exception:
        return {}
    found: dict[str, str] = {}
    try:
        for dist in distributions():
            try:
                name = dist.metadata["Name"]
            except Exception:
                name = None
            if not name or not is_sk_package(name):
                continue
            found.setdefault(name.strip(), dist.version or "")
    except Exception:
        return dict(sorted(found.items()))
    return dict(sorted(found.items()))


def collect(
    *,
    runner: Runner | None = None,
    include_system: bool = False,
    now_iso: str | None = None,
) -> dict:
    """Observe this node: enabled units plus installed SK packages.

    Args:
        runner: Injected command runner; defaults to actuation.default_runner.
        include_system: Also inventory the system scope. Off by default: the
            user scope is where SK services live, and the system scope is
            mostly distro baseline that would drown the diff.
        now_iso: Override the collection timestamp (tests, and callers that
            need a stable value).

    Returns:
        ``{"units": {"user": {...}[, "system": {...}]}, "packages": {...},
        "collectedAt": iso}``. The ``system`` key is present only when it was
        actually collected, so an absent scope is never confused with an
        empty one.

    Note:
        ``collectedAt`` changes on every call. store.write_node_file() is
        write-on-change, so a caller embedding this into node.json must strip
        the timestamp first (see ``body()``) or the file rewrites every 60s
        and floods the control-bus folder.
    """
    units: dict[str, dict[str, str]] = {"user": enabled_units(user=True, runner=runner)}
    if include_system:
        units["system"] = enabled_units(user=False, runner=runner)
    return {
        "units": units,
        "packages": sk_packages(),
        "collectedAt": now_iso if now_iso is not None else _now_iso(),
    }


def body(inventory: dict) -> dict:
    """The deterministic half of an inventory: everything but the timestamp.

    Two collections of an unchanged node compare equal through this view.
    Publishers that write into a write-on-change file must use it.
    """
    return {key: value for key, value in inventory.items() if key != "collectedAt"}
