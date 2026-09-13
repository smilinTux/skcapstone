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

from skcapstone.card_store import CardStore
from skcapstone.fleet.terminal_capacity import retire_worker_generation
from skcapstone.fleet.worker_watchdog import StartupObservation, classify_startup
from skcapstone.seat_mail import poll_mail, startup_hello


def runtime_route_identity(args: argparse.Namespace) -> dict[str, object]:
    """Return typed route identity only when the launcher supplied every field."""
    logical_route = str(getattr(args, "logical_route", "") or "")
    provider = str(getattr(args, "provider", "") or "")
    domains = list(getattr(args, "capacity_domain", ()) or ())
    if not logical_route or not provider or not domains or any(not value for value in domains):
        return {"route_schema": None, "capacity_domains": []}
    return {
        "route_schema": "skfleet.runtime-route/v1",
        "logical_route": logical_route,
        "provider": provider,
        "capacity_domains": domains,
        "model_or_bucket": args.model,
    }


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
    visited = set()
    expected_executable = Path(args.worker_executable).resolve()
    expected_env = {
        "SKAGENT": args.owner,
        "SKFLEET_CARD_ID": args.card,
        "SKFLEET_CLAIM_REVISION": args.claim_revision,
        "SKFLEET_SESSION_ID": args.session,
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
            if executable.name in {"node", "nodejs"} and not matches:
                try:
                    shebang = expected_executable.read_text(encoding="utf-8").splitlines()[0]
                    declared = Path(shebang.split()[-1]).name
                    title = (proc / "comm").read_text(encoding="utf-8").strip()
                    matches = (
                        shebang.startswith("#!")
                        and declared in {"node", "nodejs"}
                        and title == Path(args.worker_executable).name
                    )
                except (OSError, IndexError):
                    pass
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


def write_process_record(
    args: argparse.Namespace,
    *,
    pid: int,
    completion_state: str,
    heartbeat_at: str | None = None,
) -> None:
    """Publish bounded identity evidence for direct-seat execution."""
    record = {
        "card": args.card,
        "owner": args.owner,
        "claim_revision": args.claim_revision,
        "heartbeat_at": heartbeat_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "completion_state": completion_state,
        "pid": pid,
        **runtime_route_identity(args),
    }
    path = Path.home() / ".skcapstone/fleet/direct-seats" / (args.owner + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        pass


def maintain_process_record(args: argparse.Namespace, pid: int, stop: threading.Event) -> None:
    """Refresh direct-seat liveness until the wrapped process exits."""
    while not stop.wait(60):
        write_process_record(args, pid=pid, completion_state="running")


def review_supersession(args: argparse.Namespace) -> dict[str, str] | None:
    """Return exact supersession evidence for this claimed review generation."""
    card = CardStore(Path.home() / ".skcapstone").fold(args.card)
    if card is None or "review" not in {str(label).lower() for label in card.labels}:
        return None
    claim_revision = str(card.meta.get("_claim_revision") or "")
    if card.owner != args.owner or claim_revision != args.claim_revision:
        return None
    superseded_by = str(card.links.get("superseded_by") or "source-head-mismatch").strip()
    source_card = str(card.meta.get("link_source_card") or "").strip()
    source_head = str(card.meta.get("link_head_revision") or "").strip().lower()
    source = CardStore(Path.home() / ".skcapstone").fold(source_card) if source_card else None
    current_head = ""
    if source is not None:
        for key in ("candidate", "commit", "head"):
            value = str(source.links.get(key) or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", value):
                current_head = value
                break
    if not re.fullmatch(r"[0-9a-f]{40}", source_head) or not current_head:
        return None
    if current_head == source_head:
        return None
    return {
        "card_id": args.card,
        "claim_revision": args.claim_revision,
        "owner": args.owner,
        "source_card": source_card,
        "reviewed_head": source_head,
        "current_head": current_head,
        "superseded_by": superseded_by,
    }


def monitor_review_supersession(
    args: argparse.Namespace, child: subprocess.Popen, stop: threading.Event
) -> None:
    """Stop only this child process group when its review generation is obsolete."""
    while child.poll() is None and not stop.wait(15):
        evidence = review_supersession(args)
        if evidence is None:
            continue
        args.review_supersession = evidence
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if child.poll() is not None or stop.wait(0.25):
                return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return


def record_review_supersession(args: argparse.Namespace, stderr: bytes) -> Path | None:
    """Preserve immutable findings and exact generation evidence after stopping."""
    evidence = getattr(args, "review_supersession", None)
    if not isinstance(evidence, dict):
        return None
    try:
        unit_cgroup = next(
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
    except (OSError, StopIteration):
        unit_cgroup = ""
    stdout_digest = hashlib.sha256()
    with args.stdout.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            stdout_digest.update(chunk)
    payload = {
        **evidence,
        "host": args.host,
        "lane": args.lane,
        **runtime_route_identity(args),
        "session_id": args.session,
        "unit_cgroup": unit_cgroup,
        "stdout_log": str(args.stdout),
        "stdout_sha256": stdout_digest.hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "stopped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    directory = args.evidence_dir / "review-supersession"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.card}-{args.claim_revision}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return path


LOCK_RELEASE_ATTEMPTS = 3
LOCK_RELEASE_RETRY_BACKOFF_SECONDS = 0.5


def release_superseded_review_claim(args: argparse.Namespace) -> bool:
    """CAS-release this exact terminal generation through the locked Board.

    Board and card lock contention is transient: the locks are held only for
    the duration of one mutation. Retry the exact CAS release a bounded
    number of times so a temporarily contended lock cannot convert completed
    worker work into a failed process. Every retry carries the same
    ``expected_claim_revision``, so a release that landed before a timeout
    folds to the idempotent already-released check below instead of a second
    mutation. After the bound, fail truthfully with the contention chained.
    """
    from skcoord.coordination import Board

    home = Path.home() / ".skcapstone"
    released = False
    contention: TimeoutError | None = None
    for attempt in range(LOCK_RELEASE_ATTEMPTS):
        try:
            released = Board(home).release_claim(
                args.owner,
                args.card,
                actor=args.owner,
                expected_claim_revision=args.claim_revision,
            )
            contention = None
            break
        except TimeoutError as exc:
            contention = exc
            if attempt + 1 < LOCK_RELEASE_ATTEMPTS:
                time.sleep(LOCK_RELEASE_RETRY_BACKOFF_SECONDS)
        except (RuntimeError, ValueError):
            released = False
            break
    if not released:
        store = CardStore(home)
        card = store.fold(args.card)
        released = bool(
            card is not None
            and card.owner is None
            and not card.meta.get("_claim_revision")
            and any(
                event.get("action") == "release_claim"
                and event.get("released_owner") == args.owner
                and event.get("expected_claim_revision") == args.claim_revision
                for event in store._read_events(args.card)
            )
        )
    if not released:
        raise RuntimeError("terminal worker exact claim was not released") from contention
    return True


def write_startup_report(
    args: argparse.Namespace, pid: int, state: str, observation: StartupObservation | None = None
) -> None:
    """Preserve one immutable report for next-cycle fenced reconciliation."""
    payload = {
        "owner": args.owner,
        "card_id": args.card,
        "claim_revision": args.claim_revision,
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
        "mailbox": getattr(args, "mailbox_poll", None),
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
        if child.poll() is not None:
            write_startup_report(args, child.pid, "startup-child-exited", observation)
            return
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
    "upstream_template_rejection": re.compile(
        r"unable to generate parser\b|automatic parser generation failed", re.I
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
        r"failed to connect\b|unable to generate parser\b|"
        r"automatic parser generation failed\b",
        text,
        re.I,
    ):
        return None
    return classify_transport_failure(text)


def card_description_generation(card_id: str) -> str:
    """Return the immutable content generation one offer was minted against.

    The folded description carries the exact candidate identity (reviewed head,
    outcome generation, candidate digest) and never changes across the
    claim/release churn of one relaunch cycle. A changed description is a new
    generation and must not inherit an older generation's rejection hold.
    """
    try:
        card = CardStore(Path.home() / ".skcapstone").fold(card_id)
    except (OSError, ValueError):
        return ""
    description = str(getattr(card, "description", "") or "")
    if not description:
        return ""
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


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


def idle_owner_projection(
    owner: str, card_id: str | None = None, claim_revision: str | None = None
) -> None:
    """Leave projection mutation to locked Board writes and reconciliation."""
    del owner, card_id, claim_revision


def record_terminal_exit(args: argparse.Namespace, stderr: bytes, rc: int) -> None:
    """Create one immutable, claim-scoped terminal evidence record.

    ``card_generation`` must be the launch-time capture on ``args`` so a stale
    candidate-A rejection that finishes after the card advances to B stays
    attributed to A and cannot hold B.
    """
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
        "card_generation": str(getattr(args, "card_generation", "") or ""),
        "child_exit_code": rc,
        "claim_revision": args.claim_revision,
        "host": args.host,
        "lane": args.lane,
        "model": args.model,
        **runtime_route_identity(args),
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
    parser.add_argument("--logical-route", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--capacity-domain", action="append", default=[])
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--mail-recipient", default="jarvis")
    parser.add_argument("--live-snapshot", type=Path, default=None)
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


def preflight_mailbox(args: argparse.Namespace) -> bool:
    """Prove hello and read-only direct-plus-all mailbox access before work."""
    hello = startup_hello(Path.home() / ".skcapstone", args.owner, host=args.host)
    poll = poll_mail(args.owner)
    args.mailbox_poll = {
        "hello": hello,
        "ok": poll.ok,
        "new_messages": poll.new_messages,
        "help_or_handoff": poll.help_or_handoff,
        "digest": poll.digest,
        "error": poll.error,
    }
    return hello and poll.ok


def terminal_local_evidence(
    child: subprocess.Popen | None,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> bool:
    """Prove that the exact child and every peer in this worker cgroup exited."""
    if child is None or child.poll() is None or (proc_root / str(child.pid)).exists():
        return False
    try:
        control_group = next(
            line[3:]
            for line in (proc_root / "self/cgroup").read_text(encoding="utf-8").splitlines()
            if line.startswith("0::")
        )
        if not control_group.startswith("/") or ".." in control_group.split("/"):
            return False
        members = {
            int(value)
            for value in (cgroup_root / control_group.lstrip("/") / "cgroup.procs")
            .read_text(encoding="utf-8")
            .split()
        }
    except (FileNotFoundError, OSError, StopIteration, TypeError, ValueError):
        return False
    if members - {os.getpid()}:
        return False
    return True


def publish_terminal_capacity(args: argparse.Namespace, child: subprocess.Popen | None) -> bool:
    """Publish only after CardStore, process-tree, and cgroup evidence agree."""
    if args.live_snapshot is None or not terminal_local_evidence(child):
        return False
    return (
        retire_worker_generation(
            args.live_snapshot,
            Path.home() / ".skcapstone",
            args.host,
            args.card,
            args.owner,
            args.claim_revision,
        )
        is not None
    )


def finalize_terminal_capacity(args: argparse.Namespace, child: subprocess.Popen | None) -> bool:
    """Release one exact Board generation before retiring snapshot capacity."""
    if not hasattr(args, "review_supersession") and args.live_snapshot is None:
        return True
    released = release_superseded_review_claim(args)
    try:
        publish_terminal_capacity(args, child)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"terminal capacity publication failed: {exc}\n")
    return released


def finalize_worker_exit(args: argparse.Namespace, child: subprocess.Popen | None) -> None:
    """Release exact custody before removing the worker projection.

    Only a release that actually succeeded proves the terminal transition, so
    the projection may be idled solely on that success. When the release fails
    truthfully (for example persistent board lock contention exhausting the
    bounded retries), the exception propagates and the projection stays
    active for fenced reconciliation.
    """
    terminalized = False
    try:
        terminalized = finalize_terminal_capacity(args, child)
    finally:
        if terminalized:
            idle_owner_projection(args.owner, args.card, args.claim_revision)


def main() -> int:
    """Run the child, tee stderr to the journal, and record terminal evidence."""
    args = parse_args()
    args.started_at = int(time.time())
    preflight = preflight_worktree()
    if preflight == 2:
        write_startup_report(args, os.getpid(), "startup-preflight-blocked")
        return 2
    if not preflight_mailbox(args):
        write_startup_report(args, os.getpid(), "startup-mailbox-unavailable")
        return 2
    args.stdout.parent.mkdir(parents=True, exist_ok=True)
    # Capture before child launch: exit recording must not re-fold a later
    # candidate generation if the card advances while this worker is running.
    args.card_generation = card_description_generation(args.card)

    def _stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    startup_stop = threading.Event()
    startup_thread = None
    supersession_thread = None
    child = None
    try:
        with args.stdout.open("wb") as stdout:
            child = subprocess.Popen(
                args.command, stdout=stdout, stderr=subprocess.PIPE, start_new_session=True
            )
            write_process_record(args, pid=child.pid, completion_state="running")
            process_record_stop = threading.Event()
            process_record_thread = threading.Thread(
                target=maintain_process_record,
                args=(args, child.pid, process_record_stop),
                daemon=True,
            )
            process_record_thread.start()
            if args.session and args.worker_executable:
                startup_thread = threading.Thread(
                    target=monitor_startup, args=(args, child, startup_stop), daemon=True
                )
                startup_thread.start()
            supersession_thread = threading.Thread(
                target=monitor_review_supersession,
                args=(args, child, startup_stop),
                daemon=True,
            )
            supersession_thread.start()
            _, stderr = child.communicate()
            process_record_stop.set()
            process_record_thread.join(timeout=1)
        sys.stderr.buffer.write(stderr)
        final_supersession = review_supersession(args)
        if final_supersession is not None and not hasattr(args, "review_supersession"):
            args.review_supersession = final_supersession
        record_review_supersession(args, stderr)
        result_code = 75 if hasattr(args, "review_supersession") else child.returncode
        record_terminal_exit(args, stderr, result_code)
        write_process_record(
            args,
            pid=child.pid,
            completion_state="completed" if child.returncode == 0 else "failed",
        )
        emit_work_mail(
            args,
            "work.complete" if result_code == 0 else "work.blocked",
            f"phase=finished exit_code={result_code}",
        )
        return result_code
    finally:
        startup_stop.set()
        if startup_thread:
            startup_thread.join(timeout=6)
        if supersession_thread:
            supersession_thread.join(timeout=1)
        # A failed release must remain visible for fenced reconciliation.
        finalize_worker_exit(args, child)


if __name__ == "__main__":
    raise SystemExit(main())
