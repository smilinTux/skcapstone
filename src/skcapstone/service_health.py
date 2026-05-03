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

# Hostname tag for multi-machine dedup — prevents Syncthing conflicts when
# multiple daemons write to the same ITIL incident files.
_HOSTNAME = socket.gethostname()



# ---------------------------------------------------------------------------
# Per-agent YAML config fallback
# ---------------------------------------------------------------------------

def _load_agent_yaml(config_name: str, agent: str | None = None) -> dict:
    """Load ~/.skcapstone/agents/<agent>/config/<config_name>.yaml.

    Falls back gracefully when the file or yaml lib is unavailable. Used by
    check_all_services() so the laptop's jarvis daemon can read the same
    correctly-populated skvector.yaml / skgraph.yaml that skmemory uses,
    instead of probing localhost defaults that don't exist here.
    """
    if not agent:
        agent = (
            os.environ.get("SKAGENT")
            or os.environ.get("SKCAPSTONE_AGENT")
            or os.environ.get("SKMEMORY_AGENT")
            or "lumina"
        )
    path = os.path.expanduser(f"~/.skcapstone/agents/{agent}/config/{config_name}.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml  # type: ignore
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Failed to load %s: %s", path, exc)
        return {}



def _load_syncthing_config() -> tuple[str | None, str | None]:
    """Read ~/.config/syncthing/config.xml to get GUI URL + API key.

    Returns (url, api_key) tuple — either may be None if the config can't
    be parsed. Uses regex (no XML lib dep) since we only need 2 small fields.
    """
    candidates = [
        Path.home() / ".config" / "syncthing" / "config.xml",
        Path.home() / ".local" / "state" / "syncthing" / "config.xml",
    ]
    cfg_path = next((p for p in candidates if p.exists()), None)
    if cfg_path is None:
        return None, None
    try:
        text = cfg_path.read_text()
    except Exception:
        return None, None

    # Find <gui ...> ... <address>HOST:PORT</address> ... </gui>
    gui_match = re.search(
        r"<gui[^>]*>(.*?)</gui>", text, re.S | re.I
    )
    addr_in_gui = None
    if gui_match:
        body = gui_match.group(1)
        addr_match = re.search(r"<address>\s*([^<]+?)\s*</address>", body, re.I)
        if addr_match:
            addr_in_gui = addr_match.group(1).strip()

    api_match = re.search(r"<apikey>\s*([^<]+?)\s*</apikey>", text, re.I)
    api_key = api_match.group(1).strip() if api_match else None

    if not addr_in_gui:
        return None, api_key
    # GUI tls flag
    tls = bool(gui_match and ("tls=\"true\"" in gui_match.group(0) or "tls='true'" in gui_match.group(0)))
    proto = "https" if tls else "http"
    return f"{proto}://{addr_in_gui}", api_key


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

    Environment variables override default URLs (set any to "disabled" to skip):
        SKMEMORY_SKVECTOR_URL     — Qdrant REST base (default: read from
                                    ~/.skcapstone/agents/<agent>/config/skvector.yaml,
                                    else http://localhost:6333)
        SKMEMORY_SKVECTOR_API_KEY — Qdrant API key (default: from skvector.yaml)
        SKMEMORY_SKGRAPH_HOST     — FalkorDB host   (default: read from
                                    ~/.skcapstone/agents/<agent>/config/skgraph.yaml,
                                    else localhost)
        SKMEMORY_SKGRAPH_PORT     — FalkorDB port   (default: from skgraph.yaml,
                                    else 6379)
        SYNCTHING_API_URL         — Syncthing REST   (default: discovered from
                                    ~/.config/syncthing/config.xml gui address,
                                    else http://localhost:8384)
        SYNCTHING_API_KEY         — Syncthing API key (default: from config.xml)
        SKCAPSTONE_DAEMON_URL     — Daemon HTTP base (default http://localhost:9383)
        SKCHAT_DAEMON_URL         — SKChat daemon    (default http://localhost:9385)

    Returns:
        List of dicts, each containing: name, url, status ("up"|"down"|"unknown"),
        latency_ms, version, error.
    """
    results: list[dict[str, Any]] = []

    # -- SKVector (Qdrant) --------------------------------------------------
    qdrant_base = os.environ.get("SKMEMORY_SKVECTOR_URL", "")
    qdrant_api_key = os.environ.get("SKMEMORY_SKVECTOR_API_KEY", "")
    # Fall back to per-agent skvector.yaml when env vars are absent
    if not qdrant_base or not qdrant_api_key:
        cfg = _load_agent_yaml("skvector")
        if cfg.get("enabled", True):
            if not qdrant_base:
                # Reconstruct URL from host/port/https
                host = cfg.get("host", "localhost")
                port = cfg.get("port", 6333)
                proto = "https" if cfg.get("https") or int(port) == 443 else "http"
                if int(port) in (80, 443):
                    qdrant_base = f"{proto}://{host}"
                else:
                    qdrant_base = f"{proto}://{host}:{port}"
            if not qdrant_api_key and cfg.get("api_key"):
                qdrant_api_key = cfg["api_key"]
    if not qdrant_base:
        qdrant_base = "http://localhost:6333"
    if qdrant_base.lower() != "disabled":
        qdrant_url = qdrant_base.rstrip("/") + "/healthz"
        qdrant_headers: dict[str, str] = {}
        if qdrant_api_key:
            qdrant_headers["api-key"] = qdrant_api_key
        results.append(_http_check("skvector (Qdrant)", qdrant_url, headers=qdrant_headers))

    # -- SKGraph (FalkorDB) — TCP check on Redis protocol port ---------------
    graph_host = os.environ.get("SKMEMORY_SKGRAPH_HOST", "")
    graph_port_str = os.environ.get("SKMEMORY_SKGRAPH_PORT", "")
    # Fall back to per-agent skgraph.yaml when env vars are absent
    if not graph_host or not graph_port_str:
        cfg = _load_agent_yaml("skgraph")
        if cfg.get("enabled", True):
            if not graph_host and cfg.get("host"):
                graph_host = str(cfg["host"])
            if not graph_port_str and cfg.get("port"):
                graph_port_str = str(cfg["port"])
    if not graph_host:
        graph_host = "localhost"
    if not graph_port_str:
        graph_port_str = "6379"
    if graph_host.lower() != "disabled":
        graph_port = int(graph_port_str)
        results.append(_tcp_check("skgraph (FalkorDB)", graph_host, graph_port))

    # -- Syncthing -----------------------------------------------------------
    syncthing_base = os.environ.get("SYNCTHING_API_URL", "")
    api_key = os.environ.get("SYNCTHING_API_KEY", "")
    # Fall back to ~/.config/syncthing/config.xml discovery
    if not syncthing_base or not api_key:
        discovered_url, discovered_key = _load_syncthing_config()
        if not syncthing_base and discovered_url:
            syncthing_base = discovered_url
        if not api_key and discovered_key:
            api_key = discovered_key
    if not syncthing_base:
        syncthing_base = "http://localhost:8384"
    if syncthing_base.lower() != "disabled":
        syncthing_url = syncthing_base.rstrip("/") + "/rest/system/status"
        syncthing_headers: dict[str, str] = {}
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
    if chat_base.lower() != "disabled":
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
        error_info = service_result.get("error") or "unreachable"
        mgr = ITILManager(os.path.expanduser(SHARED_ROOT))

        # Dedup: skip if there's already an open incident for this service
        existing = mgr.find_open_incident_for_service(svc_name)
        if existing:
            # Only add a "still down" note if this host hasn't noted it recently
            last_notes = [e.get("note", "") for e in (existing.timeline or [])[-3:]]
            host_tag = f"[{_HOSTNAME}]"
            if any(host_tag in n and "still down" in n for n in last_notes):
                logger.debug("Skipping duplicate down note for %s from %s", svc_name, _HOSTNAME)
            else:
                mgr.update_incident(
                    existing.id, "service_health",
                    note=f"[{_HOSTNAME}] Service {svc_name} still down: {error_info}",
                )
            return
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
            # Skip if this host already noted recovery recently
            last_notes = [e.get("note", "") for e in (existing.timeline or [])[-3:]]
            host_tag = f"[{_HOSTNAME}]"
            if not any(host_tag in n and "back up" in n for n in last_notes):
                mgr.update_incident(
                    existing.id, "service_health",
                    note=f"[{_HOSTNAME}] Service {svc_name} appears to be back up",
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
