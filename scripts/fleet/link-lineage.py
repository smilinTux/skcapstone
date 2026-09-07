#!/usr/bin/env python3
"""Read-only reconciliation of open Link PRs to CardStore card lineage.

This is deliberately a dry-run tool: it never writes CardStore or the Link
observation feed.  CardStore is read through its projection and event files are
parsed as JSON, so malformed evidence cannot silently become lineage.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

PR_RE = re.compile(r"\bPR\s*#?\s*(\d+)\b", re.I)
CARD_RE = re.compile(r"\b([0-9a-f]{8})\b", re.I)
_REVIEWER_SCHEMA = "skfleet.reviewer-identity/v1"
_EXCLUSION_SCHEMA = "skfleet.link-lineage-exclusion/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_TERMINAL_REVIEW = frozenset({"PASS", "FAIL", "BLOCKED"})
_EXCLUSION_CLASSIFICATIONS = frozenset({"unmanaged", "review-artifact", "superseded"})


def _reviewer_candidates(
    home: Path, explicit: list[dict[str, Any]] | None
) -> list[dict[str, str]]:
    """Read and validate the producer's authoritative reviewer contract."""
    if explicit is None:
        source = home / "identity" / "reviewer-candidates.json"
        if not source.exists():
            return []
        value = json.loads(source.read_text(encoding="utf-8"))
    else:
        value = explicit
    if not isinstance(value, list) or not value:
        raise ValueError("authoritative reviewer input must be a non-empty list")
    out: list[dict[str, str]] = []
    for candidate in value:
        if not isinstance(candidate, dict):
            raise ValueError("reviewer identity must be an object")
        if candidate.get("schema") != _REVIEWER_SCHEMA:
            raise ValueError("reviewer identity schema is invalid")
        name, seat, fingerprint = (candidate.get(k) for k in ("name", "seat", "fingerprint"))
        identity, host, session, workspace = (
            candidate.get(k) for k in ("identity", "host", "session", "workspace")
        )
        if not all(
            isinstance(v, str) and v.strip()
            for v in (name, seat, fingerprint, identity, host, session, workspace)
        ):
            raise ValueError("reviewer identity fields are required")
        if (
            seat.strip().lower() != "seraph"
            or name.strip().lower() != "seraph"
            or identity.strip().lower().startswith("jarvis")
            or not _SHA256.fullmatch(fingerprint.strip())
        ):
            raise ValueError("reviewer identity is malformed")
        eligible = candidate.get("eligible", True)
        if eligible is not True:
            raise ValueError("reviewer identity eligibility is malformed")
        out.append(
            {
                "schema": _REVIEWER_SCHEMA,
                "name": name.strip(),
                "seat": seat.strip().lower(),
                "identity": identity.strip(),
                "host": host.strip(),
                "session": session.strip(),
                "workspace": workspace.strip(),
                "fingerprint": fingerprint.strip(),
                "eligible": eligible,
            }
        )
    return out


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _events(home: Path, card_id: str) -> list[dict[str, Any]]:
    root = home / "cards" / card_id / "events"
    out = []
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)  # fail closed on malformed CardStore evidence
            if not isinstance(item, dict):
                raise ValueError(f"non-object CardStore event: {path}")
            out.append(item)
    return out


def _revision(home: Path, card_id: str, card: dict[str, Any] | None = None) -> str | None:
    events = _events(home, card_id)
    if card is not None:
        stable = {
            "id": card.get("id"),
            "status": card.get("status"),
            "owner": card.get("owner"),
            "labels": sorted(card.get("labels") or []),
            "dependencies": sorted(card.get("dependencies") or []),
            "verdict": str(
                (card.get("links") or {}).get("verdict")
                or (card.get("links") or {}).get("outcome")
                or ""
            )
            .strip()
            .upper(),
        }
        return hashlib.sha256(_json(stable).encode()).hexdigest()
    if not events:
        return None
    # The event identity is the exact immutable revision, not lifecycle status.
    event = events[-1]
    return str(
        event.get("event_id")
        or event.get("claim_revision")
        or hashlib.sha256(_json(event).encode()).hexdigest()
    )


def _pr_number(text: str) -> int | None:
    m = PR_RE.search(text)
    return int(m.group(1)) if m else None


