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
import shutil
import subprocess
import sys
import threading
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
DEFAULT_MAIL_HEARTBEAT_INTERVAL = 300


def seat_from_owner(owner: str) -> str:
    """Return the governed seat segment when a worker owner carries one."""
    parts = owner.strip().split("-")
    if len(parts) >= 4 and parts[0] == "pi":
        return parts[1]
    return "lane-worker"


VERDICT_RE = re.compile(
    r"(?im)(?:^\s*|[\"']verdict[\"']\s*:\s*[\"'])(PASS_FOR_REVIEW|BLOCKED)\b[^\n]*"
)
HASH_RE = re.compile(r"(?i)(?:sha256|artifact_sha256)[=: ]+([0-9a-f]{64})")
PR_RE = re.compile(r"https?://[^\s]+/pull/[0-9]+")
COMMIT_RE = re.compile(r"(?i)\bcommit[=: ]+([0-9a-f]{7,64})\b")
REFERENT_RE = re.compile(r"(?i)blocked_on=card\s+referent=([0-9a-f]{8})")


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


def emit_work_mail(args: argparse.Namespace, kind: str, body: str) -> None:
    """Best-effort lifecycle notice; mailbox health never changes job outcome."""
    helper = Path(__file__).with_name("skmail_work.py")
    if not helper.exists():
        return
    try:
        subprocess.run(
            [
                sys.executable,
                str(helper),
                kind,
                args.owner,
                args.mail_recipient,
                args.card,
                args.claim_revision,
                body,
                "--host",
                args.host,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def mail_heartbeat_interval() -> int:
    """Return a safe periodic status interval without changing worker leases."""
    try:
        value = int(os.environ.get("SKFLEET_MAIL_HEARTBEAT_INTERVAL", ""))
    except ValueError:
        return DEFAULT_MAIL_HEARTBEAT_INTERVAL
    return value if value > 0 else DEFAULT_MAIL_HEARTBEAT_INTERVAL


def poll_mailbox(owner: str) -> str:
    """Read direct and ``all`` mail without acknowledging it.

    The reader includes messages addressed to ``all``. Mail is coordination
    only, so a read failure becomes a bounded status detail and never changes
    the worker's claim or exit result.
    """
    command = shutil.which("skmail")
    if not command:
        return "mailbox=unavailable"
    try:
        result = subprocess.run(
            [command, "read", owner],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "mailbox=error"
    if result.returncode != 0:
        return "mailbox=error"
    output = result.stdout or ""
    match = re.search(r"\((\d+) new\)\s*$", output, re.MULTILINE)
    new_count = match.group(1) if match else "0"
    lower = output.lower()
    help_count = sum(lower.count(word) for word in ("help", "handoff", "dependency"))
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
    return f"mailbox=ok new={new_count} help_or_handoff={help_count} digest={digest}"


def mail_heartbeat_loop(
    args: argparse.Namespace,
    stop_event: threading.Event,
    interval: int | None = None,
) -> None:
    """Emit bounded status notices while the child is still running.

    This is observability only: it never reads or changes claim state.  A
    stop event and a bounded subprocess timeout keep shutdown deterministic.
    """
    wait_for = interval if interval is not None else mail_heartbeat_interval()
    while not stop_event.wait(wait_for):
        emit_work_mail(
            args,
            "agent.status",
            (
                f"phase=running seat={seat_from_owner(args.owner)} "
                f"lane={args.lane} model={args.model} {poll_mailbox(args.owner)}"
            ),
        )


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


def _current_claim(args: argparse.Namespace, store: object) -> bool:
    """Return true only while this owner and revision still hold the card."""
    try:
        events = store._read_events(args.card)  # CardStore's fenced read path.
    except Exception:  # noqa: BLE001
        return False
    current = None
    for event in sorted(events, key=lambda row: str(row.get("ts") or "")):
        action = event.get("action")
        if action == "claim":
            current = (event.get("owner"), event.get("claim_revision"))
        elif action in {"release_claim", "complete", "void"}:
            current = None
    return current == (args.owner, args.claim_revision)


def _terminal_evidence(
    args: argparse.Namespace,
) -> tuple[str, Path, str, str | None, str | None] | None:
    """Find explicit review/blocker metadata without copying arbitrary output."""
    roots = [Path.home() / ".skcapstone" / "evidence" / "work" / args.card]
    if args.stdout.exists():
        roots.append(args.stdout)
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))[:64]
        for path in paths:
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 1_048_576:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = VERDICT_RE.search(text)
            if not match:
                continue
            verdict = match.group(1).upper()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            pr = PR_RE.search(text)
            commit = COMMIT_RE.search(text)
            if verdict == "BLOCKED" and not REFERENT_RE.search(text):
                continue
            return (
                verdict,
                path,
                digest,
                pr.group(0) if pr else None,
                commit.group(1) if commit else None,
            )
    return None


def record_terminal_card_links(args: argparse.Namespace) -> None:
    """Persist explicit terminal evidence once, fenced to this claim generation."""
    found = _terminal_evidence(args)
    if not found:
        return
    verdict, path, digest, pr, commit = found
    try:
        from skcoord.card_store import CardStore

        store = CardStore(Path.home() / ".skcapstone")
        if not _current_claim(args, store):
            return
        transition = f"worker-terminal-{args.card}-{args.claim_revision}"
        evidence_value = f"file:{path} sha256:{digest} claim_revision={args.claim_revision}"
        if verdict == "BLOCKED":
            text = path.read_text(encoding="utf-8", errors="replace")
            referent = REFERENT_RE.search(text)
            verdict_value = f"BLOCKED blocked_on=card referent={referent.group(1)}"
        else:
            verdict_value = (
                f"PASS_FOR_REVIEW|claim_revision={args.claim_revision}|"
                f"artifact_sha256={digest}"
            )
        if pr:
            verdict_value += f"|PR={pr}"
        if commit:
            verdict_value += f"|commit={commit}"
        store.append_event(args.card, "link", args.owner, link_key="evidence",
                           link_value=evidence_value, transition_id=f"{transition}-evidence")
        store.append_event(args.card, "link", args.owner, link_key="verdict",
                           link_value=verdict_value, transition_id=f"{transition}-verdict")
        if pr:
            store.append_event(args.card, "link", args.owner, link_key="pr",
                               link_value=pr, transition_id=f"{transition}-pr")
    except Exception:  # noqa: BLE001
        # Terminal mail and immutable exit evidence remain the fallback record.
        return


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
    parser.add_argument("--mail-recipient", default="all")
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
    emit_work_mail(
        args,
        "agent.hello",
        f"phase=started seat={seat_from_owner(args.owner)} lane={args.lane} model={args.model}",
    )
    emit_work_mail(args, "agent.status", f"phase=mailbox_poll {poll_mailbox(args.owner)}")

    def _stop(signum: int, _frame: object) -> None:
        idle_owner_projection(args.owner)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=mail_heartbeat_loop,
        args=(args, heartbeat_stop),
        name="skmail-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    try:
        with args.stdout.open("wb") as stdout:
            child = subprocess.run(args.command, stdout=stdout, stderr=subprocess.PIPE)
        sys.stderr.buffer.write(child.stderr)
        record_terminal_exit(args, child.stderr, child.returncode)
        record_terminal_card_links(args)
        emit_work_mail(
            args,
            "work.complete" if child.returncode == 0 else "work.blocked",
            f"phase=finished exit_code={child.returncode}",
        )
        return child.returncode
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=5)
        # Always idle the worker projection on any exit path, including SIGTERM.
        idle_owner_projection(args.owner)


if __name__ == "__main__":
    raise SystemExit(main())
