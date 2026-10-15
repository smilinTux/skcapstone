"""Simple source-card acceptance: ``skcapstone coord accept <source_card>``.

A source card is accepted when exactly these facts hold, and nothing else is
consulted:

1. A linked review card has latest verdict PASS, written by a principal other
   than the source's producer.
2. The commit the review judged (``link_head_revision``) equals the commit the
   tests ran on (source link ``simple_finish_test_commit``).
3. The source links a test log (``simple_finish_test_log`` plus
   ``simple_finish_test_log_sha256``); the file hashes to that sha and shows a
   passing pytest or vitest summary.
4. The reviewed code is on the target branch (default ``origin/main``): the
   merged commit is an ancestor of the target and either the reviewed head is
   too, or the merged commit is a squash whose stable patch-id equals that of
   the reviewed head against its merge-base. No implicit ``git fetch``.
5. The test log shows no skipped tests unless the operator passes allow-skips.
6. No live worker process holds either card. A claim held by a dead or
   finished worker is released here as an audited operator action.

It deliberately does NOT require applicability receipts, profile qualification,
test-plan generations, acceptance contexts, ci_check_* links, policy digests or
live claim equality. Completion goes straight to the board mutation
(``Board.complete_task``) and so bypasses ``validate_review_completion``
by design. That bypass is local to this module; other callers of
``validate_review_completion`` are unchanged.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path

OPERATORS = frozenset({"lumina-nor", "jarvis"})
RELEASE_ACTOR = "lumina-nor"
RELEASE_REASON = "simple-accept"

_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PASS = re.compile(r"PASS(?![_A-Za-z0-9])")
# Final pytest/vitest summary lines look like "== 12 passed, 1 skipped in 3s ==",
# "12 passed in 3.2s" or vitest "Tests  12 passed (12)".
_PASSED = re.compile(r"\b\d+ passed\b")
_BAD = re.compile(r"\b\d+ (failed|errors?)\b|\bFAILED\b", re.I)
_SKIPPED = re.compile(r"\b[1-9]\d* skipped\b")
_SUMMARY_TAIL = 8
DEFAULT_TARGET_REF = "origin/main"


class AcceptRefusedError(Exception):
    """The facts for acceptance do not hold; the message is the one-line reason."""


def log_shows_success(text: str) -> bool:
    """True if the log's final summary lines show 'passed' and no failure/error."""
    lines = [line for line in text.splitlines() if line.strip()][-_SUMMARY_TAIL:]
    tail = "\n".join(lines)
    return bool(_PASSED.search(tail)) and not _BAD.search(tail)


def _norm(value: object) -> str:
    return str(value or "").strip().casefold().replace("_", "-")


def _events(store, card_id: str) -> list[dict]:
    return sorted(
        store._read_events(card_id) + store._legacy_events(card_id),
        key=lambda e: (str(e.get("ts", "")), e.get("seq", 0)),
    )


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptRefusedError(f"git failed: {exc}") from None


def _patch_id(repo: Path, base: str, head: str) -> str:
    diff = _git(repo, "diff", base, head)
    if diff.returncode != 0:
        raise AcceptRefusedError(f"git diff {base[:12]} {head[:12]} failed")
    out = _git(repo, "patch-id", "--stable", stdin=diff.stdout)
    fields = out.stdout.split()
    if out.returncode != 0 or not fields:
        raise AcceptRefusedError("reviewed code has an empty diff; no patch-id to compare")
    return fields[0].decode()


def verify_merged(repo_path: Path, merged: str, reviewed: str, target: str) -> str:
    """Raise unless ``merged`` is on ``target`` and carries the ``reviewed`` code.

    Returns "ancestor" (reviewed head is on target) or "squash" (patch-id match).
    """
    repo = Path(repo_path).expanduser()
    if not repo.is_dir():
        raise AcceptRefusedError(f"repo path {repo} is not a directory")
    for sha, what in ((merged, "merged commit"), (reviewed, "reviewed head")):
        if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            raise AcceptRefusedError(f"{what} {sha[:12]} not found in {repo}")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}").returncode != 0:
        raise AcceptRefusedError(f"target ref {target} not found in {repo}")
    if _git(repo, "merge-base", "--is-ancestor", merged, target).returncode != 0:
        raise AcceptRefusedError(
            f"reviewed code not on {target}: {merged[:12]} is not an ancestor"
        )
    if _git(repo, "merge-base", "--is-ancestor", reviewed, target).returncode == 0:
        return "ancestor"
    base = _git(repo, "merge-base", reviewed, target)
    if base.returncode != 0 or not base.stdout.strip():
        raise AcceptRefusedError(f"reviewed code not on {target}: no merge-base with {target}")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{merged}^").returncode != 0:
        raise AcceptRefusedError(f"reviewed code not on {target}: {merged[:12]} has no parent")
    if _patch_id(repo, base.stdout.decode().strip(), reviewed) != _patch_id(
        repo, f"{merged}^", merged
    ):
        raise AcceptRefusedError(
            f"reviewed code not on {target}: patch-id of {merged[:12]} differs from reviewed head"
        )
    return "squash"


