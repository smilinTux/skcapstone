"""Bounded same-cycle SKGateway health evidence for fleet admission."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

MAX_ENDPOINT_BYTES = 65_536
MAX_SNAPSHOT_BYTES = 65_536
MAX_AGE_SECONDS = 120
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def _fetch_json(
    url: str,
    *,
    timeout: float = 5,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    with opener(url, timeout=timeout) as response:
        raw = response.read(MAX_ENDPOINT_BYTES + 1)
    if len(raw) > MAX_ENDPOINT_BYTES:
        raise ValueError("endpoint response exceeds bound")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("endpoint response is not an object")
    return value


_REMOTE_REVISION_PROGRAM = r"""
import os, subprocess, sys
port = sys.argv[1]
matches = []
for name in os.listdir('/proc'):
    if not name.isdigit():
        continue
    try:
        argv = open('/proc/%s/cmdline' % name, 'rb').read().split(b'\0')
    except OSError:
        continue
    if b'--port' not in argv:
        continue
    pos = argv.index(b'--port')
    if pos + 1 >= len(argv) or argv[pos + 1].decode() != port:
        continue
    if not any(value.endswith(b'index.mjs') for value in argv):
        continue
    matches.append(name)
if len(matches) != 1:
    raise SystemExit(2)
cwd = os.path.realpath('/proc/%s/cwd' % matches[0])
result = subprocess.run(
    ['git', '-C', cwd, 'rev-parse', 'HEAD'], text=True, capture_output=True, timeout=3
)
if result.returncode:
    raise SystemExit(3)
dirty = subprocess.run(
    ['git', '-C', cwd, 'status', '--porcelain', '--untracked-files=all', '--', 'src'],
    text=True, capture_output=True, timeout=3
)
if dirty.returncode or dirty.stdout.strip():
    raise SystemExit(4)
