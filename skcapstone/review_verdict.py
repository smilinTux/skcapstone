"""Refuse to complete a review that never said anything.

A review card exists to produce a judgement. Completing one without recording a
verdict is the worst available outcome, because the board then shows the review
as DONE and the parent card as reviewed, while nobody ever wrote down what was
found. Silence is indistinguishable from approval at a glance, and it is the
reading everyone takes.

MEASURED ON THE LIVE BOARD, 2026-08-28. Of 317 completed review cards:

    278  recorded a verdict
     39  recorded nothing at all

Twelve percent of every review this estate has ever run passed by silence. One
of them, a93fd881, has exactly three structural events, claim, claim, complete,
and zero evidence rows. It reviewed a published candidate and said nothing, and
the board counted it.

The cost is not theoretical. c0b5fdbf's review card is complete with no outcome,
so the only honest thing that can be said about that candidate is that nobody
knows whether it passed. The work of reviewing it has to be paid again.

WHY THE WRITE PATH. The verdict contract already refuses a BLOCKED verdict that
does not explain itself, and that rule works because it fires where the value is
written. Asking reviewers to remember does not work; the worker brief has always
told them to return an exact PASS or BLOCKED, and 39 did not.

DELIBERATELY NARROW. Only cards that identify themselves as reviews or
rereviews are checked. PASS additionally requires the complete protected-branch
CI set. FAIL and structured BLOCKED remain terminal without waiting for CI,
because a reviewer must be able to stop an unsafe candidate immediately.
"""

from __future__ import annotations

import glob
import json
import re
from datetime import datetime
from pathlib import Path

from .card import CardEvent
from .review_admission import reviewer_candidate_reasons

#: Governed review and rereview cards use either a terminal tag or a tag with an
#: embedded identifier, for example [REVIEW] or [REREVIEW-119db735].
_REVIEW_TITLE_RE = re.compile(r"\[RE(?:RE)?VIEW(?:\]|-)", re.IGNORECASE)

#: Link keys that carry a verdict. Matched on shape rather than an exact list,
#: because the store has many spellings of the same idea.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)
_CHECK_KEY_RE = re.compile(r"(?:^|_)(?:check|checks|ci)(?:_|$)", re.IGNORECASE)
_SUCCESS_CHECK_STATE = "SUCCESS"
_SKCAPSTONE_REPOSITORY = "https://github.com/smilinTux/skcapstone"
_HOSTED_CHECKS_RE = re.compile(
    r"(?P<passed>[1-9][0-9]*)/(?P<total>[1-9][0-9]*) SUCCESS at exact head "
    r"(?P<head>[0-9a-f]{40})"
)
_REQUIRED_CI_LINK_KEYS = frozenset(
    {
        "ci_check_docs",
        "ci_check_gitleaks",
        "ci_check_lint",
        "ci_check_shim_imports",
        "ci_check_python311",
        "ci_check_python312",
    }
)
_RECEIPT_KEY = "applicability_receipt"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_HEAD_RE = re.compile(r"[0-9a-f]{40}")
_SOURCE_ONLY_EVIDENCE_KEYS = frozenset(
    {"evidence_sha256", "patch_sha256", "review_evidence_sha256", "reviewer_evidence_sha256"}
)
_SOURCE_ONLY_EVIDENCE_PATH_KEYS = frozenset({"evidence", "review_evidence"})
_PR_BINDING_KEY_RE = re.compile(r"(?:^|_)(?:open_)?pr(?:_|$)|pull_request", re.IGNORECASE)
_EMBEDDED_SHA256_RE = re.compile(r"(?:#|\|)sha256=([0-9a-f]{64})$", re.IGNORECASE)
_EMBEDDED_SHA256_MARKER_RE = re.compile(r"(?:#|\|)sha256=", re.IGNORECASE)


def _is_terminal_verdict(value: str) -> bool:
    """Accept only canonical terminal verdicts, never lookalike prefixes."""
    verdict = str(value or "").strip()
    if verdict in {"PASS", "FAIL"}:
        return True
    if not verdict.startswith("BLOCKED "):
        return False
    fields = verdict.split()[1:]
    return any(
        field.startswith("blocked_on=") and field != "blocked_on=" for field in fields
    ) and any(field.startswith("referent=") and field != "referent=" for field in fields)


def is_review_card(title: str) -> bool:
    """True when this card identifies itself as a review."""
    return bool(_REVIEW_TITLE_RE.search(str(title or "")))


