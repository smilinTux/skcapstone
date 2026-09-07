"""Fail-closed declared CMDB topology for governed SKGateway ingress."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path

from skcoord.cmdb import CIType, make_ci_id
from skcoord.discovery_base import DISCOVERED_TAG, DiscoveredCI
from skcoord.discovery_systemd import ORIGIN_OPERATOR

ROLE_RELATIONSHIP = {
    "socket": "activates",
    "proxy": "proxies_to",
    "firewall": "protects",
}
_SECRET_RE = re.compile(
    r"(?:api[_-]?key|bearer|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_ROLES = {"gateway", "socket", "proxy", "firewall"}
_BOUNDARIES = {"loopback", "lan", "tailscale"}
_CONTAINER_BRIDGES = tuple(
    ipaddress.ip_network(value)
    for value in (
        "172.17.0.0/16",
        "172.18.0.0/15",
        "172.20.0.0/14",
        "172.24.0.0/13",
    )
)


def _safe_metadata(value: object) -> bool:
    """Reject secret-looking keys and scalar values recursively."""

    if isinstance(value, dict):
        return all(
            isinstance(key, str) and not _SECRET_RE.search(key) and _safe_metadata(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_safe_metadata(item) for item in value)
    return not (isinstance(value, str) and _SECRET_RE.search(value))


def _validated_bind(item: object) -> tuple[str, str]:
    """Validate one exact governed address and boundary declaration."""

    if not isinstance(item, dict) or set(item) != {"address", "boundary"}:
        raise ValueError("bind declaration must contain only address and boundary")
    address_text = item.get("address")
    boundary = item.get("boundary")
    if not isinstance(address_text, str) or not isinstance(boundary, str):
        raise ValueError("bind address and boundary must be strings")
    if "%" in address_text:
        raise ValueError("zone-scoped bind addresses are not governed")
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError as exc:
        raise ValueError("bind address is malformed") from exc
    if boundary not in _BOUNDARIES:
        raise ValueError("bind boundary is unsupported")
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise ValueError("bind address is not governed unicast")
    if any(address in network for network in _CONTAINER_BRIDGES):
        raise ValueError("container-bridge bind address is prohibited")
    if boundary == "loopback" and not address.is_loopback:
        raise ValueError("loopback boundary does not match address")
    tailscale = address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
    if boundary == "tailscale" and not tailscale:
        raise ValueError("Tailscale boundary does not match address")
    if boundary == "lan" and (address.is_loopback or not address.is_private or tailscale):
        raise ValueError("LAN boundary does not match address")
    return str(address), boundary


def _declared_service_id(host: str, unit: str) -> str:
    return make_ci_id(CIType.SERVICE.value, f"{host}:system:{unit}")


def _validated_profile(profile: object, profile_ids: set[str]) -> tuple:
    """Validate one complete profile before emitting any CI."""

    expected = {"profile_id", "host", "protocol", "port", "binds", "units"}
    if not isinstance(profile, dict) or set(profile) != expected:
        raise ValueError("SKGateway ingress profile shape is invalid")
    profile_id = profile.get("profile_id")
    host = profile.get("host")
    protocol = profile.get("protocol")
    port = profile.get("port")
    if (
        not isinstance(profile_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,79}", profile_id)
        or profile_id in profile_ids
        or not isinstance(host, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,79}", host)
        or protocol != "tcp"
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        raise ValueError("SKGateway ingress profile identity is invalid")
    binds = [_validated_bind(item) for item in profile.get("binds", [])]
    if len(binds) != 3 or len({address for address, _ in binds}) != len(binds):
        raise ValueError("SKGateway ingress requires three unique bind addresses")
    if {boundary for _, boundary in binds} != _BOUNDARIES:
        raise ValueError("SKGateway ingress boundaries are incomplete")
    units = profile.get("units")
    if not isinstance(units, list) or len(units) != 4:
        raise ValueError("SKGateway ingress requires four unit roles")
    unit_rows: dict[str, tuple[str, tuple[str, ...]]] = {}
    for item in units:
        if not isinstance(item, dict) or set(item) != {"unit", "role", "targets"}:
            raise ValueError("SKGateway ingress unit shape is invalid")
        unit, role, targets = item.get("unit"), item.get("role"), item.get("targets")
        if (
            not isinstance(unit, str)
            or not re.fullmatch(r"[a-zA-Z0-9@_.:-]+\.(?:service|socket)", unit)
            or not isinstance(role, str)
            or role not in _ROLES
            or role in unit_rows
            or not isinstance(targets, list)
            or not all(isinstance(target, str) for target in targets)
        ):
            raise ValueError("SKGateway ingress unit identity is invalid")
        unit_rows[role] = (unit, tuple(targets))
    if set(unit_rows) != _ROLES:
        raise ValueError("SKGateway ingress unit roles are incomplete")
    known_units = {unit for unit, _ in unit_rows.values()}
    if any(target not in known_units for _, targets in unit_rows.values() for target in targets):
        raise ValueError("SKGateway ingress unit target is unknown")
    profile_ids.add(profile_id)
    return profile_id, host, protocol, port, binds, unit_rows


def _profile_cis(profile: tuple) -> list[DiscoveredCI]:
    profile_id, host, protocol, port, binds, unit_rows = profile
    address_rows = [
        {"address": address, "boundary": boundary} for address, boundary in sorted(binds)
    ]
    found = [
        DiscoveredCI(
            ci_type=CIType.PORT.value,
            name=f"{host}:{port}",
            source="declared:skgateway-ingress",
            node=host,
            attributes={
                "port": port,
                "proto": protocol,
                "bind_addresses": [item["address"] for item in address_rows],
                "network_boundaries": address_rows,
                "profile_id": profile_id,
            },
            tags=("port", "skgateway", "governed-ingress", DISCOVERED_TAG),
            relationships=(("runs_on", make_ci_id(CIType.HOST.value, host)),),
        )
    ]
    for role, (unit, targets) in sorted(unit_rows.items()):
        identity = f"{host}:system:{unit}"
        relationships = [
            ("runs_on", make_ci_id(CIType.HOST.value, host)),
            ("binds", make_ci_id(CIType.PORT.value, f"{host}:{port}")),
        ]
        relationship = ROLE_RELATIONSHIP.get(role, "depends_on")
        relationships.extend(
            (relationship, _declared_service_id(host, target)) for target in targets
        )
        found.append(
            DiscoveredCI(
                ci_type=CIType.SERVICE.value,
                name=identity,
                canonical_name=identity,
                aliases=(unit,),
                source="declared:skgateway-ingress",
                node=host,
                attributes={
                    "profile_id": profile_id,
                    "systemd_scope": "system",
                    "systemd_kind": unit.rsplit(".", 1)[1],
                    "systemd_unit": unit,
                    "unit_role": role,
                    "origin": ORIGIN_OPERATOR,
                },
                tags=("systemd", role, "skgateway", DISCOVERED_TAG),
                relationships=tuple(sorted(relationships)),
            )
        )
    return found


def collect_declared_skgateway_ingress(home: Path) -> list[DiscoveredCI]:
    """Read one all-or-nothing governed SKGateway ingress declaration."""

    path = Path(home) / "config" / "cmdb" / "skgateway-ingress.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("SKGateway ingress declaration is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "profiles"}:
        raise ValueError("SKGateway ingress declaration shape is invalid")
    if payload.get("schema") != "skcapstone.cmdb.skgateway-ingress/v1":
        raise ValueError("SKGateway ingress declaration schema is unsupported")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or not profiles or not _safe_metadata(payload):
        raise ValueError("SKGateway ingress declaration is unsafe")
    profile_ids: set[str] = set()
    validated = [_validated_profile(profile, profile_ids) for profile in profiles]
    return [item for profile in validated for item in _profile_cis(profile)]


__all__ = ["ROLE_RELATIONSHIP", "collect_declared_skgateway_ingress"]
