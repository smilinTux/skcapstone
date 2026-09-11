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
from dataclasses import dataclass
from importlib.resources import files
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


# ---------------------------------------------------------------------------
# Authority facts for the lifecycle gate
#
# The rule at the top of this module decides where each of these lives. The
# operator, the realm and the product scope are estate-wide, so they are read
# from the synced tree. A machine's claim to be the active lifecycle host is
# not: it is host-local, it lives under $XDG_CONFIG_HOME, and it may only ever
# REFUSE, never grant. The election that grants stays in the synced
# coordination record exactly as the module docstring's deliberate exception
# describes; the claim is a second lock on the same door, so a record that
# reached this machine by mis-sync or tampering cannot quietly activate a host
# whose own operator never declared it.
#
# Investigated and deliberately not used as the operator source:
# ``<home>/identity/identity.json``. On a live estate that file is the human
# operator's PGP identity (name, email, fingerprint) and carries no
# ``operator`` or ``realm`` key. Only the per-agent
# ``agents/<agent>/identity/identity.json`` files carry those, mirrored from
# cluster.json, so reading the estate operator from an agent's mirror would
# make the gate depend on which agent happened to be migrated last.
# ---------------------------------------------------------------------------

#: Schema of the estate-wide authority record, in the synced tree.
ESTATE_SCHEMA = "sk.estate-authority/v1"

#: Schema of the host-local lifecycle claim, under XDG config.
HOST_SCHEMA = "sk.lifecycle-host/v1"

#: Estate-wide authority record, relative to the SKCapstone home.
ESTATE_CONFIG_RELATIVE = "config/estate.json"

#: Host-local lifecycle claim, relative to ``$XDG_CONFIG_HOME``.
HOST_CONFIG_RELATIVE = "skcapstone/lifecycle-host.json"

#: Machine-wide cluster record, outside any user's home.
SYSTEM_CLUSTER_PATH = Path("/etc/skcapstone/cluster.json")

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class EstateConfigError(ValueError):
    """The estate's own configuration cannot answer an authorization question."""


@dataclass(frozen=True)
class EstateProfile:
    """The estate-wide facts the lifecycle gate is allowed to trust.

    Attributes:
        operator: The one identity whose authorization a lifecycle activation
            must carry, lowercased (for example ``casey`` or ``chef``).
        realm: The estate's realm, as used by the three-tier fqid grammar.
        product_scope: The exact set of products lifecycle seats may act on.
        source: Path of the file the operator and realm were read from, kept
            so a refusal can say which file was consulted.
    """

    operator: str
    realm: str
    product_scope: frozenset[str]
    source: str