def recorded_verdict(card_id: str, home: Path) -> str | None:
    """Return this card's recorded verdict from the evidence store, or None.

    Reads the EVIDENCE store, which is where verdicts actually live. Reading only
    the structure store is the recurring mistake in this codebase and is exactly
    why other detectors reported zero while the board was full of counterexamples.
    """
    evidence_dir = Path(home) / "coordination" / "card_events"
    if not evidence_dir.is_dir():
        return None
    latest: tuple[str, str] | None = None
    for path in sorted(glob.glob(str(evidence_dir / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or card_id not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("card_id") != card_id:
                        continue
                    # The raw evidence store writes link_key/link_value. Some
                    # readers normalize those to key/value, so accept both. Getting
                    # this wrong is silent: the lookup simply finds nothing and the
                    # card looks verdict-less. Caught on the live board when this
                    # returned none for e9d7900e, which had recorded PASS.
                    key = str(row.get("link_key") or row.get("key") or row.get("raw_key") or "")
                    value = str(row.get("link_value") or row.get("value") or "")
                    if not value.strip() or not _OUTCOME_KEY_RE.search(key):
                        continue
                    stamp = str(row.get("ts") or "")
                    if latest is None or stamp >= latest[0]:
                        latest = (stamp, value)
        except OSError:
            continue
    return latest[1] if latest else None


def unsuccessful_checks(card_id: str, home: Path) -> list[str]:
    """Return missing or non-successful required CI links."""
    evidence_dir = Path(home) / "coordination" / "card_events"
    latest: dict[str, tuple[str, str]] = {}
    for path in sorted(glob.glob(str(evidence_dir / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("card_id") != card_id or row.get("action") != "link":
                        continue
                    key = str(row.get("link_key") or row.get("key") or "")
                    value = str(row.get("link_value") or row.get("value") or "")
                    if not _CHECK_KEY_RE.search(key):
                        continue
                    candidate = (str(row.get("ts") or ""), value)
                    if key not in latest or candidate[0] >= latest[key][0]:
                        latest[key] = candidate
        except OSError:
            continue
    return sorted(
        key
        for key in _REQUIRED_CI_LINK_KEYS
        if key not in latest or latest[key][1] != _SUCCESS_CHECK_STATE
    )


def _card_events(card_id: str, home: Path):
    """Yield canonical overlay events, ignoring malformed historical lines."""
    for path in sorted(glob.glob(str(Path(home) / "coordination" / "card_events" / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict) or "ts" not in row:
                            continue
                        event = CardEvent.model_validate(row)
                    except (TypeError, ValueError):
                        continue
                    if event.card_id == card_id:
                        yield event.model_dump()
        except OSError:
            continue


def _event_position(row: dict) -> tuple[str, str, int] | None:
    """Return the exact CardStore order key after validating the timestamp."""
    stamp = row.get("ts")
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp, str(row.get("writer") or ""), int(row.get("seq") or 0)


def _normalized_identity(value: object) -> str:
    """Use the same principal normalization as governed review admission."""
    return str(value or "").strip().casefold().replace("_", "-")


def _has_pr_or_ci_binding(key: object) -> bool:
    """Return true for a governed pull request or hosted CI binding key."""
    lowered = str(key or "").strip().lower()
    return bool(
        lowered == "hosted_checks"
        or lowered in _REQUIRED_CI_LINK_KEYS
        or _PR_BINDING_KEY_RE.search(lowered)
    )


def _source_only_applicability(card_id: str, home: Path) -> bool:
    """Return true only for one valid, exact-card source-only receipt."""
    try:
        core = json.loads(
            (Path(home) / "cards" / card_id / "core.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    labels = {
        str(label).strip().lower()
        for label in [
            *(core.get("labels") or []),
            *(core.get("initial_labels") or []),
        ]
    }
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    labels.update(str(label).strip().lower() for label in (meta.get("labels") or []))

    overlay_events = list(_card_events(card_id, home))
    ordered_label_events = []
    for row in overlay_events:
        action = row.get("action")
        label = str(row.get("label") or "").strip().lower()
        if action not in {"add_label", "remove_label"} or label != "source-only":
            continue
        position = _event_position(row)
        if position is None:
            return False
        ordered_label_events.append((position, row))
    ordered_label_events.sort(key=lambda item: item[0])
    label_positions: dict[tuple[str, str, int], str] = {}
    for position, row in ordered_label_events:
        previous_action = label_positions.setdefault(position, str(row["action"]))
        if previous_action != row["action"]:
            return False
        label = str(row.get("label") or "").strip().lower()
        if not label:
            continue
        if row["action"] == "add_label":
            labels.add(label)
        else:
            labels.discard(label)
    if "source-only" not in labels:
        return False

    core_links = core.get("links") if isinstance(core.get("links"), dict) else {}
    expected_heads = {
        str(value).strip().lower()
        for value in (
            core_links.get("link_head_revision"),
            core_links.get("head_revision"),
            meta.get("link_head_revision"),
            meta.get("head_revision"),
        )
        if str(value or "").strip()
    }
    if len(expected_heads) != 1 or not _HEAD_RE.fullmatch(next(iter(expected_heads), "")):
        return False
    expected_head = next(iter(expected_heads))

    producers = {
        _normalized_identity(value)
        for value in (core_links.get("producer_identity"), meta.get("producer_identity"))
        if _normalized_identity(value)
    }
    if len(producers) != 1:
        return False
    producer = next(iter(producers))
    for mapping in (core_links, meta):
        if any(value is not None and _has_pr_or_ci_binding(key) for key, value in mapping.items()):
            return False

    events = [row for row in overlay_events if row.get("action") == "link"]
    receipts: list[tuple[dict, dict]] = []
    for row in events:
        key = row.get("link_key")
        if key == _RECEIPT_KEY:
            try:
                value = json.loads(row.get("link_value") or row.get("value") or "")
            except (TypeError, ValueError):
                return False
            receipts.append((row, value))
    if len(receipts) != 1 or not isinstance(receipts[0][1], dict):
        return False
    receipt_event, receipt = receipts[0]
    required = {"type", "card_id", "source_head", "reviewer", "evidence_digest", "governed_pr_ci"}
    if set(receipt) != required or receipt["type"] != "source-only-applicability":
        return False
    if (
        receipt["card_id"] != card_id
        or not isinstance(receipt["reviewer"], str)
        or not receipt["reviewer"].strip()
    ):
        return False
    reviewer = receipt["reviewer"].strip()
    reviewer_identity = _normalized_identity(reviewer)
    if (
        not reviewer_identity
        or _normalized_identity(receipt_event.get("writer")) != reviewer_identity
        or "producer-self-review" in reviewer_candidate_reasons(reviewer, producer=producer)
    ):
        return False
    if not isinstance(receipt["source_head"], str) or not _HEAD_RE.fullmatch(
        receipt["source_head"].lower()
    ):
        return False
    if receipt["source_head"].lower() != expected_head:
        return False
    if not isinstance(receipt["evidence_digest"], str) or not _SHA256_RE.fullmatch(
        receipt["evidence_digest"].lower()
    ):
        return False
    if receipt["governed_pr_ci"] is not False:
        return False
    latest_evidence: tuple[tuple[str, str, int], str, str] | None = None
    latest_evidence_path: tuple[tuple[str, str, int], str, str] | None = None
    latest_outcome: tuple[tuple[str, str, int], str, str] | None = None
    for row in events:
        key = str(row.get("link_key") or "").lower()
        if key == _RECEIPT_KEY:
            continue
        value = str(row.get("link_value") or row.get("value") or "")
        if _has_pr_or_ci_binding(key):
            return False
        position = _event_position(row)
        writer = _normalized_identity(row.get("writer"))
        is_evidence = key in _SOURCE_ONLY_EVIDENCE_KEYS
        is_evidence_path = key in _SOURCE_ONLY_EVIDENCE_PATH_KEYS
        is_outcome = bool(_OUTCOME_KEY_RE.search(key))
        if position is None and (is_evidence or is_evidence_path or is_outcome):
            return False
        if position is None:
            continue
        if is_evidence:
            candidate = (position, value.lower(), writer)
            if latest_evidence is not None and candidate[0] == latest_evidence[0]:
                if candidate[1:] != latest_evidence[1:]:
                    return False
            if latest_evidence is None or candidate[0] >= latest_evidence[0]:
                latest_evidence = candidate
        if is_evidence_path:
            candidate_path = (position, value.strip(), writer)
            if latest_evidence_path is not None and candidate_path[0] == latest_evidence_path[0]:
                if candidate_path[1:] != latest_evidence_path[1:]:
                    return False
            if latest_evidence_path is None or candidate_path[0] >= latest_evidence_path[0]:
                latest_evidence_path = candidate_path
            digest_markers = _EMBEDDED_SHA256_MARKER_RE.findall(value.strip())
            embedded_digest = _EMBEDDED_SHA256_RE.search(value.strip())
            if digest_markers and (len(digest_markers) != 1 or embedded_digest is None):
                return False
            if embedded_digest is not None:
                candidate = (position, embedded_digest.group(1).lower(), writer)
                if latest_evidence is not None and candidate[0] == latest_evidence[0]:
                    if candidate[1:] != latest_evidence[1:]:
                        return False
                if latest_evidence is None or candidate[0] >= latest_evidence[0]:
                    latest_evidence = candidate
        if is_outcome:
            candidate_outcome = (position, value, writer)
            if latest_outcome is not None and candidate_outcome[0] == latest_outcome[0]:
                if candidate_outcome[1:] != latest_outcome[1:]:
                    return False
            if latest_outcome is None or candidate_outcome[0] >= latest_outcome[0]:
                latest_outcome = candidate_outcome
    if (
        latest_evidence is None
        or latest_evidence[1] != receipt["evidence_digest"].lower()
        or latest_evidence[2] != reviewer_identity
    ):
        return False
    if (
        latest_evidence_path is None
        or not latest_evidence_path[1]
        or latest_evidence_path[2] != reviewer_identity
    ):
        return False
    linked_digest = _EMBEDDED_SHA256_RE.search(latest_evidence_path[1])
    if linked_digest is not None:
        if linked_digest.group(1).lower() != receipt["evidence_digest"].lower():
            return False
    if (
        latest_outcome is None
        or latest_outcome[1] != "PASS"
        or latest_outcome[2] != reviewer_identity
    ):
        return False
    receipt_position = _event_position(receipt_event)
    if receipt_position is None or receipt_position <= max(
        latest_evidence[0], latest_evidence_path[0], latest_outcome[0]
    ):
        return False
    return True


def _uses_repository_hosted_checks(card_id: str, home: Path) -> bool:
    """Validate exact hosted check evidence for non-SKCapstone repositories."""
    try:
        core = json.loads(
            (Path(home) / "cards" / card_id / "core.json").read_text(encoding="utf-8")
        )
        meta = core["meta"]
        repository = meta.get("repository")
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if not isinstance(repository, str):
        return False
    if repository.rstrip("/").removesuffix(".git") == _SKCAPSTONE_REPOSITORY:
        return False
    head = meta.get("link_head_revision")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError(f"review card {card_id} has no exact hosted checks head")

    evidence_dir = Path(home) / "coordination" / "card_events"
    latest: tuple[str, str] | None = None
    for path in sorted(glob.glob(str(evidence_dir / "*.jsonl"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    key = row.get("link_key") or row.get("key")
                    if row.get("card_id") != card_id or key != "hosted_checks":
                        continue
                    candidate = (
                        str(row.get("ts") or ""),
                        str(row.get("link_value") or row.get("value") or ""),
                    )
                    if latest is None or candidate[0] >= latest[0]:
                        latest = candidate
        except OSError:
            continue
    match = _HOSTED_CHECKS_RE.fullmatch(latest[1]) if latest else None
    if match is None or match["passed"] != match["total"] or match["head"] != head:
        raise ValueError(f"review card {card_id} has missing, incomplete, or stale hosted checks")
    return True


def validate_review_completion(card_id: str, title: str, home: Path) -> None:
    """Raise ValueError if a review card is being completed with no verdict.

    Args:
        card_id: The card being completed.
        title: That card's title, used to decide whether it is a review.
        home: The agent home containing the evidence store.

    Raises:
        ValueError: If the card is a review and has recorded no verdict.
    """
    if not is_review_card(title):
        return
    verdict = recorded_verdict(card_id, home)
    if verdict == "PASS":
        if _source_only_applicability(card_id, home):
            return
        if _uses_repository_hosted_checks(card_id, home):
            return
        checks = unsuccessful_checks(card_id, home)
        if not checks:
            return
        raise ValueError(
            f"review card {card_id} has required checks that are not successful: "
            + ", ".join(checks)
        )
    if verdict and _is_terminal_verdict(verdict):
        return
    if verdict:
        raise ValueError(
            f"review card {card_id} has nonterminal verdict {verdict!r}; "
            "record a canonical terminal verdict before completion"
        )
    raise ValueError(
        f"review card {card_id} has recorded no verdict, so it cannot be "
        "completed. A review exists to produce a judgement, and completing one "
        "silently marks the parent as reviewed while leaving no record of what "
        "was found. Record the outcome first, for example: "
        f"skcapstone coord link {card_id} verdict PASS after all required CI is SUCCESS."
    )