def _producer(store, source, override: str | None = None) -> str:
    if override:
        return override.strip()
    value = source.links.get("producer_identity_override") or None
    if value:
        return str(value).strip()
    value = source.links.get("producer_identity") or source.meta.get("producer_identity")
    if value:
        return str(value).strip()
    owners = [e.get("owner") for e in _events(store, source.id) if e.get("action") == "claim"]
    owners = [o for o in owners if o]
    if owners:
        return str(owners[0])
    if source.owner:
        return str(source.owner)
    raise AcceptRefusedError("producer identity of the source is unknown")


def _verdict_writer(home: Path, store, review) -> str:
    """Writer of the review's latest outcome; falls back to its last claimant."""
    from .review_verdict import _OUTCOME_KEY_RE, _card_events

    best: tuple[str, str] | None = None
    for row in _card_events(review.id, home):
        key = str(row.get("link_key") or row.get("key") or "")
        if row.get("action") == "link" and _OUTCOME_KEY_RE.search(key) and row.get("writer"):
            stamp = (str(row.get("ts") or ""), str(row["writer"]))
            if best is None or stamp >= best:
                best = stamp
    if best:
        return best[1]
    claims = [e.get("owner") for e in _events(store, review.id) if e.get("action") == "claim"]
    if claims and claims[-1]:
        return str(claims[-1])
    raise AcceptRefusedError("review verdict has no identifiable writer")


def _find_review(home: Path, store, source):
    from .review_verdict import recorded_verdict

    candidates: list[str] = []
    linked = str(source.links.get("independent_review") or "").strip()
    if linked:
        candidates.append(linked)
    for card_id in store.list_card_ids():
        if card_id in candidates or card_id == source.id:
            continue
        card = store.fold(card_id)
        if (
            card is not None
            and not card.archived
            and (
                card.meta.get("link_source_card") == source.id
                or card.links.get("link_source_card") == source.id
            )
        ):
            candidates.append(card_id)
    found = [c for c in (store.fold(i) for i in candidates) if c is not None]
    if not found:
        raise AcceptRefusedError("no review card is linked to the source")
    for card in found:
        verdict = recorded_verdict(card.id, home)
        if verdict and _PASS.match(verdict.strip()):
            return card
    latest = recorded_verdict(found[0].id, home)
    raise AcceptRefusedError(f"review {found[0].id} latest verdict is {latest!r}, not PASS")


def _liveness_pid(store, card) -> int | None:
    """PID of an active recorded worker for this exact claim, else None."""
    claim = card.meta.get("_claim_revision")
    for event in reversed(_events(store, card.id)):
        if event.get("action") == "link" and event.get("link_key") == "worker_liveness":
            parts = str(event.get("link_value") or "").split("|")
            if len(parts) == 3 and parts[0] == card.owner and parts[1] == claim:
                pid = event.get("process_pid")
                return pid if parts[2] == "active" and isinstance(pid, int) else None
            return None
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _check_live_workers(store, cards) -> None:
    for card in cards:
        if card.owner:
            pid = _liveness_pid(store, card)
            if pid is not None and _pid_alive(pid):
                raise AcceptRefusedError(f"live worker {card.owner} (pid {pid}) holds {card.id}")


def _release_if_held(home: Path, store, card_id: str) -> None:
    """Release a dead worker's claim as an audited operator action."""
    from skcoord.card_store import card_mutation_lock

    from .coordination import Board

    with card_mutation_lock(home, card_id):
        card = store.fold(card_id)
        if card is None or not card.owner:
            return
        owner = card.owner
        store.append_event(
            card_id,
            "release_claim",
            RELEASE_ACTOR,
            released_owner=owner,
            expected_claim_revision=card.meta.get("_claim_revision"),
            transition_id=uuid.uuid4().hex,
            abandon_reason="not-abandoned",
            finished_work_override=True,
            finished_work_override_reason=RELEASE_REASON,
        )
        after = store.fold(card_id)
        if after is None or after.owner is not None:
            raise AcceptRefusedError(f"release of {card_id} did not take effect")
    board = Board(home)
    projection = board.load_agent(owner)
    if projection is not None:
        projection.claimed_tasks = [t for t in projection.claimed_tasks if t != card_id]
        if projection.current_task == card_id:
            projection.current_task = None
        board.save_agent(projection)


