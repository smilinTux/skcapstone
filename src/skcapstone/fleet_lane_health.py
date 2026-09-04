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
    """Classify one capacity domain from this cycle's health and queue rows.

    OBSERVATION AGE IS NOT AN ADMISSION CONDITION, and the reason is a deadlock
    measured on the live fleet on 2026-09-04.

    SKGateway derives a backend health row purely from proxied request outcomes:
    `Backend.recordOutcome()` is the only writer of `lastCheck`, and the gateway
    runs no active backend health checker. So `lastCheck` is not "when the
    gateway last looked at this backend", it is "when this backend last carried
    real traffic". An idle healthy backend therefore has an arbitrarily old
    `lastCheck` while remaining perfectly serviceable.

    The first version of this function required `lastCheck` to be within
    MAX_AGE_SECONDS of the observation time. That reused the SNAPSHOT expiry
    bound as a BACKEND OBSERVATION bound, which are different quantities, and it
    was never part of the documented contract (docs/fleet/lane-admission-health.md
    listed three conditions: observed up or degraded, not quarantined, positive
    queue capacity). Its effect was that a lane was admissible only during the
    120 seconds after somebody else sent traffic to that exact capacity domain.
    Since fleet dispatch is the only realistic traffic source, the fleet could
    not start itself and could not restart itself after two quiet minutes.

    Measured at 09:52 UTC on 2026-09-04 against the live dispatch gateway
    http://chiap01:18790, up 8 hours: `codex` read `status=up observed=true` with
    `lastCheck` 17801 seconds old, and every one of the four configured lanes
    resolved to `(False, "unknown")`. Zero lanes admissible on a gateway that was
    working. That is not fail-closed on ambiguous evidence, it is a refusal that
    prevents the traffic that would clear the refusal.

    So the recency bound is gone and the documented conditions are all that
    remain. This does not lower the evidence bar. `observed=true` with a non-down
    status still means a request actually completed on that backend, which is
    positive evidence, not absent or ambiguous evidence. Snapshot freshness, the
    thing MAX_AGE_SECONDS is actually for, is still enforced in `lane_health()`
    and by the `/queue` timestamp check in `acquire_lane_snapshot()`.

    What is still refused, because it is genuinely no evidence:

    - `observed` false, the fresh-start state after a gateway restart. Nothing
      has served, so nothing is known. Admission stays closed until one request
      succeeds on that domain. See the bootstrap section of
      docs/fleet/lane-admission-health.md. That refusal is self-clearing after
      one success, permanently, rather than every 120 seconds forever.
    - A missing, non-numeric, or non-positive `lastCheck`, which contradicts an
      `observed` claim and is malformed evidence.
    - A `lastCheck` more than MAX_AGE_SECONDS in the FUTURE. Recency is not
      required, but a timestamp the serving host cannot yet have produced is
      ambiguous, so it fails closed.

    Admitting a backend whose host died since its last success costs exactly one
    failed dispatch, after which the error-rate and quarantine machinery marks it
    down and the next cycle refuses it correctly. That error self-corrects in one
    cycle. The recency bound did not self-correct at all.
    """
    health_row = health.get(domain) if isinstance(health, dict) else None
    queue_row = queue.get(domain) if isinstance(queue, dict) else None
    if not isinstance(health_row, dict) or not isinstance(queue_row, dict):
        return {"capacity_domain": domain, "state": "unknown"}
    if queue_row.get("capacityDomain") != domain or type(queue_row.get("max")) is not int:
        return {"capacity_domain": domain, "state": "mismatch"}
    last_check = health_row.get("lastCheck")
    if health_row.get("quarantined") is True:
        state = "quarantined"
    elif health_row.get("quarantined") is not False:
        state = "unknown"
    elif (
        not isinstance(last_check, (int, float))
        or isinstance(last_check, bool)
        or last_check <= 0
        or last_check / 1000 - observed_at > MAX_AGE_SECONDS
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
        domains = capacity_domains.get(str(lane["name"]), ())
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
