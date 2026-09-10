"""Elastic, independent review admission built on the existing worker wrapper.

This module is deliberately an admission and evidence contract, not a scheduler.
The caller supplies the existing CardStore claim/release adapter and launches each
accepted job through ``skfleet-worker-wrapper.py``.  A reviewer can never mutate
its producer checkout: the command is checked against a read-only allowlist and
its source head is bound at admission time.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


class ReviewAdmissionError(ValueError):
    """The review request violates an independent-review fence."""


@dataclass(frozen=True)
class ReviewRequest:
    card_id: str
    source_head: str
    producer: str
    reviewer: str
    session: str
    identity: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class ReviewReceipt:
    card_id: str
    reviewer: str
    identity: str
    source_head: str
    verdict: str
    artifact_sha256: str


READ_ONLY_TOOLS = frozenset({"git", "python", "pytest", "ruff", "sha256sum"})
MUTATING_TOKENS = frozenset(
    {"commit", "push", "merge", "reset", "checkout", "clean", "write", "rm"}
)
VERDICTS = frozenset({"PASS", "PASS_FOR_REVIEW", "BLOCKED"})


def review_fanout_limit(eligible: int, free_codex_slots: int, configured_maximum: int) -> int:
    """Return the exact bounded review fanout, rejecting invalid capacity."""
    values = (eligible, free_codex_slots, configured_maximum)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise ReviewAdmissionError("review capacities must be non-negative integers")
    return min(values)


def elastic_reviewer_identity(host: str, card_id: str) -> str:
    """Return a claim-fenced managed Codex reviewer identity."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", host) or not re.fullmatch(r"[0-9a-f]{8}", card_id):
        raise ReviewAdmissionError("invalid review host or card identity")
    return f"pi-codex-review-{host}-{card_id}"


def validate_request(request: ReviewRequest) -> None:
    """Fail closed before a claim or process is started."""
    if not request.card_id or not request.source_head or not request.identity:
        raise ReviewAdmissionError("card, exact source head, and identity are required")
    if request.reviewer == request.producer:
        raise ReviewAdmissionError("producer exclusion violated")
    if not request.command or request.command[0] not in READ_ONLY_TOOLS:
        raise ReviewAdmissionError("review command is not read-only")
    if any(token.lower() in MUTATING_TOKENS for token in request.command):
        raise ReviewAdmissionError("mutating review command denied")


def artifact_digest(artifact: bytes) -> str:
    return hashlib.sha256(artifact).hexdigest()


def append_jsonl(path: Path, value: dict[str, Any]) -> str:
    """Append only serializer output that round-trips as one JSON object.

    This is intentionally separate from structural CardStore events.  Callers
    record lifecycle claims/releases through CardStore and use this for review
    evidence, never deriving a verdict from lifecycle state or links.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise TypeError("evidence must serialize to a JSON object")
    path.parent.mkdir(parents=True, exist_ok=True)
    # CardStore is append-only: validate every prior line before extending the
    # log, so a damaged stream can never receive a seemingly valid suffix.
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            prior = json.loads(line)
            if not isinstance(prior, dict):
                raise ValueError("existing evidence line is not a JSON object")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(encoded + "\n")
    return artifact_digest(encoded.encode())


def run_bounded(
    requests: Iterable[ReviewRequest],
    run: Callable[[ReviewRequest], tuple[str, bytes]],
    *,
    max_workers: int = 2,
) -> list[ReviewReceipt]:
    """Run independent jobs with bounded concurrency and distinct identities."""
    jobs = list(requests)
    if not 1 <= max_workers <= 32:
        raise ReviewAdmissionError("max_workers must be between 1 and 32")
    for request in jobs:
        validate_request(request)
    if len({r.identity for r in jobs}) != len(jobs):
        raise ReviewAdmissionError("review identities must be unique")
    if len({r.card_id for r in jobs}) != len(jobs):
        raise ReviewAdmissionError("one-card claims are required")

    results: list[ReviewReceipt] = []
    lock = threading.Lock()

    def one(request: ReviewRequest) -> None:
        verdict, artifact = run(request)
        if verdict not in VERDICTS:
            raise ReviewAdmissionError(f"invalid verdict: {verdict}")
        receipt = ReviewReceipt(
            request.card_id,
            request.reviewer,
            request.identity,
            request.source_head,
            verdict,
            artifact_digest(artifact),
        )
        with lock:
            results.append(receipt)

    # ThreadPoolExecutor is only the bounded fan-out. Process lifecycle,
    # heartbeat, worktree isolation, and terminal cleanup remain wrapper duties.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="elastic-review") as pool:
        futures = [pool.submit(one, request) for request in jobs]
        for future in futures:
            future.result()
    return sorted(results, key=lambda receipt: receipt.card_id)
