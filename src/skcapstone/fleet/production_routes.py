"""Qualify native builder routes from fresh gateway capability and capacity."""

import json
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..fleet_route_preflight import resolve_and_preflight
from .pi_catalog import materialize_gateway_catalog
from .production_dispatch import resolve_production_routes
from .review_capacity import acquire_review_route_snapshot

_SNAPSHOTS: dict = {}
_PREFLIGHTS: dict = {}
_CATALOGS: dict = {}
_BOOTSTRAP_PROBES: dict = {}
FRESH_SECONDS = 30


class RouteUnavailableError(ValueError):
    """A currently unqualified route can be retried without claiming source."""


def snapshot(value: dict) -> dict:
    """Reuse a bounded current gateway observation, never a worker-count ceiling."""
    gateway = value["gateway_url"]
    prior = _SNAPSHOTS.get(gateway)
    now = time.time()
    if prior is not None and 0 <= now - prior.get("observed_at", 0) <= FRESH_SECONDS:
        return prior
    with tempfile.TemporaryDirectory(prefix="skfleet-routes-") as directory:
        result = acquire_review_route_snapshot(gateway, Path(directory) / "routes.json", str(now))
    _SNAPSHOTS[gateway] = result
    return result


def candidates(value: dict, route: str, labels: list[str], *, observed=None) -> list[dict]:
    """Require actual tools, reasoning, size, policy tier and exact family binding."""
    observed = snapshot(value) if observed is None else observed
    if (
        observed.get("error") is not None
        or not 0 <= time.time() - observed.get("observed_at", 0) <= FRESH_SECONDS
    ):
        return []
    required_size = route.removeprefix("sk-").upper()
    raw_routes = observed.get("routes", [])
    routes = resolve_production_routes(
        raw_routes, policy=value, required_size=required_size, labels=labels
    )
    if not routes:
        # Unknown health is not admission. Use the normal resolver with only
        # unknown health values replaced to find one exact, policy-compatible
        # probe target. The target is never returned: admission uses only the
        # fresh snapshot taken after the bounded synthetic request succeeds.
        unknown_models = {
            row.get("model_or_bucket")
            for row in raw_routes
            if isinstance(row, dict) and row.get("state") == "unknown"
        }
        probe_routes = resolve_production_routes(
            [
                {**row, "state": "healthy"}
                if isinstance(row, dict) and row.get("state") == "unknown"
                else row
                for row in raw_routes
            ],
            policy=value,
            required_size=required_size,
            labels=labels,
        )
        targets = [row for row in probe_routes if row["model_or_bucket"] in unknown_models]
        targets = [
            row
            for row in targets
            if sum(
                candidate["model_or_bucket"] == row["model_or_bucket"]
                for candidate in probe_routes
            )
            == 1
        ]
        if targets:
            target = targets[0]
            key = (value["gateway_url"], target["model_or_bucket"], target["provider"])
            now = time.time()
            prior = _BOOTSTRAP_PROBES.get(key)
            if prior is None or not 0 <= now - prior < FRESH_SECONDS:
                _BOOTSTRAP_PROBES[key] = now
                try:
                    probe = resolve_and_preflight(value["gateway_url"], target["model_or_bucket"])
                    if (
                        probe.requested_identity != target["model_or_bucket"]
                        or probe.provider != target["provider"]
                    ):
                        return []
                except (OSError, RuntimeError, ValueError):
                    return []
                _PREFLIGHTS[key] = {**probe.to_dict(), "observed_at": time.time()}
                _SNAPSHOTS.pop(value["gateway_url"], None)
                observed = snapshot(value)
                if (
                    observed.get("error") is not None
                    or not 0 <= time.time() - observed.get("observed_at", 0) <= FRESH_SECONDS
                ):
                    return []
                routes = resolve_production_routes(
                    observed.get("routes", []),
                    policy=value,
                    required_size=required_size,
                    labels=labels,
                )
    result = []
    for found in routes:
        if sum(row["model_or_bucket"] == found["model_or_bucket"] for row in routes) != 1:
            continue
        result.append(
            {
                "family": found["lane"],
                "provider_family": found["family"],
                "provider": "skgateway",
                "model": found["model_or_bucket"],
                "gateway_backend": found["provider"],
                "capacity_domain": found["capacity_domain"],
                "size_class": found["size_class"],
                "policy_tier": found["policy_tier"],
            }
        )
    return result


def catalog_preflight(value: dict, binding: dict) -> None:
    """Read only needed local Pi metadata; never return credential-bearing objects."""
    try:
        with (Path.home() / ".pi/agent/models.json").open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("catalog bound")
        provider = json.loads(raw)["providers"]["skgateway"]
        rows = [row for row in provider["models"] if row.get("id") == binding["model"]]
        actual = (
            urlsplit(rows[0].get("baseUrl", provider["baseUrl"]))
            if len(rows) == 1
            else urlsplit("")
        )
        expected = urlsplit(value["gateway_url"])
        if (
            len(rows) != 1
            or actual.username is not None
            or actual.password is not None
            or (actual.scheme, actual.hostname, actual.port)
            != (expected.scheme, expected.hostname, expected.port)
            or actual.path.rstrip("/") != "/v1"
        ):
            raise ValueError("exact catalog binding mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("production Pi exact model catalog binding unavailable") from exc


def preflight(value: dict, binding: dict) -> dict:
    """Probe exact gateway route once per short window before native claim mutation."""
    observed = snapshot(value)
    qualified = candidates(value, "sk-" + binding["size_class"].lower(), [], observed=observed)
    if not any(all(binding.get(key) == item for key, item in row.items()) for row in qualified):
        raise ValueError("production selected route no longer matches current gateway truth")
    key = (str(Path.home()), value["gateway_url"])
    revision = (observed.get("capacity_revision"), observed.get("observed_at"))
    if _CATALOGS.get(key) != revision:
        materialize_gateway_catalog(Path.home(), value, observed)
        _CATALOGS[key] = revision
    catalog_preflight(value, binding)
    key = (value["gateway_url"], binding["model"], binding["gateway_backend"])
    prior = _PREFLIGHTS.get(key)
    now = time.time()
    if prior is not None and 0 <= now - prior["observed_at"] <= FRESH_SECONDS:
        if prior.get("error"):
            raise ValueError("production exact route preflight remains unavailable")
        return prior
    try:
        result = resolve_and_preflight(value["gateway_url"], binding["model"])
        if (
            result.requested_identity != binding["model"]
            or result.provider != binding["gateway_backend"]
        ):
            raise ValueError("production preflight exact model/backend binding differs")
    except ValueError:
        _PREFLIGHTS[key] = {"error": True, "observed_at": time.time()}
        raise
    receipt = {**result.to_dict(), "observed_at": time.time()}
    _PREFLIGHTS[key] = receipt
    return receipt
