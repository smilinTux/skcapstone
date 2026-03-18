"""Service URL health check mechanism.

Pings all known services in the sovereign stack and returns structured
status reports.  Each check uses urllib.request with a 3-second timeout
so the full sweep completes in bounded time even when services are down.

Usage (library):
    from skcapstone.service_health import check_all_services
    results = check_all_services()
    for svc in results:
        print(f"{svc['name']}: {svc['status']} ({svc['latency_ms']}ms)")

Usage (scheduled task):
    from skcapstone.service_health import make_service_health_task
    callback = make_service_health_task()
    scheduler.register("service_health_check", 300, callback)
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("skcapstone.service_health")

# Default timeout per service check (seconds).
CHECK_TIMEOUT = 3


# ---------------------------------------------------------------------------
# Individual service checks
# ---------------------------------------------------------------------------


def _http_check(
    name: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    version_key: str | None = None,
) -> dict[str, Any]:
    """Perform an HTTP GET health check against *url*.

    Args:
        name: Human-readable service name.
        url: Full URL to probe (e.g. ``http://localhost:6333/healthz``).
        headers: Optional extra HTTP headers (e.g. API keys).
        version_key: If set, extract this key from the JSON response as version.

    Returns:
        Status dict with name, url, status, latency_ms, version, error.
    """
    result: dict[str, Any] = {
        "name": name,
        "url": url,
        "status": "unknown",
        "latency_ms": 0,
        "version": None,
        "error": None,
    }
    req = urllib.request.Request(url, headers=headers or {})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            latency = (time.monotonic() - t0) * 1000
            result["latency_ms"] = round(latency, 1)
            result["status"] = "up"

            if version_key:
                try:
                    body = json.loads(resp.read().decode("utf-8"))
                    result["version"] = body.get(version_key)
                except Exception as exc:
                    logger.warning("Failed to parse version from service health response: %s", exc)
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - t0) * 1000
        result["latency_ms"] = round(latency, 1)
        # A non-2xx response still means the service is reachable.
        if exc.code < 500:
            result["status"] = "up"
        else:
            result["status"] = "down"
            result["error"] = f"HTTP {exc.code}"
    except Exception as exc:
        latency = (time.monotonic() - t0) * 1000
        result["latency_ms"] = round(latency, 1)
        result["status"] = "down"
        result["error"] = str(exc)[:200]
    return result


def _tcp_check(name: str, host: str, port: int) -> dict[str, Any]:
    """Perform a raw TCP connect check.

    Args:
        name: Human-readable service name.
        host: Hostname or IP to connect to.
        port: TCP port number.

    Returns:
        Status dict with name, url, status, latency_ms, version, error.
    """
    url = f"tcp://{host}:{port}"
    result: dict[str, Any] = {
        "name": name,
        "url": url,
        "status": "unknown",
        "latency_ms": 0,
        "version": None,
        "error": None,
    }
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=CHECK_TIMEOUT)
        latency = (time.monotonic() - t0) * 1000
        sock.close()
        result["latency_ms"] = round(latency, 1)
        result["status"] = "up"
    except Exception as exc:
        latency = (time.monotonic() - t0) * 1000
        result["latency_ms"] = round(latency, 1)
        result["status"] = "down"
        result["error"] = str(exc)[:200]
    return result


# ---------------------------------------------------------------------------
# Aggregate check
# ---------------------------------------------------------------------------


def check_all_services() -> list[dict[str, Any]]:
    """Ping every known service and return a list of status dicts.

    Environment variables override default URLs:
        SKMEMORY_SKVECTOR_URL   — Qdrant REST base (default http://localhost:6333)
        SKMEMORY_SKGRAPH_HOST   — FalkorDB host   (default localhost)
        SKMEMORY_SKGRAPH_PORT   — FalkorDB port   (default 6379)
        SYNCTHING_API_URL       — Syncthing REST   (default http://localhost:8384)
        SYNCTHING_API_KEY       — Syncthing API key (optional)
        SKCAPSTONE_DAEMON_URL   — Daemon HTTP base (default http://localhost:9383)
        SKCHAT_DAEMON_URL       — SKChat daemon    (default http://localhost:9385)

    Returns:
        List of dicts, each containing: name, url, status ("up"|"down"|"unknown"),
        latency_ms, version, error.
    """
    results: list[dict[str, Any]] = []

    # -- SKVector (Qdrant) --------------------------------------------------
    qdrant_base = os.environ.get("SKMEMORY_SKVECTOR_URL", "http://localhost:6333")
    qdrant_url = qdrant_base.rstrip("/") + "/healthz"
    results.append(_http_check("skvector (Qdrant)", qdrant_url))

    # -- SKGraph (FalkorDB) — TCP check on Redis protocol port ---------------
    graph_host = os.environ.get("SKMEMORY_SKGRAPH_HOST", "localhost")
    graph_port = int(os.environ.get("SKMEMORY_SKGRAPH_PORT", "6379"))
    results.append(_tcp_check("skgraph (FalkorDB)", graph_host, graph_port))

    # -- Syncthing -----------------------------------------------------------
    syncthing_base = os.environ.get("SYNCTHING_API_URL", "http://localhost:8384")
    syncthing_url = syncthing_base.rstrip("/") + "/rest/system/status"
    syncthing_headers: dict[str, str] = {}
    api_key = os.environ.get("SYNCTHING_API_KEY", "")
    if api_key:
        syncthing_headers["X-API-Key"] = api_key
    results.append(
        _http_check(
            "syncthing",
            syncthing_url,
            headers=syncthing_headers,
            version_key="version",
        )
    )

    # -- skcapstone daemon ---------------------------------------------------
    daemon_base = os.environ.get("SKCAPSTONE_DAEMON_URL", "http://localhost:9383")
    daemon_url = daemon_base.rstrip("/") + "/health"
    results.append(_http_check("skcapstone daemon", daemon_url))

    # -- skchat daemon -------------------------------------------------------
    chat_base = os.environ.get("SKCHAT_DAEMON_URL", "http://localhost:9385")
    chat_url = chat_base.rstrip("/") + "/health"
    results.append(_http_check("skchat daemon", chat_url))

    return results


# ---------------------------------------------------------------------------
# Scheduled-task factory
# ---------------------------------------------------------------------------


def _create_incident_for_down_service(service_result: dict[str, Any]) -> None:
    """Auto-create an ITIL incident for a down service (with dedup).

    Only creates a new incident if there is no existing open incident
    for the same service. Uses best-effort: failures are logged but
    never block the health check.
    """
    try:
        from . import SHARED_ROOT
        from .itil import ITILManager

        svc_name = service_result["name"]
        mgr = ITILManager(os.path.expanduser(SHARED_ROOT))

        # Dedup: skip if there's already an open incident for this service
        existing = mgr.find_open_incident_for_service(svc_name)
        if existing:
            logger.debug(
                "Skipping incident creation for %s — open incident %s exists",
                svc_name, existing.id,
            )
            return

        error_info = service_result.get("error") or "unreachable"
        mgr.create_incident(
            title=f"{svc_name} down",
            severity="sev3",
            source="service_health",
            affected_services=[svc_name],
            impact=f"Service unreachable: {error_info}",
            managed_by="lumina",
            created_by="service_health",
            tags=["auto-detected", "service-health"],
        )
        logger.info("Auto-created incident for down service: %s", svc_name)
    except Exception as exc:
        logger.debug("Failed to create incident for %s: %s", service_result.get("name"), exc)


def _auto_resolve_recovered_service(service_result: dict[str, Any]) -> None:
    """Auto-resolve sev4 incidents when a service recovers."""
    try:
        from . import SHARED_ROOT
        from .itil import ITILManager

        svc_name = service_result["name"]
        mgr = ITILManager(os.path.expanduser(SHARED_ROOT))
        existing = mgr.find_open_incident_for_service(svc_name)
        if existing is None:
            return

        if existing.severity.value == "sev4":
            mgr.update_incident(
                existing.id, "service_health",
                new_status="resolved",
                note=f"Service {svc_name} recovered automatically",
                resolution_summary="Auto-resolved: service came back up",
            )
            logger.info("Auto-resolved sev4 incident %s for recovered service %s",
                        existing.id, svc_name)
        else:
            mgr.update_incident(
                existing.id, "service_health",
                note=f"Service {svc_name} appears to be back up",
            )
    except Exception as exc:
        logger.debug("Failed to auto-resolve incident for %s: %s",
                      service_result.get("name"), exc)


def make_service_health_task() -> callable:
    """Return a zero-arg callback suitable for TaskScheduler.register().

    Runs check_all_services() and logs results.  Down services are logged
    at WARNING level; all-up is logged at DEBUG level.  Auto-creates ITIL
    incidents for down services and auto-resolves sev4 incidents for
    recovered services.
    """

    def _run() -> None:
        results = check_all_services()
        down = [r for r in results if r["status"] == "down"]
        up = [r for r in results if r["status"] == "up"]

        if down:
            names = ", ".join(r["name"] for r in down)
            logger.warning(
                "Service health: %d/%d down — %s", len(down), len(results), names
            )
            for r in down:
                logger.warning(
                    "  %s (%s): %s", r["name"], r["url"], r["error"] or "unreachable"
                )
                _create_incident_for_down_service(r)
        else:
            up_count = len(up)
            logger.debug(
                "Service health: %d/%d up, %d unknown",
                up_count,
                len(results),
                len(results) - up_count,
            )

        # Check for recovered services
        for r in up:
            _auto_resolve_recovered_service(r)

    return _run