print(result.stdout.strip())
"""


def active_gateway_revision(
    base_url: str,
    *,
    timeout: float = 6,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return the Git revision of the unique process serving the endpoint port."""
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("invalid gateway host")
    local = host in {"localhost", "127.0.0.1", os.uname().nodename}
    command = ["python3", "-", str(port)]
    if not local:
        user = os.environ.get("SKFLEET_GATEWAY_SSH_USER", "skuser01")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", user):
            raise ValueError("invalid gateway SSH user")
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=4",
            f"{user}@{host}",
            *command,
        ]
    result = runner(
        command,
        input=_REMOTE_REVISION_PROGRAM,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    revision = result.stdout.strip() if result.returncode == 0 else ""
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("active gateway revision unavailable")
    return revision


def _domain_state(health: Any, queue: Any, domain: str, observed_at: float) -> dict[str, Any]:
    health_row = health.get(domain) if isinstance(health, dict) else None
    queue_row = queue.get(domain) if isinstance(queue, dict) else None
    if not isinstance(health_row, dict) or not isinstance(queue_row, dict):
        return {"capacity_domain": domain, "state": "unknown"}
    if queue_row.get("capacityDomain") != domain or type(queue_row.get("max")) is not int:
        return {"capacity_domain": domain, "state": "mismatch"}
    if health_row.get("quarantined") is True:
        state = "quarantined"
    elif health_row.get("quarantined") is not False:
        state = "unknown"
    elif (
        not isinstance(health_row.get("lastCheck"), (int, float))
        or abs(observed_at - health_row["lastCheck"] / 1000) > MAX_AGE_SECONDS
    ):
        state = "unknown"
    elif health_row.get("observed") is not True or health_row.get("status") in {"down", "unknown"}:
        state = "owner-down"
    elif queue_row["max"] <= 0:
        state = "owner-down"
    elif health_row.get("status") in {"up", "degraded"}:
        state = "healthy"
    else:
        state = "unknown"
    return {"capacity_domain": domain, "state": state, "max": queue_row["max"]}


def acquire_lane_snapshot(
    base_url: str,
    lanes: list[dict[str, Any]],
    capacity_domains: dict[str, tuple[str, ...]],
    path: Path,
    cycle_id: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    revision_resolver: Callable[[str], str] = active_gateway_revision,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Fetch each endpoint once, validate, and atomically seal this cycle."""
    endpoint = base_url.rstrip("/")
    errors: list[str] = []
    try:
        health_doc = _fetch_json(endpoint + "/health", opener=opener)
        health = health_doc.get("backends")
        if health_doc.get("status") != "ok" or not isinstance(health, dict):
            raise ValueError("health schema")
    except Exception as exc:
        health, errors = {}, [f"health:{type(exc).__name__}"]
    try:
        queue_doc = _fetch_json(endpoint + "/queue", opener=opener)
        queue = queue_doc.get("backends")
        if not isinstance(queue_doc.get("pool"), dict) or not isinstance(queue, dict):
            raise ValueError("queue schema")
        timestamp = queue_doc.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError("queue timestamp")
        parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("queue timestamp timezone")
        queue_observed_at = parsed.timestamp()
    except Exception as exc:
        queue, errors = {}, [*errors, f"queue:{type(exc).__name__}"]
    try:
        revision = revision_resolver(endpoint)
    except Exception as exc:
        revision, errors = "", [*errors, f"revision:{type(exc).__name__}"]
    observed_at = now()
    if queue and abs(observed_at - queue_observed_at) > MAX_AGE_SECONDS:
        queue, errors = {}, [*errors, "queue:stale"]
    entries = []
    for lane in lanes:
        domains = lane.get("capacity_domains", capacity_domains.get(str(lane["name"]), ()))
        states = [_domain_state(health, queue, domain, observed_at) for domain in domains]
        entries.append(
            {
                "lane": lane["name"],
                "model": lane["model"],
                "endpoint": endpoint,
                "capacity_domains": list(domains),
                "domains": states,
            }
        )
    snapshot = {
        "schema_version": 2,
        "cycle_id": cycle_id,
        "observed_at": observed_at,
        "endpoint": endpoint,
        "runtime_revision": revision,
        "errors": errors,
        "lanes": entries,
    }
    encoded = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot exceeds bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return snapshot


def lane_health(
    snapshot: dict[str, Any],
    lane: str,
    model: str,
    *,
    cycle_id: str,
    endpoint: str,
    capacity_domains: tuple[str, ...],
    active_revision: str,
    now: float | None = None,
) -> tuple[bool, str]:
    """Admit only an exact healthy binding from the current selector cycle."""
    current = time.time() if now is None else now
    if snapshot.get("schema_version") != 2 or snapshot.get("cycle_id") != cycle_id:
        return False, "cycle-mismatch"
    try:
        age = current - float(snapshot["observed_at"])
    except (KeyError, TypeError, ValueError):
        return False, "unknown"
    if age < 0 or age > MAX_AGE_SECONDS:
        return False, "stale"
    if snapshot.get("endpoint") != endpoint.rstrip("/"):
        return False, "endpoint-mismatch"
    if (
        not REVISION_RE.fullmatch(active_revision)
        or snapshot.get("runtime_revision") != active_revision
    ):
        return False, "revision-mismatch"
    matches = [
        row
        for row in snapshot.get("lanes", [])
        if isinstance(row, dict) and row.get("lane") == lane
    ]
    exact = [row for row in matches if row.get("model") == model]
    if len(exact) != 1:
        return False, "model-mismatch" if not exact and matches else "unknown"
    row = exact[0]
    if row.get("capacity_domains") != list(capacity_domains):
        return False, "capacity-mismatch"
    states = row.get("domains")
    if not isinstance(states, list) or len(states) != len(capacity_domains):
        return False, "unknown"
    if any(item.get("state") == "healthy" for item in states if isinstance(item, dict)):
        return True, "healthy"
    values = {item.get("state") for item in states if isinstance(item, dict)}
    if "quarantined" in values:
        return False, "model_claim_quarantined"
    if "owner-down" in values:
        return False, "model_owner_backend_down"
    return False, "unknown"


def cycle_id(host: str, stamp: str) -> str:
    material = f"{host}\0{stamp}\0{os.getpid()}\0{time.time_ns()}".encode()
    return hashlib.sha256(material).hexdigest()[:32]