def sovereign_home(home: Path | str | None = None) -> Path:
    """Resolve the synced SKCapstone home.

    Args:
        home: Explicit home, used by tests and by callers that already know
            which estate tree they are operating on.

    Returns:
        The estate home, from the argument, else ``$SKCAPSTONE_HOME``, else
        ``~/.skcapstone``.
    """
    if home is not None:
        return Path(home).expanduser()
    raw = os.environ.get("SKCAPSTONE_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".skcapstone"


def _read_json(path: Path) -> dict | None:
    """Read a JSON object, returning ``None`` when absent or not an object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _name(value: object, field: str, source: Path) -> str:
    """Coerce and validate one lowercase name field, failing closed.

    Args:
        value: The raw field value.
        field: Field name, for the error message.
        source: File the value came from, for the error message.

    Returns:
        The normalised name.

    Raises:
        EstateConfigError: When the value is empty or not a safe short name.
    """
    text = str(value or "").strip().lower()
    if not _NAME.fullmatch(text):
        raise EstateConfigError(f"estate {field} is missing or malformed in {source}")
    return text


def _default_product_scope() -> frozenset[str]:
    """The product scope shipped with the package, used when the estate is silent.

    The scope is a product fact (which repositories the lifecycle seats are
    built to touch) rather than an estate fact, so the packaged profile is a
    truthful default. An estate running a different scope states it in its own
    ``config/estate.json`` and the gate then compares against that.

    Returns:
        The packaged product scope.

    Raises:
        EstateConfigError: When the packaged profile declares no scope.
    """
    path = files("skcapstone").joinpath("data/lifecycle-seat-profiles.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    scope = frozenset(str(item).strip().lower() for item in value.get("product_scope", []))
    if not scope:
        raise EstateConfigError("packaged lifecycle profile declares no product scope")
    return scope


def load_estate_profile(home: Path | str | None = None) -> EstateProfile:
    """Load the estate-wide authority facts, failing closed when absent.

    Resolution order, all inside the synced estate tree or machine-wide
    configuration, never host-local state:

    1. ``<home>/config/estate.json`` (schema ``sk.estate-authority/v1``), the
       explicit record an estate writes to state its own operator, realm, or
       product scope.
    2. ``<home>/cluster.json``, then ``/etc/skcapstone/cluster.json``: the
       same document :func:`estate_realm` and ``capauth.agent_identity`` read.

    Args:
        home: The estate home to read. Defaults to the sovereign home.

    Returns:
        The resolved :class:`EstateProfile`.

    Raises:
        EstateConfigError: When no consulted file yields a usable operator and
            realm. There is no default operator: an estate that has not said
            who authorizes its lifecycle work authorizes nobody.
    """
    root = sovereign_home(home)
    estate_path = root / ESTATE_CONFIG_RELATIVE
    estate = _read_json(estate_path)
    if estate is not None:
        if estate.get("schema") != ESTATE_SCHEMA:
            raise EstateConfigError(f"estate record schema must be {ESTATE_SCHEMA}")
        declared = estate.get("product_scope")
        if declared is None:
            scope = _default_product_scope()
        else:
            scope = frozenset(str(item).strip().lower() for item in declared)
            if not scope:
                raise EstateConfigError(f"estate product scope is empty in {estate_path}")
        return EstateProfile(
            operator=_name(estate.get("operator"), "operator", estate_path),
            realm=_name(estate.get("realm"), "realm", estate_path),
            product_scope=scope,
            source=str(estate_path),
        )

    for cluster_path in (root / "cluster.json", SYSTEM_CLUSTER_PATH):
        cluster = _read_json(cluster_path)
        if cluster is None:
            continue
        return EstateProfile(
            operator=_name(cluster.get("operator"), "operator", cluster_path),
            realm=_name(cluster.get("realm"), "realm", cluster_path),
            product_scope=_default_product_scope(),
            source=str(cluster_path),
        )

    raise EstateConfigError(
        f"no estate authority record: expected {estate_path} or {root / 'cluster.json'}"
    )


def host_lifecycle_claim(
    *, config_home: Path | str | None = None, host: str | None = None
) -> str | None:
    """This machine's own claim to be the estate's active lifecycle host.

    The claim never grants. The election stays estate-wide in
    ``coordination/seat-control-plane.json`` for the reason the module
    docstring gives, and this file can only ever add a refusal on top of it.
    A machine may claim only itself: the recorded name is compared against the
    machine actually executing, so copying the file to a second node refuses
    rather than promotes it.

    Args:
        config_home: Override the XDG config root (tests).
        host: Override the running host (tests).

    Returns:
        The claimed host name when this machine declares itself active, or
        ``None`` when it makes no claim, which is the common case.

    Raises:
        EstateConfigError: When the file exists but is malformed, carries the
            wrong schema, or names a machine other than the one running.
    """
    root = Path(config_home).expanduser() if config_home is not None else xdg_config_home()
    path = root / HOST_CONFIG_RELATIVE
    value = _read_json(path)
    if value is None:
        if path.exists():
            raise EstateConfigError(f"host lifecycle claim is malformed: {path}")
        return None
    if value.get("schema") != HOST_SCHEMA:
        raise EstateConfigError(f"host lifecycle claim schema must be {HOST_SCHEMA}")
    claimed = _name(value.get("active_host"), "active_host", path)
    running = local_host(host)
    if claimed != running:
        raise EstateConfigError(
            f"host lifecycle claim names {claimed} but this machine is {running}"
        )
    return claimed


#: Key an estate uses to name the worker hosts its rotation may dispatch to.
ROTATION_HOSTS_KEY = "rotation_hosts"


def estate_rotation_hosts(home: Path | str | None = None) -> tuple[str, ...] | None:
    """Return the worker hosts this estate declares, in the order it declares them.

    The fleet rotation partitions card OWNERSHIP by hashing each card id across
    this roster, so both its MEMBERSHIP and its ORDER are load bearing: change
    either and every card's owner moves, which is how two hosts end up believing
    they own the same card. That is exactly why the roster is stated once, by
    the estate, instead of each host deriving one for itself.

    Resolution order, most explicit first, reading the same files
    :func:`load_estate_profile` already consults:

    1. ``<home>/config/estate.json``
    2. ``<home>/cluster.json``
    3. ``/etc/skcapstone/cluster.json``

    The first file that declares the key answers. A file that is absent,
    unreadable, or simply silent on the key is not an answer, so an estate that
    has stated nothing gets ``None`` and the caller keeps its own default. That
    is what leaves an existing estate partitioning byte for byte as it did
    before this key existed.

    Args:
        home: The estate home to read. Defaults to the sovereign home.

    Returns:
        The declared hosts, or ``None`` when no consulted file declares any.

    Raises:
        EstateConfigError: When a file declares the key but the value is not a
            non-empty list of unique, well formed short host names. Failing
            closed matters more here than anywhere else in this module. A
            malformed operator refuses work, which is loud; a silently dropped
            or repeated host refuses nothing and instead hands two machines the
            same cards.
    """
    root = sovereign_home(home)
    for path in (root / ESTATE_CONFIG_RELATIVE, root / "cluster.json", SYSTEM_CLUSTER_PATH):
        document = _read_json(path)
        if document is None or ROTATION_HOSTS_KEY not in document:
            continue
        declared = document[ROTATION_HOSTS_KEY]
        if not isinstance(declared, list) or not declared:
            raise EstateConfigError(
                f"estate {ROTATION_HOSTS_KEY} must be a non-empty list in {path}"
            )
        hosts = tuple(_name(item, ROTATION_HOSTS_KEY, path) for item in declared)
        if len(set(hosts)) != len(hosts):
            raise EstateConfigError(f"estate {ROTATION_HOSTS_KEY} names a host twice in {path}")
        return hosts
    return None
