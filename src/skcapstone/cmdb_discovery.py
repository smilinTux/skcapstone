"""SKCapstone CMDB discovery extensions for governed shared ingress."""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import skcoord.cmdb_reconcile as upstream_reconcile
import skcoord.discovery as upstream
from skcoord.cmdb import CIType, make_ci_id
from skcoord.discovery_base import (
    AUTHORITY_DECLARED,
    AUTHORITY_OBSERVED,
    DISCOVERED_TAG,
    CommandRunner,
    DiscoveredCI,
    LocalRunner,
    SSHRunner,
    _utc_now,
)
from skcoord.discovery_connects import CONNECTS_COLLECTORS
from skcoord.discovery_runtime import _ephemeral_range
from skcoord.discovery_systemd import _classify_origin, _fragment_paths, _unit_dependencies

from .cmdb_ingress_declaration import (
    collect_declared_skgateway_ingress,
    systemd_service_identity,
)

logger = logging.getLogger("skcapstone.cmdb.discovery")

reconcile = upstream.reconcile
drift = upstream.drift
merge = upstream.merge
_upstream_scan_network = upstream_reconcile.scan_network

_LISTENER_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<extra>.*))?$")
_PROCESS_RE = re.compile(r"users:\(\(\"(?P<proc>[^\"]+)\"")
_UNIT_RE = re.compile(
    r"^(?P<unit>[\w@:.\\-]+)\.(?P<kind>service|socket|timer)\s+"
    r"(?P<load>\S+)\s+(?P<active>\S+)\s+(?P<sub>\S+)"
)


def _split_endpoint(local: str) -> tuple[str, int] | None:
    """Return an exact bind address and port from one ss endpoint."""

    address, separator, port_text = local.rpartition(":")
    if not separator or not port_text.isdigit():
        return None
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    return (address or "*", int(port_text))


def collect_listening_ports(runner: CommandRunner) -> list[DiscoveredCI]:
    """Collect one stable port CI with every observed exact bind address."""

    stdout = runner.run(["ss", "-tlnpH"])
    if stdout is None:
        stdout = runner.run(["ss", "-tlnH"])
    if stdout is None:
        return []
    low, high = _ephemeral_range(runner)
    grouped: dict[tuple[str, int], dict[str, set[str]]] = defaultdict(
        lambda: {"addresses": set(), "processes": set()}
    )
    for line in stdout.splitlines():
        match = _LISTENER_RE.match(line.strip())
        if not match:
            continue
        endpoint = _split_endpoint(match.group("local"))
        if endpoint is None:
            continue
        address, port = endpoint
        if low <= port <= high:
            continue
        item = grouped[("tcp", port)]
        item["addresses"].add(address)
        process = _PROCESS_RE.search(match.group("extra") or "")
        if process:
            item["processes"].add(process.group("proc")[:120])

    found: list[DiscoveredCI] = []
    for (protocol, port), values in sorted(grouped.items()):
        addresses = sorted(values["addresses"])
        processes = sorted(values["processes"])
        attributes: dict[str, Any] = {
            "port": port,
            "proto": protocol,
            "bind_addresses": addresses,
        }
        if len(addresses) == 1:
            attributes["bind"] = addresses[0]
        if processes:
            attributes["processes"] = processes
        if len(processes) == 1:
            attributes["process"] = processes[0]
        found.append(
            DiscoveredCI(
                ci_type=CIType.PORT.value,
                name=f"{runner.host}:{port}",
                source="ss",
                observed=True,
                node=runner.host,
                attributes=attributes,
                tags=("port", DISCOVERED_TAG),
                relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
            )
        )
    return found


def _unit_role(unit: str, kind: str) -> str:
    """Classify a systemd unit without collapsing its exact unit identity."""

    lowered = unit.casefold()
    if kind == "socket":
        return "socket"
    if "firewall" in lowered or "nftable" in lowered:
        return "firewall"
    if "proxy" in lowered or "ingress" in lowered:
        return "proxy"
    if "gateway" in lowered or "skgateway" in lowered:
        return "gateway"
    return "service"


def _service_identity(host: str, scope: str, unit_id: str) -> str:
    """Preserve observed service identity and distinguish other unit kinds."""

    unit, kind = unit_id.rsplit(".", 1)
    if kind == "service":
        return unit
    return systemd_service_identity(host, scope.lstrip("-"), unit_id)


