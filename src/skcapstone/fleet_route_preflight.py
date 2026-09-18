"""Resolve and probe a logical SKGateway route before fleet state changes."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

MAX_RESPONSE_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class RoutePreflight:
    requested_identity: str
    served_identity: str
    provider: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


PREFLIGHT_USER_AGENT = "skfleet-route-preflight/1.0"
"""Sent on every preflight probe.

The gateway forwards the caller's User-Agent upstream, so the agent string is
not cosmetic: an upstream edge that refuses unknown clients refuses the probe,
and the dispatcher reads that as an unhealthy route rather than a rejected
client. Keep it a conventional token.
"""


def _json_response(response: Any) -> dict[str, Any]:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("gateway response exceeds bound")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("gateway response is not an object")
    return value


def resolve_and_preflight(
    gateway_url: str,
    logical_route: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> RoutePreflight:
    """Require current advertisement, then exercise that exact dispatch route."""
    route = logical_route.strip()
    if not route:
        raise ValueError("logical route is required")
    base = gateway_url.rstrip("/")

    def request_timeout() -> float:
        """Return a per-request timeout bounded by the shared deadline."""
        if deadline is None:
            return timeout
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("gateway preflight deadline exhausted")
        return min(timeout, remaining)

    try:
        with opener(base + "/v1/models", timeout=request_timeout()) as response:
            catalog = _json_response(response)
        rows = catalog.get("data")
        current = (
            [row for row in rows if isinstance(row, dict) and row.get("id") == route]
            if isinstance(rows, list)
            else []
        )
        if (
            len(current) != 1
            or current[0].get("advertised", True) is not True
            or current[0].get("stale") is True
        ):
            raise ValueError("logical route is not currently advertised")
        request = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps(
                {
                    "model": route,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    "max_tokens": 1,
                    # No temperature. The kimi backends REJECT temperature 0, so
                    # sending it fails the probe on exactly the backends most
                    # likely to need probing. The same omission is already in
                    # scripts/gateway/skgw-warm for the same reason.
                    "stream": False,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # The gateway forwards the CALLER's User-Agent upstream, and the
                # kimi upstream's edge rejects python-urllib's default with HTTP
                # 403. Measured on chiap01 2026-09-18: the identical request body
                # returns 403 with the default agent and does not with a
                # conventional one. Every kimi preflight therefore failed, and
                # the dispatcher logged ROUTE_PREFLIGHT_BLOCKED and launched
                # nothing on hosts whose owned card routed to kimi.
                "User-Agent": PREFLIGHT_USER_AGENT,
            },
            method="POST",
        )
        with opener(request, timeout=request_timeout()) as response:
            body = _json_response(response)
            headers = response.headers
        served = str(headers.get("x-sk-model-served") or body.get("model") or "").strip()
        provider = str(headers.get("x-sk-backend") or current[0].get("provider") or "").strip()
        if not served:
            raise ValueError("gateway did not report served identity")
        return RoutePreflight(route, served, provider)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"gateway rejected preflight with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(f"gateway preflight unavailable: {type(exc).__name__}") from None
