"""Exact native Pi transport failure evidence for an operator continuation."""

import json
import re
from datetime import datetime
from pathlib import Path

from . import builder_retire as custody
from . import builder_terminal as terminal
from . import source_bundle

MAX_SESSION = 32 * 1024 * 1024


def token(session, digest):
    """Accept only a Pi session basename and its exact immutable digest."""
    if (
        not isinstance(session, str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-f-]{36}\.jsonl", session
        )
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("exact transport session and digest required")
    return {"session": session, "sha256": digest}


def timestamp(value):
    """Parse an aware native/session timestamp without accepting naive dates."""
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("aware transport timestamp required")
    return value.timestamp()


def proof(paths, request, status, evidence):
    """Require exact death and a hash-pinned terminal gateway413 in that session."""
    if request["production"].get("family") != "glm":
        raise ValueError("GLM transport generation required")
    if not isinstance(evidence, dict) or evidence != token(
        evidence.get("session"), evidence.get("sha256")
    ):
        raise ValueError("transport binding changed")
    terminal.prove(paths.root.parent, status)
    workspace = paths.root / "workspaces" / status["owner"]
    directory = (
        Path.home()
        / ".pi/agent/sessions"
        / ("--" + str(workspace).strip("/").replace("/", "-") + "--")
    )
    raw = source_bundle._read(directory / evidence["session"], MAX_SESSION)
    if custody.sha(raw) != evidence["sha256"]:
        raise ValueError("transport session bytes changed")
    try:
        events = [json.loads(line) for line in raw.splitlines()]
        first, last = events[0], events[-1]
        message = last["message"]
        error_text = message["errorMessage"]
        if not error_text.startswith("413: "):
            raise ValueError("terminal gateway413 required")
        error = json.loads(error_text[5:])
        if (
            first["type"] != "session"
            or first["id"] != evidence["session"].split("_", 1)[1][:-6]
            or first["cwd"] != str(workspace)
            or last["type"] != "message"
            or message["role"] != "assistant"
            or message["stopReason"] != "error"
            or message["provider"] != "skgateway"
            or message["model"] != request["production"]["model"]
            or error["code"] != "request_too_large"
            or error["param"] != "body"
            or error["retryable"] is not False
            or type(error["actual_bytes"]) is not int
            or type(error["limit_bytes"]) is not int
            or not error["actual_bytes"] > error["limit_bytes"] > 0
        ):
            raise ValueError("exact terminal gateway transport failure required")
        # The trusted journal has already rejected reuse and ambiguous invocations.
        rows, _ = terminal.history(status, terminal.boot_id())
        started = int(rows[0]["__REALTIME_TIMESTAMP"]) / 1e6
        ended = int(rows[-1]["__REALTIME_TIMESTAMP"]) / 1e6
        session_start, session_end = timestamp(first["timestamp"]), timestamp(last["timestamp"])
        if (
            not started <= session_start <= started + 30
            or not session_start <= session_end <= ended
        ):
            raise ValueError("transport session is outside exact native invocation")
        return {
            "session_sha256": evidence["sha256"],
            "session_id": first["id"],
            "started_at": first["timestamp"],
            "failed_at": last["timestamp"],
            "actual_bytes": error["actual_bytes"],
            "limit_bytes": error["limit_bytes"],
        }
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("transport session evidence unavailable") from exc