def _review_is_bound_to_pr(
    card: dict[str, Any], repository: str, number: int, head_revision: object
) -> bool:
    """Return true only for a review explicitly bound to this PR and head."""
    links = card.get("links") if isinstance(card.get("links"), dict) else {}
    pr_link = str(links.get("pr") or "").strip().rstrip("/")
    commit = str(links.get("commit") or links.get("head_commit") or "").strip()
    head = str(head_revision or "").strip()
    pr_matches = pr_link in {str(number), f"{repository}#{number}"} or pr_link.endswith(
        f"/{number}"
    )
    return bool(pr_matches and head and commit == head)


def _review_has_pr_binding(card: dict[str, Any]) -> bool:
    links = card.get("links") if isinstance(card.get("links"), dict) else {}
    return bool(str(links.get("pr") or "").strip() or str(links.get("commit") or "").strip())


def _source_link_matches(
    card: dict[str, Any], repository: str, number: int, head: object
) -> bool:
    """Return true only for an exact folded source-card PR and head link."""
    links = card.get("links")
    if not isinstance(links, dict):
        return False
    pr_link = links.get("pr")
    commit = links.get("commit")
    if not isinstance(pr_link, str) or not isinstance(commit, str):
        return False
    pr_match = re.fullmatch(
        r"https?://[^/\s]+/(?P<repository>[^/\s]+/[^/\s]+)/pull/(?P<number>\d+)/?",
        pr_link.strip(),
        re.IGNORECASE,
    )
    if pr_match is None:
        return False
    observed_head = str(head or "")
    return (
        pr_match.group("repository") == repository
        and int(pr_match.group("number")) == number
        and bool(_SHA1.fullmatch(commit))
        and bool(_SHA1.fullmatch(observed_head))
        and commit == observed_head
    )


