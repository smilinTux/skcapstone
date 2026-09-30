"""Idempotent draft publication of operator-qualified immutable candidates.

The caller owns source-only inspection, exact card/review/evidence validation,
and authority. This module does not infer those from worker prose, push Git,
transfer claims, complete cards, publish review approvals, merge or deploy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .forgejo import ForgejoError

_ROOT = "/api/v1/repos/smilinTux/sklegal"
_REPO = "smilinTux/sklegal"
_FAMILIES = {"deepseek", "zai", "codex"}


@dataclass(frozen=True)
class WipCandidate:
    """A source head already qualified outside the model credential boundary."""

    source_card: str
    review_card: str
    head: str
    base: str
    producer_family: str
    reviewer_family: str
    review_evidence_sha256: str
    title: str

    @property
    def branch(self) -> str:
        """Use the full candidate hash so a changed candidate gets a new branch."""
        return f"wip-{self.source_card}-{self.head}"

    def payload(self) -> dict[str, str]:
        """Validate exact identities and generate stable, credential-free lineage."""
        pins = (
            (self.source_card, 8),
            (self.review_card, 8),
            (self.head, 40),
            (self.base, 40),
            (self.review_evidence_sha256, 64),
        )
        if any(not isinstance(v, str) or not re.fullmatch(f"[0-9a-f]{{{n}}}", v) for v, n in pins):
            raise ForgejoError("wip_candidate_pin_invalid")
        if (
            self.source_card == self.review_card
            or self.producer_family not in _FAMILIES
            or self.reviewer_family not in _FAMILIES
            or self.producer_family == self.reviewer_family
            or not isinstance(self.title, str)
            or not self.title.strip()
            or len(self.title) > 220
            or any(ord(c) < 32 or ord(c) == 127 for c in self.title)
        ):
            raise ForgejoError("wip_candidate_identity_invalid")
        return {
            "title": f"WIP: [{self.source_card}] {self.title}",
            "head": self.branch,
            "base": "main",
            "body": (
                "Source-only candidate for CI. No automatic merge or deployment.\n\n"
                f"Source card: `{self.source_card}`\n"
                f"Independent review card: `{self.review_card}`\n"
                f"Candidate: `{self.head}`\n"
                f"Qualified target base: `{self.base}`\n"
                f"Provider families: `{self.producer_family}` / `{self.reviewer_family}`\n"
                f"Review evidence SHA256: `{self.review_evidence_sha256}`\n\n"
                "Independent source review is not hosted CI or merge approval.\n"
            ),
        }


def _check_pr(raw: Any, candidate: WipCandidate) -> dict[str, Any]:
    """Refuse drift or conflicting publication instead of mutating an existing PR."""
    payload = candidate.payload()
    if not isinstance(raw, dict):
        raise ForgejoError("wip_pr_malformed")
    head, base = raw.get("head"), raw.get("base")
    if (
        type(raw.get("number")) is not int
        or raw["number"] < 1
        or raw.get("state") != "open"
        or raw.get("merged") is not False
        or raw.get("title") != payload["title"]
        or raw.get("body") != payload["body"]
        or not isinstance(head, dict)
        or not isinstance(base, dict)
        or head.get("ref") != candidate.branch
        or head.get("sha") != candidate.head
        or not isinstance(head.get("repo"), dict)
        or head["repo"].get("full_name") != _REPO
        or base.get("ref") != "main"
        or base.get("sha") != candidate.base
    ):
        raise ForgejoError("wip_pr_binding_conflict")
    return raw


def publish_wip(client: Any, candidate: WipCandidate) -> dict[str, Any]:
    """Reconcile before creation and read back after; never retry a POST in place.

    Caller serializes publication using the existing authority lock. Following
    an uncertain POST result, a fresh invocation reads the forge first. Existing
    mismatched publications are parked, not retitled, rebased or overwritten.
    """
    payload = candidate.payload()
    for branch, expected in (("main", candidate.base), (candidate.branch, candidate.head)):
        raw = client.request("GET", f"{_ROOT}/branches/{branch}")
        if not isinstance(raw, dict) or raw.get("commit", {}).get("id") != expected:
            raise ForgejoError("wip_branch_drift")
    rows = client.pages(_ROOT + "/pulls?state=all")
    matches = [
        p
        for p in rows
        if isinstance(p.get("head"), dict) and p["head"].get("ref") == candidate.branch
    ]
    if len(matches) > 1:
        raise ForgejoError("wip_duplicate_publications")
    raw = matches[0] if matches else client.request("POST", _ROOT + "/pulls", payload)
    verified = _check_pr(raw, candidate)
    current = client.request("GET", f"{_ROOT}/pulls/{verified['number']}")
    current = _check_pr(current, candidate)
    if current["number"] != verified["number"]:
        raise ForgejoError("wip_pr_identity_changed")
    return {
        "number": current["number"],
        "head": candidate.head,
        "base": candidate.base,
        "title": current["title"],
        "created": not matches,
        "url": f"https://skgit.skstack01.douno.it/{_REPO}/pulls/{current['number']}",
    }


def observe_ci(client: Any, candidate: WipCandidate) -> dict[str, Any]:
    """Observe latest statuses at an exact head, not required-check acceptance."""
    candidate.payload()
    rows = client.pages(f"{_ROOT}/commits/{candidate.head}/statuses")
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        context, identity, state = (row.get(k) for k in ("context", "id", "status"))
        if (
            not isinstance(context, str)
            or not context
            or type(identity) is not int
            or identity < 1
            or state not in {"pending", "success", "failure", "error"}
        ):
            raise ForgejoError("wip_ci_malformed")
        prior = latest.get(context)
        if prior and prior["id"] == identity and prior["status"] != state:
            raise ForgejoError("wip_ci_conflict")
        if not prior or identity > prior["id"]:
            latest[context] = {"id": identity, "status": state}
    states = {row["status"] for row in latest.values()}
    state = (
        "unknown"
        if not states
        else (
            "failure"
            if states & {"failure", "error"}
            else "pending" if "pending" in states else "success"
        )
    )
    return {
        "head": candidate.head,
        "state": state,
        "checks": latest,
        "required_checks_accepted": False,
    }