def collect_systemd_units(
    runner: CommandRunner,
    scopes: Sequence[str] = ("--user", "--system"),
    kinds: Sequence[str] = ("service", "socket", "timer"),
) -> list[DiscoveredCI]:
    """Collect distinct role-aware service CIs for exact systemd unit IDs."""

    found: list[DiscoveredCI] = []
    for scope in scopes:
        rows: list[tuple[str, str, str, str, str]] = []
        for kind in kinds:
            stdout = runner.run(
                [
                    "systemctl",
                    scope,
                    "list-units",
                    f"--type={kind}",
                    "--all",
                    "--no-legend",
                    "--no-pager",
                    "--plain",
                ]
            )
            if stdout is None:
                continue
            for line in stdout.splitlines():
                match = _UNIT_RE.match(line.strip())
                if not match or match.group("kind") != kind:
                    continue
                if match.group("load") == "not-found":
                    continue
                rows.append(
                    (
                        match.group("unit"),
                        kind,
                        match.group("load"),
                        match.group("active"),
                        match.group("sub"),
                    )
                )
        unit_ids = [f"{unit}.{kind}" for unit, kind, *_ in rows]
        paths = _fragment_paths(runner, scope, unit_ids)
        dependencies = _unit_dependencies(runner, scope, unit_ids)
        observed_ids = set(unit_ids)
        roles = {unit_id: _unit_role(*unit_id.rsplit(".", 1)) for unit_id in unit_ids}
        for unit, kind, load, active, sub in rows:
            unit_id = f"{unit}.{kind}"
            role = roles[unit_id]
            identity = _service_identity(runner.host, scope, unit_id)
            relationships = [("runs_on", make_ci_id(CIType.HOST.value, runner.host))]
            if kind == "socket":
                target = f"{unit}.service"
                if target in observed_ids:
                    relationships.append(
                        (
                            "activates",
                            make_ci_id(
                                CIType.SERVICE.value,
                                _service_identity(runner.host, scope, target),
                            ),
                        )
                    )
            for dependency in sorted(dependencies.get(unit_id, set()) - {unit_id}):
                if dependency not in observed_ids:
                    continue
                relationships.append(
                    (
                        "depends_on",
                        make_ci_id(
                            CIType.SERVICE.value,
                            _service_identity(runner.host, scope, dependency),
                        ),
                    )
                )
            fragment = paths.get(unit_id, "")
            found.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=identity,
                    canonical_name="" if kind == "service" else identity,
                    aliases=(unit_id,),
                    source=f"systemd{scope}",
                    observed=True,
                    node=runner.host,
                    attributes={
                        "systemd_scope": scope.lstrip("-"),
                        "systemd_kind": kind,
                        "systemd_unit": unit_id,
                        "unit_role": role,
                        "load_state": load,
                        "active_state": active,
                        "sub_state": sub,
                        "fragment_path": fragment,
                        "origin": _classify_origin(fragment),
                    },
                    tags=("systemd", kind, role, DISCOVERED_TAG),
                    relationships=tuple(sorted(set(relationships))),
                )
            )
    return found


def _replace_collector(collectors: Iterable[Any], name: str, replacement: Any) -> tuple[Any, ...]:
    """Replace one upstream collector by stable function name."""

    return tuple(replacement if item.__name__ == name else item for item in collectors)


DECLARED_COLLECTORS = (
    *upstream.DECLARED_COLLECTORS,
    collect_declared_skgateway_ingress,
)
OBSERVED_COLLECTORS = _replace_collector(
    _replace_collector(
        upstream.OBSERVED_COLLECTORS,
        "collect_listening_ports",
        collect_listening_ports,
    ),
    "collect_systemd_units",
    collect_systemd_units,
)


def scan(
    home: Path,
    runners: Sequence[CommandRunner] = (),
    include_declared: bool = True,
) -> list[DiscoveredCI]:
    """Run upstream collectors with SKCapstone ingress extensions."""

    found: list[DiscoveredCI] = []
    scan_id = uuid.uuid4().hex
    observed_at = _utc_now()
    if include_declared:
        for collector in DECLARED_COLLECTORS:
            try:
                found.extend(
                    replace(
                        item,
                        scan_id=item.scan_id or scan_id,
                        authority=item.authority or AUTHORITY_DECLARED,
                    )
                    for item in collector(Path(home))
                )
            except Exception:
                if collector is collect_declared_skgateway_ingress:
                    raise
                logger.exception("declared collector failed: %s", collector.__name__)
    for runner in runners:
        for observer in (*OBSERVED_COLLECTORS, *CONNECTS_COLLECTORS):
            try:
                found.extend(
                    replace(
                        item,
                        observed_at=item.observed_at or observed_at,
                        scan_id=item.scan_id or scan_id,
                        authority=item.authority or AUTHORITY_OBSERVED,
                    )
                    for item in observer(runner)
                )
            except Exception:
                logger.exception("observed collector failed: %s", observer.__name__)
    return merge(found)


def scan_network(home: Path, *args: Any, **kwargs: Any) -> Any:
    """Preflight governed declarations before bounded network collection."""

    collect_declared_skgateway_ingress(Path(home))
    return _upstream_scan_network(home, *args, **kwargs)


__all__ = [
    "DECLARED_COLLECTORS",
    "LocalRunner",
    "OBSERVED_COLLECTORS",
    "SSHRunner",
    "collect_declared_skgateway_ingress",
    "collect_listening_ports",
    "collect_systemd_units",
    "drift",
    "merge",
    "reconcile",
    "scan",
    "scan_network",
]
