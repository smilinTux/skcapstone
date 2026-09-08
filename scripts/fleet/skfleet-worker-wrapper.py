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
import threading
import time
from pathlib import Path

from skcapstone.card_store import CardStore, card_mutation_lock
from skcapstone.fleet.worker_watchdog import StartupObservation, classify_startup


def startup_observation(args: argparse.Namespace, child_pid: int) -> StartupObservation:
    """Read host-local heartbeat and actual attributed executable descendants.

    A shell with Pi text in its command is not executable evidence. Only a
    live executable, or a Node process whose script resolves to Pi, qualifies.
    Observation failures never authorize termination or release.
    """
    identity = dict(
        owner=args.owner,
        card_id=args.card,
        session_id=args.session,
        claim_revision=args.claim_revision,
        attempt_id=args.attempt_id,
    )
    heartbeat_at = None
    beat_path = Path.home() / ".skcapstone/fleet/beats" / f"{args.owner}.json"
    try:
        beat = json.loads(beat_path.read_text(encoding="utf-8"))
        if all(beat.get(key) == value for key, value in identity.items()):
            stamp = float(beat["beat_at"])
            if stamp >= args.started_at:
                heartbeat_at = datetime.datetime.fromtimestamp(
                    stamp, datetime.timezone.utc
                ).isoformat()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    evidence = None
    pending = [child_pid]
    visited: set[int] = set()
    expected_executable = Path(args.worker_executable).resolve()
    expected_env = {
        "SKAGENT": args.owner,
        "SKFLEET_CARD_ID": args.card,
        "SKFLEET_CLAIM_REVISION": args.claim_revision,
        "SKFLEET_SESSION_ID": args.session,
        "SKFLEET_ATTEMPT_ID": args.attempt_id,
    }
    while pending and len(visited) < 256:
        pid = pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            pending.extend(
                int(value) for value in (proc / f"task/{pid}/children").read_text().split()
            )
            executable = (proc / "exe").resolve(strict=True)
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            environment = dict(
                item.split(b"=", 1)
                for item in (proc / "environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            attributed = all(
                environment.get(key.encode()) == value.encode()
                for key, value in expected_env.items()
            )
            matches = executable == expected_executable
            if executable.name in {"node", "nodejs"} and len(argv) > 1:
                matches = Path(os.fsdecode(argv[1])).resolve() == expected_executable
            if matches and attributed:
                evidence = {
                    **identity,
                    "kind": "executable-work",
                    "pid": pid,
                    "executable": str(expected_executable),
                }
                break
        except (OSError, ValueError, RuntimeError):
            continue
    return StartupObservation(
        **identity,
        expected_claim_revision=args.claim_revision,
        heartbeat_seen=heartbeat_at is not None,
        heartbeat_at=heartbeat_at,
        executable_evidence_seen=evidence is not None,
        executable_evidence=evidence,
    )


def write_startup_report(
    args: argparse.Namespace, pid: int, state: str, observation: StartupObservation | None = None
) -> None:
    """Preserve one immutable report for next-cycle fenced reconciliation."""
    payload = {
        "owner": args.owner,
        "card_id": args.card,
        "claim_revision": args.claim_revision,
        "attempt_id": args.attempt_id,
        "session_id": args.session,
        "host": args.host,
        "state": state,
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "heartbeat_at": observation.heartbeat_at if observation else None,
        "executable_evidence": observation.executable_evidence if observation else None,
        "release_recommended": False,
        "lane": args.lane,
        "child_pid": pid,
        "control_group": None,
    }
    try:
        payload["control_group"] = next(
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
    except (OSError, StopIteration):
        pass
    # The wrapper and its unit still exist here. Exact fenced exit
    # cleanup belongs to the child shell, not this observer.
    directory = args.evidence_dir.parent / "worker-startup"
    key = hashlib.sha256(
        f"{args.owner}\0{args.claim_revision}\0{args.started_at}".encode()
    ).hexdigest()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{key}.json").open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        sys.stderr.write(f"startup evidence write failed: {exc}\n")
    emit_work_mail(args, "agent.status", f"phase=startup state={state}")


def monitor_startup(
    args: argparse.Namespace, child: subprocess.Popen, stop: threading.Event
) -> None:
    """Publish one bounded startup verdict without mistaking timeout for death."""
    deadline = time.monotonic() + args.startup_timeout
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        observation = startup_observation(args, child.pid)
        state = classify_startup(observation, now=now)
        if state == "startup-ready" or stop.is_set() or time.monotonic() >= deadline:
            write_startup_report(args, child.pid, state, observation)
            return
        stop.wait(min(1.0, max(0.0, deadline - time.monotonic())))


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


SUCCESS_OUTCOMES = frozenset({"PASS", "PASS_FOR_REVIEW"})
BLOCKED_OUTCOMES = frozenset({"BLOCKED", "FAIL_CLOSED"})


def _card_events(store: CardStore, card_id: str) -> list[dict]:
    """Return the same ordered event union used by the authoritative fold."""
    events = store._read_events(card_id) + store._legacy_events(card_id)
    return sorted(
        events,
        key=lambda event: (
            str(event.get("ts") or ""),
            str(event.get("writer") or ""),
            event.get("seq", 0),
        ),
    )


def validate_cardstore_completion(args: argparse.Namespace) -> tuple[bool, str, str | None]:
    """Validate terminal evidence against one exact CardStore claim generation."""
    if not args.session or not args.attempt_id:
        return False, "missing_attempt_identity", None
    home = Path.home() / ".skcapstone"
    try:
        store = CardStore(home)
        with card_mutation_lock(home, args.card):
            return _validate_and_consume_cardstore_completion(store, args)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, TimeoutError):
        return False, "cardstore_unavailable", None


def _validate_and_consume_cardstore_completion(
    store: CardStore, args: argparse.Namespace
) -> tuple[bool, str, str | None]:
    """Validate and consume one exact attempt outcome while holding the card lock."""
    card = store.fold(args.card)
    events = _card_events(store, args.card)
    if card is None:
        return False, "card_missing", None
    claim_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "claim"
        and event.get("owner") == args.owner
        and event.get("claim_revision") == args.claim_revision
    ]
    if not claim_indexes:
        return False, "claim_missing", None
    claim_index = claim_indexes[-1]
    if any(event.get("action") in {"claim", "reopen"} for event in events[claim_index + 1 :]):
        return False, "stale_claim", None
    identity = {
        "claim_revision": args.claim_revision,
        "attempt_id": args.attempt_id,
        "session_id": args.session,
    }
    outcome_events = [
        event
        for event in events[claim_index + 1 :]
        if event.get("action") == "link"
        and event.get("writer") == args.owner
        and event.get("link_key") in {"verdict", "evidence"}
    ]
    matching_events = [
        event
        for event in outcome_events
        if all(event.get(key) == value for key, value in identity.items())
    ]
    verdict_events = [event for event in matching_events if event.get("link_key") == "verdict"]
    evidence_events = [event for event in matching_events if event.get("link_key") == "evidence"]
    if not matching_events and outcome_events:
        if any(event.get("claim_revision") != args.claim_revision for event in outcome_events):
            return False, "cross_revision_outcome", None
        if any(event.get("session_id") != args.session for event in outcome_events):
            return False, "cross_session_outcome", None
        if any(event.get("attempt_id") != args.attempt_id for event in outcome_events):
            return False, "cross_attempt_outcome", None
    if not verdict_events:
        return False, "missing_terminal_card_outcome", None
    if len(verdict_events) != 1 or len(evidence_events) > 1:
        return False, "duplicate_terminal_outcome", None
    outcome = str(verdict_events[-1].get("link_value") or "").strip().upper()
    if outcome in BLOCKED_OUTCOMES:
        return False, "blocked_outcome", outcome
    if outcome not in SUCCESS_OUTCOMES:
        return False, "invalid_outcome", outcome or None
    if not evidence_events:
        return False, "missing_evidence", outcome
    revision = card.meta.get("_claim_revision")
    live_claim = card.owner == args.owner and revision == args.claim_revision
    completed_by_claim = any(
        event.get("action") == "complete" and event.get("writer") == args.owner
        for event in events[claim_index + 1 :]
    )
    if not live_claim and not (str(card.status.value) == "done" and completed_by_claim):
        return False, "stale_claim", outcome
    verdict_id = verdict_events[0].get("event_id")
    evidence_id = evidence_events[0].get("event_id")
    if not verdict_id or not evidence_id:
        return False, "malformed_terminal_outcome", outcome
    consumed = any(
        event.get("action") == "consume_terminal_outcome"
        and event.get("claim_revision") == args.claim_revision
        for event in events[claim_index + 1 :]
    )
    if consumed:
        return False, "claim_outcome_already_consumed", outcome
    store.append_event(
        args.card,
        "consume_terminal_outcome",
        args.owner,
        **identity,
        verdict_event_id=verdict_id,
        evidence_event_id=evidence_id,
        transition_id=f"consume:{args.card}:{args.claim_revision}",
    )
    return True, "valid_cardstore_completion", outcome


