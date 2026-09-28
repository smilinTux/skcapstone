#!/usr/bin/env python3
"""Read-only reconciliation of open Link PRs to CardStore card lineage.

This is deliberately a dry-run tool: it never writes CardStore or the Link
observation feed.  CardStore is read through its projection and event files are
parsed as JSON, so malformed evidence cannot silently become lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from skcapstone.forgejo import SKGIT_REPOSITORY, MultiForgeReadOnlyConnector
from skcapstone.link_review_work import card_generation

PR_RE = re.compile(r"\bPR\s*#?\s*(\d+)\b", re.I)
CARD_RE = re.compile(r"\b([0-9a-f]{8})\b", re.I)
_REVIEWER_SCHEMA = "skfleet.reviewer-identity/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TERMINAL_REVIEW = frozenset({"PASS", "FAIL", "BLOCKED"})
_TERMINAL_REVIEW_PREFIX = re.compile(r"^(PASS|FAIL|BLOCKED)(?:$|[\s|:])")
_MAX_REVIEW_WORK = 50


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
        return card_generation(card)
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


def _terminal_review_verdict(value: object) -> str | None:
    """Return the typed terminal token while retaining explanatory text."""
    match = _TERMINAL_REVIEW_PREFIX.match(str(value or "").strip().upper())
    return match.group(1) if match else None


def _review_is_bound_to_pr(
    card: dict[str, Any], repository: str, number: int, head_revision: object
) -> bool:
    """Return true only for a review explicitly bound to this PR and head."""
    links = card.get("links") if isinstance(card.get("links"), dict) else {}
    pr_link = str(links.get("pr") or "").strip().rstrip("/")
    head = str(head_revision or "").strip()
    url = (
        f"{repository}/pulls/{number}"
        if repository == SKGIT_REPOSITORY
        else f"https://github.com/{repository}/pull/{number}"
    )
    pr_matches = pr_link in {
        f"{repository}#{number}",
        url,
    }
    return bool(
        pr_matches
        and head
        and _card_heads(card) == {head}
        and _card_repositories(card) == {repository}
    )


def _card_heads(card: dict[str, Any]) -> set[str]:
    """Collect explicit source or review pins without choosing between conflicts."""
    links = card.get("links") if isinstance(card.get("links"), dict) else {}
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    pins = [links.get(k) for k in ("commit", "head_commit", "head")]
    pins.append(meta.get("link_head_revision"))
    return {str(pin).strip() for pin in pins if pin}


def _card_repositories(card: dict[str, Any]) -> set[str]:
    """Read explicit forge scope without inferring it from a bare PR number."""
    repositories = set()
    for name in ("links", "meta"):
        values = card.get(name) if isinstance(card.get(name), dict) else {}
        repository = str(values.get("repository") or "").strip().rstrip("/")
        if repository:
            repositories.add(repository.removesuffix(".git").removeprefix("https://github.com/"))
        pr = str(values.get("pr") or "").strip().rstrip("/")
        if "/pull/" in pr or "/pulls/" in pr:
            repositories.add(re.split(r"/pulls?/", pr)[0].removeprefix("https://github.com/"))
        elif "#" in pr:
            repositories.add(pr.rsplit("#", 1)[0])
    return repositories


def _card_pr_numbers(card: dict[str, Any]) -> set[int]:
    """Read explicit PR links before falling back to legacy title discovery."""
    numbers = set()
    for name in ("links", "meta"):
        values = card.get(name) if isinstance(card.get(name), dict) else {}
        match = re.search(r"(?:/pulls?/|#)(\d+)/?$", str(values.get("pr") or ""))
        if match:
            numbers.add(int(match.group(1)))
    return numbers


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


def reconcile(
    open_prs: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    home: Path,
    exclusions: dict[str, str] | None = None,
    reviewer_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    exclusions = exclusions or {}
    candidates_from_authority = _reviewer_candidates(home, reviewer_candidates)
    cards_by_id = {str(card.get("id")): card for card in cards if str(card.get("id") or "")}
    by_pr: dict[int, list[dict[str, Any]]] = {}
    for card in cards:
        text = " ".join(
            str(card.get(k, "")) for k in ("title", "description", "acceptance_criteria")
        )
        numbers = _card_pr_numbers(card)
        n = _pr_number(text)
        if not numbers and n is not None:
            numbers.add(n)
        for n in numbers:
            by_pr.setdefault(n, []).append(card)
    records: dict[str, dict[str, Any]] = {}
    diagnostics = []
    review_work = []
    for pr in sorted(open_prs, key=lambda x: (str(x.get("repository", "")), int(x["number"]))):
        repository = str(pr.get("repository") or "")
        if not repository:
            raise ValueError("open PR missing repository")
        number = int(pr["number"])
        key = f"{repository}#{number}"
        unique_number = sum(int(item["number"]) == number for item in open_prs) == 1
        exclusion = exclusions.get(key)
        if exclusion is None and unique_number and repository != SKGIT_REPOSITORY:
            exclusion = exclusions.get(str(number))
        if exclusion is not None:
            diagnostics.append(
                {
                    "repository": repository,
                    "pr": number,
                    "classification": "excluded",
                    "reason": exclusion,
                }
            )
            continue
        pr_text = " ".join(str(pr.get(k, "")) for k in ("title", "body"))
        referenced = [
            cards_by_id[card_id] for card_id in CARD_RE.findall(pr_text) if card_id in cards_by_id
        ]
        candidates = []
        for candidate in by_pr.get(number, []) + referenced:
            scopes = _card_repositories(candidate)
            if scopes and scopes != {repository}:
                continue
            numbers = _card_pr_numbers(candidate)
            if numbers and numbers != {number}:
                continue
            heads = _card_heads(candidate)
            if heads and heads != {str(pr.get("headRefOid") or "").strip()}:
                continue
            if (
                not scopes
                and candidate not in referenced
                and (not unique_number or repository == SKGIT_REPOSITORY)
            ):
                continue
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

        def review_terminal(card: dict[str, Any]) -> bool:
            status = str(card.get("status") or "").lower().split(".")[-1]
            links = card.get("links") if isinstance(card.get("links"), dict) else {}
            verdict = _terminal_review_verdict(links.get("verdict") or links.get("outcome") or "")
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
        if len(sources) != 1:
            classification = "unresolved"
        elif len(bound_terminal_reviews) != 1:
            classification = "unresolved"
        else:
            source, review = sources[0], bound_terminal_reviews[0]
            source_generation = _revision(home, source["id"], source)
            review_revision = _revision(home, review["id"], review)
            review_links = review.get("links") if isinstance(review.get("links"), dict) else {}
            review_verdict = _terminal_review_verdict(
                review_links.get("verdict") or review_links.get("outcome") or ""
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
                "repository": repository,
                "pr": number,
                "head_revision": str(pr.get("headRefOid") or ""),
                "base_revision": str(pr.get("baseRefOid") or ""),
                "classification": classification,
                "candidate_source_cards": [c["id"] for c in sources],
                "candidate_review_cards": [c["id"] for c in reviews],
                "terminal_review_cards": [c["id"] for c in terminal_reviews],
                "head_bound_review_cards": [c["id"] for c in bound_terminal_reviews],
            }
        )
        if len(sources) == 1 and (
            not terminal_reviews or (terminal_reviews and not bound_terminal_reviews)
        ):
            source = sources[0]
            source_owner = str(
                source.get("originator") or source.get("owner") or source.get("created_by") or ""
            ).strip()
            eligible_reviewers = [
                reviewer
                for reviewer in candidates_from_authority
                if reviewer["identity"] != source_owner and reviewer["name"] != source_owner
            ]
            source_generation = _revision(home, source["id"], source)
            if source_generation and eligible_reviewers and len(review_work) < _MAX_REVIEW_WORK:
                source_links = source.get("links") if isinstance(source.get("links"), dict) else {}
                recommendation = {
                    "kind": "review-work",
                    "reason": (
                        "missing_terminal_review"
                        if not terminal_reviews
                        else "review_not_bound_to_head"
                    ),
                    "repository": repository,
                    "pr": number,
                    "head_revision": str(pr.get("headRefOid") or ""),
                    "base_revision": str(pr.get("baseRefOid") or ""),
                    "source_card": source["id"],
                    "card_generation": source_generation,
                    "source_owner": source_owner,
                    "reviewer_candidates": eligible_reviewers,
                }
                if "repository" in source_links or "base_ref" in source_links:
                    recommendation.update(
                        {
                            "workspace_repository": source_links.get("repository"),
                            "base_ref": source_links.get("base_ref"),
                        }
                    )
                review_work.append(recommendation)
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
        "review_work_recommendations": review_work,
        "reviewer_candidates": candidates_from_authority,
        "diagnostics": diagnostics,
    }
    body["evidence_hash"] = hashlib.sha256(_json(body).encode()).hexdigest()
    return body


def fetch_prs(repo: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in MultiForgeReadOnlyConnector().list_open(repo):
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
    ap.add_argument("--repo", action="append")
    ap.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--exclude", type=Path)
    args = ap.parse_args()
    from skcoord.card_store import CardStore

    cards = [
        c.model_dump(mode="json") for c in CardStore(args.home).list_cards(include_archived=True)
    ]
    exclusions = json.loads(args.exclude.read_text()) if args.exclude else {}
    repositories = args.repo or ["smilinTux/skcapstone"]
    pull_requests = [row for repo in repositories for row in fetch_prs(repo)]
    report = reconcile(pull_requests, cards, args.home, exclusions)
    rendered = _json(report) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
