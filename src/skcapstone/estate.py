"""Where estate-wide truth ends and host-local truth begins.

``~/.skcapstone`` is ONE Syncthing folder shared by every node in an estate.
Anything written there is seen by every host, so only ESTATE-WIDE truth may
live there: the operator, the realm, the product scope, and the roster of
seats the estate runs. HOST-SPECIFIC truth written into that tree is a bug
with a specific shape: every node inherits one host's answer, and you end up
with an estate where every node believes it is the control node.

Host-local truth belongs in the XDG directories, resolved from the
environment with the standard fallbacks and never from a hardcoded path:

    $XDG_CONFIG_HOME   (default ~/.config)      configuration
    $XDG_STATE_HOME    (default ~/.local/state) state that survives a reboot

Nothing in this module hardcodes a host name, an estate name, or a home
directory. Every value is derived from the local machine or read from the
estate's own declared files, so the same code produces the right answer on
the first host of a brand new estate as it does on an existing one.

ONE DELIBERATE EXCEPTION, stated loudly because it looks like a violation of
the rule above: ``coordination/seat-control-plane.json`` carries
``active_host`` and stays in the SYNCED tree. That field is not host-local
truth, it is an estate-wide ELECTION: its entire job is to guarantee that
exactly one host in the estate runs the six lifecycle seats. If each host
answered "am I the active host?" from its own XDG file, two hosts could both
answer yes and both dispatch the same cards, which is precisely the
duplicate-dispatch failure the gate exists to prevent. Estate separation is
already achieved structurally, because each estate is a separate Syncthing
share, so nor and chi each get their own file naming their own active host.
A second estate therefore needs nothing host-local here; it needs its own
file, which is what ``coord bootstrap`` now creates from the local hostname.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

#: Environment variable an operator may set to name the estate explicitly.
#: Estate identity is estate-wide, so the authoritative place for it is
#: ``cluster.json`` in the synced tree; this override exists for a host that
#: is bootstrapping before that file has synced.
ESTATE_ENV = "SKESTATE"

#: Host-local node name override, read by ``fleet.paths.self_node_name``.
NODE_ENV = "SKFLEET_NODE"

#: Host-local environment variables that must NEVER appear in the synced
#: tree. Each one answers a question about ONE machine, so a synced copy
#: hands every node the same wrong answer.
HOST_LOCAL_ENV_NAMES = (NODE_ENV, "SKFLEET_ROOT", "XDG_CONFIG_HOME", "XDG_STATE_HOME")


def xdg_config_home() -> Path:
    """Return ``$XDG_CONFIG_HOME``, falling back to ``~/.config``.

    Returns:
        The user's XDG configuration root. An unset or empty variable, and a
        relative path (which the XDG spec says to ignore), both fall back to
        the default rather than producing a path relative to the cwd.
    """
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / ".config"


def xdg_state_home() -> Path:
    """Return ``$XDG_STATE_HOME``, falling back to ``~/.local/state``.

    Returns:
        The user's XDG state root, with the same absolute-path rule as
        :func:`xdg_config_home`.
    """
    raw = os.environ.get("XDG_STATE_HOME", "").strip()
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / ".local" / "state"


def environment_d_dir() -> Path:
    """Return the host-local ``environment.d`` directory under XDG config.

    Returns:
        ``$XDG_CONFIG_HOME/environment.d``. This is where a variable that
        every shell and every user unit on THIS machine must see belongs,
        precisely because it is not shared with the rest of the estate.
    """
    return xdg_config_home() / "environment.d"


def local_host(host: str | None = None) -> str:
    """Return this machine's short host name, lowercased.

    Args:
        host: Override used by tests and by callers that already resolved a
            name. ``None`` asks the operating system.

    Returns:
        The first label of the host name, lowercased and stripped. Never a
        hardcoded literal, and never an FQDN, because the seat control plane
        compares against ``socket.gethostname()`` the same way.
    """
    raw = host if host is not None else socket.gethostname()
    return str(raw).strip().split(".")[0].lower()


def node_name(host: str | None = None) -> str:
    """Derive this machine's fleet node name from its host name.

    Deliberately ignores ``$SKFLEET_NODE``: this is the function that
    computes the value to PERSIST, so reading the variable it is meant to
    define would let one bad value copy itself forward forever.

    Args:
        host: Override host name, mainly for tests.

    Returns:
        ``node-<sanitized host>``, matching
        :func:`skcapstone.fleet.paths.self_node_name`'s own derivation so the
        persisted value and the computed fallback can never disagree.
    """
    safe = re.sub(r"[^a-z0-9-]", "-", local_host(host)).strip("-") or "unknown"
    return f"node-{safe}"


def _cluster_document(home: Path) -> dict:
    """Read the estate's ``cluster.json``, or an empty dict when unreadable.

    Args:
        home: The SKCapstone home (the synced estate tree).

    Returns:
        The parsed document, or ``{}``. Absent and malformed are the same
        answer on purpose: callers fall back to a derived value rather than
        failing a bootstrap on a file that has not synced yet.
    """
    try:
        value = json.loads((Path(home) / "cluster.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def estate_id(home: Path, *, estate: str | None = None, host: str | None = None) -> str:
    """Resolve the estate's short identifier without guessing.

    Resolution order, most explicit first:

    1. the ``estate`` argument (an operator typed it on the command line),
    2. ``$SKESTATE``,
    3. the ``estate`` key in the estate's own ``cluster.json``,
    4. the local host name.

    Step 4 is the honest fallback rather than string surgery on the host
    name. A three letter estate code like "nor" is a real fact about the
    estate, but nothing on disk states it, so deriving one by slicing
    "noroc2027" would be an invention. Using the whole host name produces a
    label that is unambiguous and true, and an operator who wants the short
    code states it in ``cluster.json`` where the rest of the estate can read
    it too.

    Args:
        home: The SKCapstone home (the synced estate tree).
        estate: Explicit override, usually from a CLI flag.
        host: Override host name, mainly for tests.

    Returns:
        A non-empty estate identifier.
    """
    for candidate in (estate, os.environ.get(ESTATE_ENV), _cluster_document(home).get("estate")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return local_host(host)


def estate_realm(home: Path) -> str | None:
    """Return the estate's realm from ``cluster.json``, or ``None``.

    Args:
        home: The SKCapstone home (the synced estate tree).

    Returns:
        The realm string, or ``None`` when the estate has not declared one.
        Absent is reported honestly; a guessed realm would be worse than
        none, the same rule ``coord_mail.sender_fqid`` already follows.
    """
    realm = _cluster_document(home).get("realm")
    return realm.strip() if isinstance(realm, str) and realm.strip() else None
