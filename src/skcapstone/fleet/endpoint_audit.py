"""Read-only reconciliation of fleet nodes against Tailscale peer status."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import store
from .paths import FleetPaths

_MAX_PEERS = 10_000


@dataclass(frozen=True)
class Peer:
    """The non-secret Tailscale fields needed for endpoint reconciliation."""

    node_id: str
    hostname: str
    dns_name: str
    ips: tuple[str, ...]
    online: bool
    last_seen: str
    os: str
    tags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "hostname": self.hostname,
            "dns_name": self.dns_name,
            "ips": list(self.ips),
            "online": self.online,
            "last_seen": self.last_seen,
            "os": self.os,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class DeclaredEndpoint:
    """A role-scoped endpoint declared by the canonical fleet object."""

    kind: str
    value: str

    @property
    def expected_os(self) -> str | None:
        suffix = self.kind.removeprefix("tailscale-")
        if suffix == "windows":
            return "windows"
        if suffix in {"linux", "wsl", "wsl2"}:
            return "linux"
        return None


def read_status(path: Path | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    """Read a fixture or execute the local read-only Tailscale status command."""
    if path is not None:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("Tailscale status must be a JSON object")
    return value


def _peers(status: Mapping[str, Any]) -> list[Peer]:
    raw = [status.get("Self") or {}]
    peer_map = status.get("Peer") or {}
    if not isinstance(peer_map, Mapping):
        raise ValueError("Tailscale Peer must be an object")
    raw.extend(peer_map.values())
    if len(raw) > _MAX_PEERS:
        raise ValueError(f"Tailscale status exceeds {_MAX_PEERS} peers")
    peers = []
    for item in raw:
        if not isinstance(item, Mapping) or not item.get("ID"):
            continue
        peers.append(
            Peer(
                node_id=str(item["ID"]),
                hostname=str(item.get("HostName") or "").strip().lower(),
                dns_name=str(item.get("DNSName") or "").strip().rstrip(".").lower(),
                ips=tuple(str(value) for value in (item.get("TailscaleIPs") or []) if value),
                online=bool(item.get("Online")),
                last_seen=str(item.get("LastSeen") or ""),
                os=str(item.get("OS") or ""),
                tags=tuple(str(value) for value in (item.get("Tags") or []) if value),
            )
        )
    return sorted(peers, key=lambda item: (item.hostname, item.node_id))


def _node_identity(
    payload: Mapping[str, Any],
) -> tuple[set[str], set[str], tuple[DeclaredEndpoint, ...]]:
    spec = payload.get("spec") or {}
    address = spec.get("address") or {}
    names = {
        str(value).strip().rstrip(".").lower()
        for value in (
            payload.get("name"),
            address.get("hostname"),
            address.get("ssh_alias"),
            *(spec.get("aliases") or []),
        )
        if value
    }
    declared = tuple(
        DeclaredEndpoint(kind=str(entry.get("kind") or ""), value=str(entry.get("value")))
        for entry in (spec.get("addresses") or [])
        if isinstance(entry, Mapping) and entry.get("value")
    )
    endpoints = {
        str(value)
        for value in (
            address.get("ip"),
            address.get("tailscale"),
            *(entry.value for entry in declared),
        )
        if value
    }
    return names, endpoints, declared


def _peer_names(peer: Peer) -> set[str]:
    names = {peer.hostname}
    if peer.dns_name:
        names.add(peer.dns_name)
        names.add(peer.dns_name.split(".", 1)[0])
    return {name for name in names if name}


def _node_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    spec = payload.get("spec") or {}
    raw = spec.get("tailscale") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("node spec.tailscale must be an object")
    allowed = raw.get("allowed_os") or []
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("node spec.tailscale.allowed_os must be a string list")
    maximum = raw.get("max_active_peers")
    if maximum is not None and (not isinstance(maximum, int) or maximum < 1):
        raise ValueError("node spec.tailscale.max_active_peers must be a positive integer")
    return {
        "allowed_os": sorted({item.strip().lower() for item in allowed if item.strip()}),
        "max_active_peers": maximum,
    }


def _policy_findings(active: list[Peer], policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    allowed = set(policy.get("allowed_os") or [])
    disallowed = [peer for peer in active if allowed and peer.os.lower() not in allowed]
    if disallowed:
        findings.append(
            {
                "kind": "disallowed_peer_os",
                "severity": "error",
                "allowed_os": sorted(allowed),
                "peer_ids": [peer.node_id for peer in disallowed],
            }
        )
    maximum = policy.get("max_active_peers")
    if maximum is not None and len(active) > maximum:
        findings.append(
            {
                "kind": "active_peer_limit_exceeded",
                "severity": "error",
                "maximum": maximum,
                "peer_ids": [peer.node_id for peer in active],
            }
        )
    return findings


def _declared_multi_runtime_routes(
    active: list[Peer], declared: tuple[DeclaredEndpoint, ...]
) -> list[dict[str, str]] | None:
    """Resolve an explicitly declared Windows plus Linux/WSL peer topology."""
    role_endpoints = [item for item in declared if item.expected_os is not None]
    if len(active) < 2 or len(role_endpoints) < 2:
        return None

    routes: list[dict[str, str]] = []
    routed_peers: set[str] = set()
    for endpoint in role_endpoints:
        owners = [peer for peer in active if endpoint.value in peer.ips]
        if len(owners) != 1 or owners[0].os.lower() != endpoint.expected_os:
            return None
        peer = owners[0]
        if peer.node_id in routed_peers:
            return None
        routed_peers.add(peer.node_id)
        routes.append(
            {
                "kind": endpoint.kind,
                "endpoint": endpoint.value,
                "peer_id": peer.node_id,
                "os": peer.os,
            }
        )
    if routed_peers != {peer.node_id for peer in active}:
        return None
    return sorted(routes, key=lambda item: (item["kind"], item["peer_id"]))


def audit(paths: FleetPaths, status: Mapping[str, Any]) -> dict[str, Any]:
    """Compare canonical node identities with peers without changing either source."""
    peers = _peers(status)
    reports = []
    for node in store.list_specs(paths, "node"):
        names, configured_endpoints, declared_endpoints = _node_identity(node)
        policy = _node_policy(node)
        matches = [
            peer
            for peer in peers
            if names.intersection(_peer_names(peer)) or configured_endpoints.intersection(peer.ips)
        ]
        if not matches:
            continue
        active = [peer for peer in matches if peer.online]
        stale = [peer for peer in matches if not peer.online]
        active_ips = {ip for peer in active for ip in peer.ips}
        findings = _policy_findings(active, policy)
        active_routes = (
            None if findings else _declared_multi_runtime_routes(active, declared_endpoints)
        )
        if len(matches) > 1 and active_routes is None:
            findings.append(
                {
                    "kind": "duplicate_tailscale_identity",
                    "severity": "warn",
                    "peer_ids": [peer.node_id for peer in matches],
                }
            )
        if stale and active:
            findings.append(
                {
                    "kind": "stale_registration",
                    "severity": "warn",
                    "peer_ids": [peer.node_id for peer in stale],
                }
            )
        if active_routes is not None:
            findings.append(
                {
                    "kind": "declared_multi_runtime",
                    "severity": "info",
                    "peer_ids": [item["peer_id"] for item in active_routes],
                }
            )
        elif len(active) > 1:
            findings.append(
                {
                    "kind": "ambiguous_active_endpoint",
                    "severity": "error",
                    "peer_ids": [peer.node_id for peer in active],
                }
            )
        elif len(active) == 1 and configured_endpoints.isdisjoint(active_ips):
            findings.append(
                {
                    "kind": "configured_endpoint_mismatch",
                    "severity": "error",
                    "configured": sorted(configured_endpoints),
                    "observed": sorted(active_ips),
                }
            )
        elif not active:
            findings.append(
                {
                    "kind": "no_active_endpoint",
                    "severity": "error",
                    "peer_ids": [peer.node_id for peer in matches],
                }
            )
        safe_to_route = not any(item["severity"] == "error" for item in findings) and (
            active_routes is not None
            or (
                len(active) == 1
                and bool(configured_endpoints)
                and not configured_endpoints.isdisjoint(active_ips)
            )
        )
        reports.append(
            {
                "node": node.get("name"),
                "policy": policy,
                "safe_to_route": safe_to_route,
                "configured_endpoints": sorted(configured_endpoints),
                "active_peer_id": (
                    active[0].node_id if len(active) == 1 and safe_to_route else None
                ),
                "active_routes": active_routes or [],
                "retirement_candidates": [peer.node_id for peer in stale] if active else [],
                "peers": [peer.as_dict() for peer in matches],
                "findings": findings,
                "severity": (
                    "error"
                    if any(item["severity"] == "error" for item in findings)
                    else ("warn" if any(item["severity"] == "warn" for item in findings) else "ok")
                ),
            }
        )
    reports.sort(key=lambda item: str(item["node"]))
    return {
        "schema": "skfleet.endpoint-audit/v1",
        "read_only": True,
        "peer_count": len(peers),
        "reports": reports,
        "summary": {
            "nodes": len(reports),
            "duplicates": sum(
                any(item["kind"] == "duplicate_tailscale_identity" for item in report["findings"])
                for report in reports
            ),
            "unsafe": sum(not report["safe_to_route"] for report in reports),
            "retirement_candidates": sum(
                len(report["retirement_candidates"]) for report in reports
            ),
        },
    }