def _link(home: Path, card_id: str, key: str, value: str, actor: str) -> None:
    from .card import CardEvent, CardEventLog

    CardEventLog(home).append(
        CardEvent(card_id=card_id, action="link", link_key=key, link_value=value, writer=actor)
    )


def accept_source(
    home: Path,
    source_id: str,
    actor: str,
    *,
    merged_commit: str | None = None,
    repo_path: Path | str | None = None,
    target_ref: str = DEFAULT_TARGET_REF,
    producer: str | None = None,
    allow_skips: bool = False,
) -> str:
    """Accept ``source_id``; return a one-line result or raise AcceptRefusedError."""
    from skcoord.card_store import CardStore

    from .coordination import Board
    from .review_admission import reviewer_candidate_reasons

    home = Path(home).expanduser()
    if _norm(actor) not in OPERATORS:
        raise AcceptRefusedError(f"{actor} is not an operator identity (lumina-nor, jarvis)")
    store = CardStore(home)
    source = store.fold(source_id)
    if source is None:
        raise AcceptRefusedError(f"source card {source_id} not found")
    if source.status.value == "done":
        return f"already accepted: {source_id}"

    review = _find_review(home, store, source)
    producer_override_used = bool(producer and producer.strip())

    producer = _producer(store, source, producer)
    reviewer = _verdict_writer(home, store, review)
    if _norm(reviewer) == _norm(producer) or "producer-self-review" in (
        reviewer_candidate_reasons(reviewer, producer=producer)
    ):
        raise AcceptRefusedError(f"reviewer {reviewer} is the producer {producer} (self-review)")

    judged = str(review.meta.get("link_head_revision") or review.links.get("link_head_revision"))
    tested = str(source.links.get("simple_finish_test_commit") or "").strip().lower()
    if not _SHA40.fullmatch(tested):
        raise AcceptRefusedError("source has no valid simple_finish_test_commit (40-hex)")
    if judged.strip().lower() != tested:
        raise AcceptRefusedError(f"review judged {judged[:12]} but tests ran on {tested[:12]}")

    log_path = str(source.links.get("simple_finish_test_log") or "").strip()
    log_sha = str(source.links.get("simple_finish_test_log_sha256") or "").strip().lower()
    if not log_path or not _SHA256.fullmatch(log_sha):
        raise AcceptRefusedError("source has no simple_finish_test_log with a sha256")
    try:
        data = Path(log_path).expanduser().read_bytes()
    except OSError as exc:
        raise AcceptRefusedError(f"test log unreadable: {exc.strerror or exc}") from None
    if hashlib.sha256(data).hexdigest() != log_sha:
        raise AcceptRefusedError("test log sha256 does not match the file")
    if not log_shows_success(data.decode("utf-8", errors="replace")):
        raise AcceptRefusedError("test log does not show a passing summary")

    text = data.decode("utf-8", errors="replace")
    if _SKIPPED.search(text) and not allow_skips:
        raise AcceptRefusedError(
            "test log has skipped tests (zero unapproved skips); pass --allow-skips to approve"
        )

    merged = (merged_commit or str(source.links.get("merged_commit") or "")).strip().lower()
    if not _SHA40.fullmatch(merged):
        raise AcceptRefusedError(
            "source has no merged_commit (40-hex); link it or pass --merged-commit"
        )
    if repo_path is None:
        raise AcceptRefusedError("--repo-path is required to verify the merged commit")
    how = verify_merged(Path(repo_path), merged, tested, target_ref)

    _check_live_workers(store, (source, review))
    for card in (review, source):
        _release_if_held(home, store, card.id)

    _link(home, source_id, "verdict", "PASS", actor)
    _link(home, source_id, "accepted_review", review.id, actor)
    _link(home, source_id, "accepted_test_log_sha256", log_sha, actor)
    _link(home, source_id, "merged_commit", merged, actor)
    _link(home, source_id, "accepted_merge_check", f"{how}:{target_ref}", actor)
    if producer_override_used:
        _link(home, source_id, "producer_identity_override", producer.strip(), actor)
    if allow_skips and _SKIPPED.search(text):
        _link(home, source_id, "accepted_allow_skips", "true", actor)

    board = Board(home)
    for card_id in (review.id, source_id):
        current = store.fold(card_id)
        if current is not None and current.status.value != "done":
            board.complete_task(actor, card_id)
        done = store.fold(card_id)
        if done is None or done.status.value != "done":
            raise AcceptRefusedError(f"completion readback failed for {card_id}")
    return (
        f"accepted: {source_id} (review {review.id}, tests on {tested[:12]}, "
        f"merged {merged[:12]} via {how})"
    )
