#!/usr/bin/env python3
"""Run one fleet worker and preserve bounded terminal diagnostics."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

STDERR_LIMIT = 2048
TRANSPORT_PATTERNS = {
    "rate_limited": re.compile(r"(?:\b429\b|rate.?limit)", re.I),
    "model_owner_backend_down": re.compile(r"model_owner_backend_down", re.I),
    "backend_claims_quarantined": re.compile(r"backend-claims-quarantined", re.I),
    "invalid_upstream_tool_calls": re.compile(r"invalid_upstream_tool_calls", re.I),
    "connection_failure": re.compile(
        r"connection (?:error|failed|failure|refused|reset|timed? ?out)|"
        r"failed to connect|network is unreachable|temporary failure in name resolution",
        re.I,
    ),
}
SECRET_RE = re.compile(
    r"(?i)(authorization:\s*(?:bearer|basic)\s+|"
    r"(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)\S+"
)
TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_-]{32,})\b")


def classify_transport_failure(text: str) -> str | None:
    """Return the allow-listed pre-agent transport failure class."""
    for kind, pattern in TRANSPORT_PATTERNS.items():
        if pattern.search(text):
            return kind
    return None


def classify_pre_agent_failure(stdout: bytes, stderr: bytes, rc: int) -> str | None:
    """Classify only terminal diagnostics that precede substantive output."""
    if rc == 0:
        return None
    if not stdout:
        return classify_transport_failure(redact_stderr(stderr))
    text = stdout.decode("utf-8", errors="replace").strip()
    if not re.match(
        r"(?:HTTP\s+)?(?:429|5\d\d)\b|model_owner_backend_down\b|"
        r"backend-claims-quarantined\b|invalid_upstream_tool_calls\b|"
        r"connection (?:error|failed|failure|refused|reset|timed? ?out)\b|"
        r"failed to connect\b",
        text,
        re.I,
    ):
        return None
    return classify_transport_failure(text)


def redact_stderr(stderr: bytes) -> str:
    """Return a bounded diagnostic with common credentials removed."""
    text = stderr[-STDERR_LIMIT:].decode("utf-8", errors="replace")
    text = SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    return TOKEN_RE.sub("[REDACTED]", text)


def idle_owner_projection(owner: str) -> None:
    """Clear the ephemeral worker agent file so monitors stop listing ghosts.

    release-claim frees the card, but the agent projection can stay
    state=active with current_task set. skfleet-working then shows
    STALE PROJECTION after the unit is gone. Fail soft: never block exit.
    """
    path = Path.home() / ".skcapstone" / "coordination" / "agents" / f"{owner}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or data.get("agent") != owner:
        return
    data["state"] = "idle"
    data["current_task"] = None
    data["claimed_tasks"] = []
    data["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def record_terminal_exit(args: argparse.Namespace, stderr: bytes, rc: int) -> None:
    """Create one immutable, claim-scoped terminal evidence record."""
    stdout_size = args.stdout.stat().st_size
    stdout_tail = b""
    if stdout_size <= STDERR_LIMIT:
        stdout_tail = args.stdout.read_bytes()
    failure = classify_pre_agent_failure(stdout_tail, stderr, rc)
    if stdout_size and not failure:
        return
    attempted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    redacted = redact_stderr(stderr)
    payload = {
        "attempted_at": attempted_at,
        "card_id": args.card,
        "child_exit_code": rc,
        "claim_revision": args.claim_revision,
        "host": args.host,
        "lane": args.lane,
        "model": args.model,
        "owner": args.owner,
        "stderr": redacted,
        "stdout_log": args.stdout.name,
        "transport_failure": failure,
    }
    digest = hashlib.sha256(
        f"{args.card}\0{args.claim_revision}\0{attempted_at}".encode()
    ).hexdigest()[:16]
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = args.evidence_dir / f"{args.card}-{digest}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    """Parse wrapper metadata and the child command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--card", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--claim-revision", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("child command is required")
    return args


def preflight_worktree() -> int:
    """Clear safe stale sequencer state; refuse to start on dirty stale state."""
    helper = Path(__file__).resolve().parent / "worktree-hygiene.py"
    if not helper.exists():
        return 0
    r = subprocess.run(
        [sys.executable, str(helper), "--clear", os.getcwd()], capture_output=True, text=True
    )
    if r.stdout.strip():
        sys.stderr.write(r.stdout)
    if r.returncode == 2:
        sys.stderr.write(
            "worktree preflight blocked: stale sequencer state with a dirty tree; "
            "resolve it by hand before starting a worker\n"
        )
    return r.returncode


def main() -> int:
    """Run the child, tee stderr to the journal, and record terminal evidence."""
    args = parse_args()
    preflight = preflight_worktree()
    if preflight == 2:
        return 2
    args.stdout.parent.mkdir(parents=True, exist_ok=True)

    def _stop(signum: int, _frame: object) -> None:
        idle_owner_projection(args.owner)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        with args.stdout.open("wb") as stdout:
            child = subprocess.run(args.command, stdout=stdout, stderr=subprocess.PIPE)
        sys.stderr.buffer.write(child.stderr)
        record_terminal_exit(args, child.stderr, child.returncode)
        return child.returncode
    finally:
        # Always idle the worker projection on any exit path, including SIGTERM.
        idle_owner_projection(args.owner)


if __name__ == "__main__":
    raise SystemExit(main())