def _mapping_hash(
    repository: str,
    number: int,
    head: object,
    base: object,
    source: dict[str, Any],
    review: dict[str, Any],
    source_generation: str,
    review_revision: str,
    review_verdict: str,
) -> str:
    value = {
        "repository": repository,
        "number": number,
        "head_revision": str(head or ""),
        "base_revision": str(base or ""),
        "source_card": source["id"],
        "card_generation": source_generation,
        "review_card_id": review["id"],
        "review_card_revision": review_revision,
        "review_verdict": review_verdict,
    }
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _exclusion_time(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("exclusion expiry is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("exclusion expiry is malformed") from exc
    if parsed.tzinfo is None:
        raise ValueError("exclusion expiry must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= now:
        raise ValueError("exclusion expiry is not in the future")
    return parsed


def _validate_exclusions(
    value: object, open_prs: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Validate exact, expiring exclusion records before reconciliation."""
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("schema") != _EXCLUSION_SCHEMA:
        raise ValueError("exclusions must use the structured exclusion schema")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError("exclusion records must be a list")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("exclusion validation time must include a timezone")
    open_by_key: dict[str, dict[str, Any]] = {}
    for pr in open_prs:
        repository = str(pr.get("repository") or "").strip()
        number = pr.get("number")
        if not repository or isinstance(number, bool) or not isinstance(number, int):
            raise ValueError("open PR identity is malformed")
        key = f"{repository}#{number}"
        if key in open_by_key:
            raise ValueError("duplicate open PR")
        open_by_key[key] = pr
    required = {
        "repository",
        "pr",
        "head_sha",
        "base_sha",
        "owner",
        "reason",
        "classification",
        "source_evidence_sha256",
        "expires_at",
    }
    validated: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("exclusion record is malformed")
        repository = record["repository"]
        number = record["pr"]
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("exclusion repository is malformed")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("exclusion PR is malformed")
        key = f"{repository.strip()}#{number}"
        if key in validated:
            raise ValueError("duplicate exclusion PR")
        pr = open_by_key.get(key)
        if pr is None:
            if any(candidate.get("number") == number for candidate in open_prs):
                raise ValueError("exclusion repository does not match the observed PR")
            raise ValueError("exclusion PR is unknown")
        head_sha = record["head_sha"]
        base_sha = record["base_sha"]
        if not isinstance(head_sha, str) or not _SHA1.fullmatch(head_sha):
            raise ValueError("exclusion head SHA is malformed")
        if not isinstance(base_sha, str) or not _SHA1.fullmatch(base_sha):
            raise ValueError("exclusion base SHA is malformed")
        if head_sha != str(pr.get("headRefOid") or ""):
            raise ValueError("exclusion head SHA does not match the observed PR")
        if base_sha != str(pr.get("baseRefOid") or ""):
            raise ValueError("exclusion base SHA does not match the observed PR")
        owner = record["owner"]
        reason = record["reason"]
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("exclusion owner is malformed")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("exclusion reason is malformed")
        classification = record["classification"]
        if (
            not isinstance(classification, str)
            or classification not in _EXCLUSION_CLASSIFICATIONS
        ):
            raise ValueError("exclusion classification is unknown")
        evidence = record["source_evidence_sha256"]
        if not isinstance(evidence, str) or not _SHA256.fullmatch(evidence):
            raise ValueError("exclusion source evidence hash is malformed")
        _exclusion_time(record["expires_at"], current)
        validated[key] = {
            "repository": repository.strip(),
            "pr": number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "owner": owner.strip(),
            "reason": reason.strip(),
            "classification": classification,
            "source_evidence_sha256": evidence,
            "expires_at": str(record["expires_at"]).strip(),
        }
    return validated


def reconcile(
    open_prs: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    home: Path,
    exclusions: dict[str, Any] | None = None,
    reviewer_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    exclusions = _validate_exclusions(exclusions, open_prs)
    candidates_from_authority = _reviewer_candidates(home, reviewer_candidates)
    cards_by_id = {str(card.get("id")): card for card in cards if str(card.get("id") or "")}
    by_pr: dict[int, list[dict[str, Any]]] = {}
    for card in cards:
        text = " ".join(
            str(card.get(k, "")) for k in ("title", "description", "acceptance_criteria")
        )
        n = _pr_number(text)
        if n is not None:
            by_pr.setdefault(n, []).append(card)
    records: dict[str, dict[str, Any]] = {}
    diagnostics = []
    for pr in sorted(open_prs, key=lambda x: (str(x.get("repository", "")), int(x["number"]))):
        repository = str(pr.get("repository") or "")
        if not repository:
            raise ValueError("open PR missing repository")
        number = int(pr["number"])
        exclusion = exclusions.get(f"{repository}#{number}")
        if exclusion is not None:
            diagnostics.append(
                {
                    "pr": number,
                    "repository": repository,
                    "classification": "excluded",
                    "exclusion_classification": exclusion["classification"],
                    "reason": exclusion["reason"],
                    "owner": exclusion["owner"],
                    "source_evidence_sha256": exclusion["source_evidence_sha256"],
                    "expires_at": exclusion["expires_at"],
                }
            )
            continue
        pr_text = " ".join(str(pr.get(k, "")) for k in ("title", "body"))
        referenced = [
            cards_by_id[card_id] for card_id in CARD_RE.findall(pr_text) if card_id in cards_by_id
        ]
        candidates = []
        for candidate in by_pr.get(number, []) + referenced:
            if candidate not in candidates:
                candidates.append(candidate)

        def labels(card: dict[str, Any]) -> set[str]:
            return {str(label).lower() for label in (card.get("labels") or [])}

        def is_review(card: dict[str, Any]) -> bool:
            title = str(card.get("title") or "").upper()
            card_labels = labels(card)
            return (
                "review" in card_labels
                or "[REVIEW]" in title
                or any(label.endswith("-review") for label in card_labels)
            )

        linked_sources = [
            card
            for card in cards
            if not is_review(card)
            and _source_link_matches(card, repository, number, pr.get("headRefOid"))
        ]
        for candidate in linked_sources:
            if candidate not in candidates:
                candidates.append(candidate)

        def review_terminal(card: dict[str, Any]) -> bool:
            status = str(card.get("status") or "").lower().split(".")[-1]
            links = card.get("links") if isinstance(card.get("links"), dict) else {}
            verdict = str(links.get("verdict") or links.get("outcome") or "").strip().upper()
            return status == "done" and verdict in _TERMINAL_REVIEW

        sources = [c for c in candidates if not is_review(c)]
        reviews = [
            c
            for c in cards
            if is_review(c) and len(sources) == 1 and f"parent-{sources[0]['id']}" in labels(c)
        ]
        terminal_reviews = [review for review in reviews if review_terminal(review)]
        bound_terminal_reviews = [
            review
            for review in terminal_reviews
            if _review_is_bound_to_pr(review, repository, number, pr.get("headRefOid"))
        ]
        eligible_terminal_reviews = [
            review
            for review in terminal_reviews
            if not _review_has_pr_binding(review) or review in bound_terminal_reviews
        ]
        selected_reviews = (
            bound_terminal_reviews
            if len(bound_terminal_reviews) == 1
            else eligible_terminal_reviews
        )
        if len(sources) != 1:
            classification = "unresolved"
        elif len(selected_reviews) != 1:
            classification = "unresolved"
        else:
            source, review = sources[0], selected_reviews[0]
            source_generation = _revision(home, source["id"], source)
            review_revision = _revision(home, review["id"], review)
            review_links = review.get("links") if isinstance(review.get("links"), dict) else {}
            review_verdict = (
                str(review_links.get("verdict") or review_links.get("outcome") or "")
                .strip()
                .upper()
            )
            if source_generation and review_revision:
                records[f"{repository}#{number}"] = {
                    "source_card": source["id"],
                    "card_generation": source_generation,
                    "review_card_id": review["id"],
                    "review_card_revision": review_revision,
                    "review_verdict": review_verdict,
                    "head_revision": str(pr.get("headRefOid") or ""),
                    "base_revision": str(pr.get("baseRefOid") or ""),
                    "mapping_evidence_sha256": _mapping_hash(
                        repository,
                        number,
                        pr.get("headRefOid"),
                        pr.get("baseRefOid"),
                        source,
                        review,
                        source_generation,
                        review_revision,
                        review_verdict,
                    ),
                }
                continue
        diagnostics.append(
            {
                "pr": number,
                "classification": classification,
                "candidate_source_cards": [c["id"] for c in sources],
                "candidate_review_cards": [c["id"] for c in reviews],
                "terminal_review_cards": [c["id"] for c in terminal_reviews],
                "eligible_terminal_review_cards": [c["id"] for c in eligible_terminal_reviews],
                "head_bound_review_cards": [c["id"] for c in bound_terminal_reviews],
            }
        )
    counts = {
        k: sum(r["classification"] == k for r in diagnostics) for k in ("excluded", "unresolved")
    }
    counts["lineage-complete"] = len(records)
    ordered_prs = sorted(open_prs, key=lambda x: (str(x.get("repository", "")), int(x["number"])))
    body = {
        "schema": "skfleet.link-lineage/v1",
        "source_revision": hashlib.sha256(_json(ordered_prs).encode()).hexdigest(),
        "coverage": counts,
        "unresolved_prs": sorted(
            r["pr"] for r in diagnostics if r["classification"] == "unresolved"
        ),
        "records": records,
        "reviewer_candidates": candidates_from_authority,
        "diagnostics": diagnostics,
    }
    body["evidence_hash"] = hashlib.sha256(_json(body).encode()).hexdigest()
    return body


def fetch_prs(repo: str) -> list[dict[str, Any]]:
    cmd = ["gh", "api", "--paginate", f"repos/{repo}/pulls?state=open&per_page=100", "--jq", ".[]"]
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        item = json.loads(line)
        rows.append(
            {
                "number": item["number"],
                "title": item.get("title", ""),
                "body": item.get("body", ""),
                "headRefOid": (item.get("head") or {}).get("sha"),
                "baseRefOid": (item.get("base") or {}).get("sha"),
                "baseRefName": (item.get("base") or {}).get("ref"),
                "repository": repo,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="smilinTux/skcapstone")
    ap.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--exclude", type=Path)
    args = ap.parse_args()
    from skcoord.card_store import CardStore

    cards = [
        c.model_dump(mode="json") for c in CardStore(args.home).list_cards(include_archived=True)
    ]
    exclusions = json.loads(args.exclude.read_text()) if args.exclude else None
    report = reconcile(fetch_prs(args.repo), cards, args.home, exclusions)
    rendered = _json(report) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