def classify_pre_agent_failure(
    stdout: bytes,
    stderr: bytes,
    rc: int,
    completion: tuple[bool, str, str | None] | None = None,
) -> str | None:
    """Classify terminal diagnostics, including invalid zero-exit completion."""
    if rc == 0:
        valid, reason, _outcome = completion or (False, "missing_completion_check", None)
        return None if valid else ("incomplete_" + reason)
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


def record_terminal_exit(
    args: argparse.Namespace,
    stderr: bytes,
    rc: int,
    completion: tuple[bool, str, str | None] | None = None,
) -> None:
    """Create one immutable, claim-scoped terminal evidence record."""
    stdout_size = args.stdout.stat().st_size
    stdout_tail = b""
    if stdout_size <= STDERR_LIMIT:
        stdout_tail = args.stdout.read_bytes()
    failure = classify_pre_agent_failure(stdout_tail, stderr, rc, completion)
    attempted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    valid, completion_reason, completion_outcome = completion or (
        False,
        "child_exit",
        None,
    )
    payload = {
        "attempted_at": attempted_at,
        "attempt_id": args.attempt_id,
        "card_id": args.card,
        "child_exit_code": rc,
        "claim_revision": args.claim_revision,
        "host": args.host,
        "lane": args.lane,
        "model": args.model,
        "owner": args.owner,
        "session_id": args.session,
        "completion_outcome": completion_outcome if valid else None,
        "stderr_bytes": len(stderr),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stdout_log": args.stdout.name,
        "transport_failure": failure if failure in TRANSPORT_PATTERNS else None,
        "completion_failure": failure if failure and failure.startswith("incomplete_") else None,
        "reason": failure or completion_reason,
    }
    digest = hashlib.sha256(
        f"{args.card}\0{args.claim_revision}\0{args.attempt_id}\0{attempted_at}".encode()
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
    parser.add_argument("--mail-recipient", default="jarvis")
    parser.add_argument("--session", default="")
    parser.add_argument("--worker-executable", default="")
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("child command is required")
    if args.startup_timeout <= 0 or not args.startup_timeout < float("inf"):
        parser.error("startup timeout must be finite and positive")
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
    args.started_at = int(time.time())
    args.attempt_id = hashlib.sha256(
        f"{args.card}\0{args.owner}\0{args.claim_revision}\0{args.session}\0"
        f"{args.started_at}\0{os.getpid()}".encode()
    ).hexdigest()
    preflight = preflight_worktree()
    if preflight == 2:
        write_startup_report(args, os.getpid(), "startup-preflight-blocked")
        return 2
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    emit_work_mail(args, "agent.hello", f"phase=started lane={args.lane} model={args.model}")

    def _stop(signum: int, _frame: object) -> None:
        idle_owner_projection(args.owner)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    startup_stop = threading.Event()
    startup_thread = None
    try:
        with args.stdout.open("wb") as stdout:
            child = subprocess.Popen(
                args.command,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env={**os.environ, "SKFLEET_ATTEMPT_ID": args.attempt_id},
            )
            if args.session and args.worker_executable:
                startup_thread = threading.Thread(
                    target=monitor_startup, args=(args, child, startup_stop), daemon=True
                )
                startup_thread.start()
            _, stderr = child.communicate()
        sys.stderr.buffer.write(stderr)
        completion = (
            validate_cardstore_completion(args)
            if child.returncode == 0
            else (False, "child_exit", None)
        )
        record_terminal_exit(args, stderr, child.returncode, completion)
        valid, completion_reason, outcome = completion
        effective_rc = child.returncode if child.returncode else (0 if valid else 75)
        emit_work_mail(
            args,
            "work.complete" if valid else "work.blocked",
            f"phase=finished exit_code={child.returncode} reason={completion_reason} "
            f"outcome={outcome or 'none'} attempt_id={args.attempt_id}",
        )
        return effective_rc
    finally:
        startup_stop.set()
        if startup_thread:
            startup_thread.join(timeout=6)
        # Always idle the worker projection on any exit path, including SIGTERM.
        idle_owner_projection(args.owner)


if __name__ == "__main__":
    raise SystemExit(main())
