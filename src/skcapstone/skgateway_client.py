"""Minimal skgateway (OpenAI-compatible) client for dashboard inference.

Points at the sovereign gateway (``http://localhost:18780/v1``, model
``sk-default`` -> auto-router). Stdlib-only (urllib), robust: returns None on
any failure so callers can fall back. Reused by the AI-suggestions feature and
the Phase 5 assistant console.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("skcapstone.skgateway")

DEFAULT_BASE = os.environ.get("SKGATEWAY_URL", "http://localhost:18780/v1")
DEFAULT_MODEL = os.environ.get("SKGATEWAY_MODEL", "sk-default")


def chat(messages: list[dict], model: str = DEFAULT_MODEL, max_tokens: int = 2048,
         temperature: float = 0.3, timeout: float = 25.0,
         base_url: str = DEFAULT_BASE) -> str | None:
    """Call the gateway's chat-completions endpoint. Returns text or None.

    ``max_tokens`` defaults high because the auto-routed model may think before
    answering (callers need headroom).
    """
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        logger.info("skgateway chat failed (%s); caller will fall back", exc)
        return None


def available(timeout: float = 2.0, base_url: str = DEFAULT_BASE) -> bool:
    """Cheap reachability probe for the gateway."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False
