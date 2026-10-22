"""Bounded Herdr calls in the caller's explicit local managed session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
from pathlib import Path


class HerdrTransportError(ValueError):
    """The transport cannot establish the required local session evidence."""


def _identifier(value, pattern: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise HerdrTransportError(f"invalid Herdr {field}")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise HerdrTransportError("duplicate Herdr response field")
        result[key] = value
    return result


def _response(result) -> dict:
    if result.returncode != 0:
        raise HerdrTransportError("Herdr command refused or failed")
    if not isinstance(result.stdout, str) or len(result.stdout.encode()) > 131072:
        raise HerdrTransportError("Herdr response exceeds the bounded protocol")
    try:
        payload = json.loads(result.stdout, object_pairs_hook=_unique)
        body = payload["result"]
        if not isinstance(body, dict):
            raise TypeError
        return body
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HerdrTransportError("malformed Herdr response") from exc


def _observation(body: dict) -> dict:
    try:
        agent = body["agent"]
        if not isinstance(agent, dict):
            raise TypeError
        result = {
            "pane_id": _identifier(agent["pane_id"], r"w[A-Za-z0-9]+:p[A-Za-z0-9]+", "pane"),
            "workspace_id": _identifier(agent["workspace_id"], r"w[A-Za-z0-9]+", "workspace"),
            "tab_id": _identifier(agent["tab_id"], r"w[A-Za-z0-9]+:t[A-Za-z0-9]+", "tab"),
            "terminal_id": _identifier(agent["terminal_id"], r"[A-Za-z0-9_-]{1,128}", "terminal"),
            "agent_kind": _identifier(agent["agent"], r"[a-z][a-z0-9_-]{0,31}", "kind"),
            "agent_name": None,
            "agent_session": None,
        }
        if agent.get("name") is not None:
            result["agent_name"] = _identifier(agent["name"], r"[a-z][a-z0-9_-]{0,31}", "name")
        cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if (
            not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
            or any(ord(c) < 32 for c in cwd)
        ):
            raise HerdrTransportError("Herdr cwd must be an absolute path")
        result["cwd"] = os.path.normpath(cwd)
        state = agent["agent_status"]
        if state not in {"idle", "working", "blocked", "done", "unknown"}:
            raise HerdrTransportError("unknown Herdr state")
        result["state"] = state
        for field in ("revision", "state_change_seq"):
            value = agent.get(field, 0 if field == "state_change_seq" else None)
            if type(value) is not int or value < 0:
                raise HerdrTransportError(f"invalid Herdr {field}")
            result[field] = value
        session = agent.get("agent_session")
        if session is not None:
            if not isinstance(session, dict):
                raise HerdrTransportError("invalid Herdr agent session")
            encoded = json.dumps(session, sort_keys=True, separators=(",", ":")).encode()
            result["agent_session"] = hashlib.sha256(encoded).hexdigest()
        return result
    except (KeyError, TypeError) as exc:
        raise HerdrTransportError("incomplete Herdr observation") from exc


class HerdrTransport:
    """Submit once, preserving uncertainty without logging terminal contents.

    Herdr's public prompt API has no expected-occupant compare-and-submit field.
    Callers must inspect before/after and retain uncertainty when identity changes.
    Neither socket identity nor a terminal observation grants card authority.
    """

    def __init__(self, *, runner=None, environ=None):
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner or self._run
        self._context = None

    def _run(self, argv: list[str], timeout: int):
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=self.environ,
        )

    def context(self) -> dict:
        """Bind actual host and local socket instance without changing sessions."""
        if self.environ.get("HERDR_ENV") != "1":
            raise HerdrTransportError("caller must be inside a Herdr-managed pane")
        name = self.environ.get("HERDR_SOCKET_PATH")
        if not name or not Path(name).is_absolute():
            raise HerdrTransportError("explicit absolute Herdr socket context is required")
        try:
            path = Path(name).resolve(strict=True)
            info = path.stat()
        except OSError as exc:
            raise HerdrTransportError("Herdr socket is unavailable") from exc
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise HerdrTransportError("Herdr context must be the caller's local socket")
        current = {
            "host": socket.gethostname(),
            "socket_path": str(path),
            "socket_dev": info.st_dev,
            "socket_inode": info.st_ino,
        }
        if self._context is not None and self._context != current:
            raise HerdrTransportError("Herdr socket context changed")
        self._context = current
        return dict(current)

    @staticmethod
    def _target(target: str) -> str:
        return _identifier(
            target, r"(?:[a-z][a-z0-9_-]{0,31}|w[A-Za-z0-9]+:p[A-Za-z0-9]+)", "target"
        )

    def inspect(self, target: str) -> dict:
        target = self._target(target)
        self.context()
        try:
            result = self.runner(["herdr", "agent", "get", target], 10)
            observation = _observation(_response(result))
            self.context()
            return observation
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise HerdrTransportError("Herdr inspection is unavailable") from exc

    def prompt(self, target: str, text: str) -> dict:
        """Submission success is not pickup; every ambiguous outcome stays unknown."""
        target = self._target(target)
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\0" in text
            or len(text.encode()) > 32768
        ):
            raise HerdrTransportError("prompt must contain 1 to 32768 bounded UTF-8 bytes")
        self.context()
        try:
            result = self.runner(["herdr", "agent", "prompt", target, text], 15)
            body = _response(result)
            if body.get("type") != "agent_prompted":
                raise HerdrTransportError("unexpected Herdr submission response")
            observation = _observation(body)
            self.context()
            return {"delivery": "submitted", "agent": observation}
        except (OSError, subprocess.SubprocessError, UnicodeError, HerdrTransportError):
            return {"delivery": "unknown", "reason": "submission-not-confirmed"}
