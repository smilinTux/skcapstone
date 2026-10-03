"""Opt-in HTTP application readiness for the existing service-health registry."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

MAX_BODY_BYTES = 65536


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not turn a redirected login page into a readiness success."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Decline redirects so urllib reports the original HTTP code."""
        return None


def check_http_readiness(
    name: str,
    url: str,
    *,
    expected_json: dict[str, Any] | None = None,
    user_agent: str | None = None,
    timeout: float = 3,
) -> dict[str, Any]:
    """Require observed 2xx and optional exact top-level JSON scalar assertions.

    Args:
        name: Service label.
        url: HTTP(S) readiness endpoint without embedded credentials.
        expected_json: Required top-level scalar fields, with exact JSON types.
        user_agent: Explicit client identifier; defaults to SKCapstone readiness.
        timeout: Network timeout in seconds.

    Returns:
        Existing service-health shape plus HTTP status and failure class.
        Policy/auth denial is unknown; observed bad readiness is down.
    """
    result = dict(
        name=name,
        url=url,
        status="unknown",
        latency_ms=0,
        version=None,
        error=None,
        error_class=None,
        http_status=None,
    )
    started = time.monotonic()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("readiness requires an HTTP(S) URL without credentials")
        if expected_json is not None and (
            not isinstance(expected_json, dict)
            or any(
                not isinstance(k, str) or type(v) not in {str, int, float, bool, type(None)}
                for k, v in expected_json.items()
            )
        ):
            raise ValueError("expected_json must contain only top-level scalar assertions")
        agent = user_agent if user_agent is not None else "SKCapstone-Readiness/1.0"
        if not isinstance(agent, str) or not agent or "\r" in agent or "\n" in agent:
            raise ValueError("user_agent must be a nonempty single-line string")
    except (TypeError, ValueError) as exc:
        result.update(error=str(exc), error_class="invalid_config")
        return result
    try:
        request = urllib.request.Request(url, headers={"User-Agent": agent})
        # Default HTTPSHandler retains certificate and hostname validation.
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            result["http_status"] = response.status
            if not 200 <= response.status < 300:
                result.update(
                    error=f"HTTP {response.status}", error_class="http_policy_or_endpoint"
                )
            elif expected_json is not None:
                body = response.read(MAX_BODY_BYTES + 1)
                try:
                    if len(body) > MAX_BODY_BYTES:
                        raise ValueError("readiness JSON exceeds size limit")
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ValueError("readiness JSON is not an object")
                    for key, expected in expected_json.items():
                        if (
                            key not in data
                            or type(data[key]) is not type(expected)
                            or data[key] != expected
                        ):
                            raise ValueError(f"readiness assertion failed: {key}")
                except (ValueError, UnicodeError) as exc:
                    result.update(status="down", error=str(exc), error_class="not_ready")
                else:
                    result["status"] = "up"
            else:
                result["status"] = "up"
    except urllib.error.HTTPError as exc:
        result.update(
            http_status=exc.code,
            error=f"HTTP {exc.code}",
            status="down" if exc.code >= 500 else "unknown",
            error_class="not_ready" if exc.code >= 500 else "http_policy_or_endpoint",
        )
        exc.close()
    except Exception as exc:
        result.update(status="down", error=str(exc)[:200], error_class="transport")
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return result
