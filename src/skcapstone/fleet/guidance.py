"""Producer-side guidance queue for supervised fleet work."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_TEXT_BYTES = 4096
MAX_PER_HOUR = 3


def _author() -> str:
    value = os.environ.get("SKAGENT", "").strip()
    if not value:
        raise ValueError("guidance author identity is required")
    # Worker identities are scoped to a card and must never enqueue guidance.
    if value.startswith(("pi-", "worker-")) or "worker" in value.lower():
        raise ValueError("fleet worker identities cannot enqueue guidance")
    return value


def _record_evidence(card_id: str, request: dict[str, str]) -> None:
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    subprocess.run(
        ["skcapstone", "coord", "link", card_id, "guidance", payload, "--agent", _author()],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def enqueue_guidance(card_id: str, text: str, *, root: Path | None = None) -> dict[str, str]:
    author = _author()
    if not card_id or any(c not in "0123456789abcdef" for c in card_id.lower()):
        raise ValueError("invalid card id")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("guidance text exceeds 4KB")
    root = root or Path.home() / ".skcapstone" / "fleet" / "guidance"
    path = root / f"{card_id}.ndjson"
    now = time.time()
    recent = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if now - datetime.fromisoformat(item["ts"].replace("Z", "+00:00")).timestamp() < 3600:
                    recent.append(item)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    if len(recent) >= MAX_PER_HOUR:
        raise ValueError("guidance rate limit exceeded: at most 3 requests per card per hour")
    request = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "author": author, "host": socket.gethostname(), "card": card_id, "text": text}
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
    # Record through the coord CLI, never by touching CardStore files.
    try:
        _record_evidence(card_id, request)
    except Exception as exc:
        raise ValueError("could not record guidance evidence through coord") from exc
    root.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
    return request
