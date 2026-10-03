"""Exact-candidate Forgejo approval adapter with explicit authorization gates.

The transport credential is not CapAuth authority. Deployment must inject an
authoritative verifier and a credential-scope attestor. This module provides
no environment-based substitute for either decision.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .forgejo import (
    SKGIT_REPOSITORY,
    ForgejoClient,
    ForgejoReadOnlyConnector,
    _pages,
)
from .seraph_review_contracts import (
    _GIT_SHA,
    BranchProtection,
    ConnectorCapabilities,
    LivePullRequest,
    PublishedReview,
    ReviewPublicationError,
    SeraphPassEvidence,
    _digest,
)

_ROOT = "/api/v1/repos/smilinTux/sklegal"
_REPOSITORY_SLUG = "smilinTux/sklegal"
_SERVICE_IDENTITY = "seraph-review-bot"
_TOKEN_NAME = "sklegal-seraph-review-publisher"
_TEAM_NAME = "sklegal-seraph-reviewers"


@dataclass(frozen=True)
class ProvisionedCredential:
    """Authority metadata sealed beside the owner-only PAT at provisioning."""

    token: str
    token_sha256: str
    token_id: int
    token_name: str
    scopes: tuple[str, ...]
    repositories: tuple[str, ...]


def attest_private_credential(
    client: ForgejoClient,
    repository: str,
    binding: ProvisionedCredential,
) -> ConnectorCapabilities:
    """Derive exact service identity and repository scope from live Forgejo state."""
    _repository(repository)
    user = client.request("GET", "/api/v1/user")
    teams = _pages(client, "/api/v1/user/teams")
    accessible = client.request("GET", _ROOT)
    if (
        not isinstance(user, dict)
        or user.get("login") != _SERVICE_IDENTITY
        or user.get("is_admin") is not False
        or not isinstance(accessible, dict)
        or accessible.get("full_name") != _REPOSITORY_SLUG
        or accessible.get("private") is not True
    ):
        raise ReviewPublicationError("forge_credential_identity_invalid")
    if (
        binding.token_id < 1
        or binding.token_name != _TOKEN_NAME
        or binding.scopes != ("read:organization", "read:user", "write:repository")
        or binding.repositories != (_REPOSITORY_SLUG,)
        or not hmac.compare_digest(
            hashlib.sha256(binding.token.encode("utf-8")).hexdigest(),
            binding.token_sha256,
        )
    ):
        raise ReviewPublicationError("forge_credential_scope_invalid")
    if len(teams) != 1:
        raise ReviewPublicationError("forge_credential_team_invalid")
    team = teams[0]
    units = team.get("units_map")
    if (
        team.get("name") != _TEAM_NAME
        or team.get("includes_all_repositories") is not False
        or team.get("can_create_org_repo") is not False
        or not isinstance(units, dict)
        or units.get("repo.code") != "read"
        or units.get("repo.pulls") != "write"
        or any(
            value != "none"
            for key, value in units.items()
            if key not in {"repo.code", "repo.pulls"}
        )
    ):
        raise ReviewPublicationError("forge_credential_team_invalid")
    return ConnectorCapabilities(
        _SERVICE_IDENTITY,
        frozenset({"read:organization", "read:user", "write:repository"}),
        "forgejo",
        frozenset({SKGIT_REPOSITORY}),
    )


def _repository(repository: str) -> None:
    """Keep publication and policy reads on the approved private repository."""
    if repository != SKGIT_REPOSITORY:
        raise ReviewPublicationError("forge_repository_not_authorized")


class ForgejoReviewConnector:
    """Implement the existing review port through a confined Forgejo transport."""

    def __init__(
        self,
        client: ForgejoClient,
        *,
        attest_capabilities: Callable[[ForgejoClient, str], ConnectorCapabilities] | None = None,
    ) -> None:
        self.client = client
        self.attest_capabilities = attest_capabilities

    def capabilities(self) -> ConnectorCapabilities:
        """Require trusted attestation of current credential identity and scope."""
        if self.attest_capabilities is None:
            raise ReviewPublicationError("forge_credential_scope_attestation_unavailable")
        value = self.attest_capabilities(self.client, SKGIT_REPOSITORY)
        if not isinstance(value, ConnectorCapabilities) or value.forge != "forgejo":
            raise ReviewPublicationError("forge_credential_scope_attestation_invalid")
        value.validate(value.service_identity.lower())
        return value

    def read_pull_request(self, repository: str, number: int) -> LivePullRequest:
        """Reuse the read-only producer's exact-head and latest-status checks."""
        _repository(repository)
        rows = ForgejoReadOnlyConnector(self.client).list_open(repository)
        matches = [row for row in rows if row.get("number") == number]
        if len(matches) != 1:
            raise ReviewPublicationError("forge_open_pull_request_missing")
        row = matches[0]
        try:
            if row.get("state") != "open" or row.get("merged") is not False:
                raise ReviewPublicationError("forge_pull_request_not_open")
            branch = self.client.request("GET", _ROOT + "/branches/main")
            if row["base"]["sha"] != branch["commit"]["id"]:
                raise ReviewPublicationError("forge_base_changed")
            self._require_ancestor(row["base"]["sha"], row["head"]["sha"])
            reviews = _pages(self.client, f"{_ROOT}/pulls/{number}/reviews")
            latest = {}
            for review in reviews:
                login = review["user"]["login"]
                if not isinstance(login, str) or not login.strip():
                    raise ReviewPublicationError("forge_review_identity_malformed")
                user = login.lower()
                if type(review.get("id")) is not int or review["id"] < 1:
                    raise ReviewPublicationError("forge_review_id_malformed")
                if review.get("state") not in {"APPROVED", "REQUEST_CHANGES"}:
                    continue
                if (
                    user in latest
                    and review["id"] == latest[user]["id"]
                    and review != latest[user]
                ):
                    raise ReviewPublicationError("forge_review_conflict")
                if user not in latest or review["id"] > latest[user]["id"]:
                    latest[user] = review
            if any(
                review.get("state") == "REQUEST_CHANGES" and review.get("dismissed") is not True
                for review in latest.values()
            ):
                raise ReviewPublicationError("forge_unresolved_rejection")
            live = LivePullRequest(
                repository,
                number,
                row["head"]["sha"],
                row["base"]["ref"],
                row["user"]["login"],
                {check["context"]: check["status"] for check in row["checks"]},
            )
            live.validate()
            return live
        except (KeyError, TypeError) as exc:
            raise ReviewPublicationError("forge_pull_request_malformed") from exc

    def _require_ancestor(self, base: str, head: str) -> None:
        """Prove live main ancestry with bounded immutable Git commit reads."""
        pending, seen, deadline = [head], set(), time.monotonic() + 20
        while pending and len(seen) < 32 and time.monotonic() < deadline:
            commit = pending.pop(0)
            if commit == base:
                return
            if commit in seen:
                continue
            seen.add(commit)
            raw = self.client.request("GET", f"{_ROOT}/git/commits/{commit}")
            if not isinstance(raw, dict) or raw.get("sha") != commit:
                raise ReviewPublicationError("forge_ancestry_commit_mismatch")
            parents = raw.get("parents")
            if not isinstance(parents, list) or len(parents) > 32:
                raise ReviewPublicationError("forge_ancestry_parents_invalid")
            for parent in parents:
                sha = parent.get("sha") if isinstance(parent, dict) else None
                if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
                    raise ReviewPublicationError("forge_ancestry_parents_invalid")
                if sha not in seen and sha not in pending:
                    pending.append(sha)
        raise ReviewPublicationError("forge_current_base_ancestry_unproven")

    def read(self, repository: str, base_branch: str) -> BranchProtection:
        """Read live exact-main protection; never infer a wildcard policy."""
        _repository(repository)
        if base_branch != "main":
            raise ReviewPublicationError("forge_protected_branch_not_authorized")
        raw = self.client.request("GET", _ROOT + "/branch_protections/main")
        if (
            not isinstance(raw, dict)
            or raw.get("rule_name") != "main"
            or raw.get("enable_status_check") is not True
            or type(raw.get("required_approvals")) is not int
            or raw["required_approvals"] < 1
            or raw.get("apply_to_admins") is not True
            or raw.get("block_on_rejected_reviews") is not True
            or raw.get("block_on_outdated_branch") is not True
            or raw.get("dismiss_stale_approvals") is not True
            or raw.get("ignore_stale_approvals") is not False
            or raw.get("enable_push") is not False
        ):
            raise ReviewPublicationError("forge_branch_protection_invalid")
        contexts = raw.get("status_check_contexts")
        if (
            not isinstance(contexts, list)
            or not contexts
            or any(not isinstance(value, str) or not value.strip() for value in contexts)
        ):
            raise ReviewPublicationError("forge_required_checks_invalid")
        required = frozenset(contexts)
        payload = {
            "repository": repository,
            "base_branch": base_branch,
            "required_checks": sorted(required),
        }
        policy = BranchProtection(repository, base_branch, required, _digest(payload))
        policy.validate()
        return policy

    def find_approval(
        self,
        repository: str,
        number: int,
        *,
        service_identity: str,
        head_sha: str,
    ) -> PublishedReview | None:
        """Use the latest exact-head review; reject dismissal or a later rejection."""
        _repository(repository)
        latest = None
        for row in _pages(self.client, f"{_ROOT}/pulls/{number}/reviews"):
            user = row.get("user")
            if not isinstance(user, dict) or not isinstance(user.get("login"), str):
                raise ReviewPublicationError("forge_review_identity_malformed")
            if user["login"].lower() != service_identity.lower():
                continue
            if row.get("commit_id") != head_sha:
                continue
            if type(row.get("id")) is not int or row["id"] < 1:
                raise ReviewPublicationError("forge_review_id_malformed")
            if latest is not None and row["id"] == latest["id"] and row != latest:
                raise ReviewPublicationError("forge_review_conflict")
            if latest is None or row["id"] > latest["id"]:
                latest = row
        if latest is None:
            return None
        if (
            latest.get("state") != "APPROVED"
            or latest.get("dismissed") is not False
            or latest.get("stale") is not False
            or latest.get("official") is not True
        ):
            raise ReviewPublicationError("forge_review_not_approved")
        return PublishedReview(
            str(latest["id"]), repository, number, head_sha, service_identity.lower(), "APPROVE"
        )

    def create_review(
        self,
        repository: str,
        number: int,
        *,
        commit_sha: str,
        event: str,
        service_identity: str,
        idempotency_key: str,
    ) -> None:
        """Publish only APPROVED at the exact current head with receipt correlation."""
        _repository(repository)
        if event != "APPROVE":
            raise ReviewPublicationError("forge_review_event_not_authorized")
        self.capabilities().validate(service_identity.lower())
        if self.read_pull_request(repository, number).head_sha != commit_sha:
            raise ReviewPublicationError("stale_head")
        if (
            self.find_approval(
                repository, number, service_identity=service_identity, head_sha=commit_sha
            )
            is not None
        ):
            return
        self.client.request(
            "POST",
            f"{_ROOT}/pulls/{number}/reviews",
            {
                "event": "APPROVED",
                "commit_id": commit_sha,
                "body": f"Independent Seraph PASS. Publication receipt: {idempotency_key}",
            },
        )


def main(argv: list[str] | None = None) -> int:
    """Preflight actual card and evidence bindings without credentials or writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight", help="Validate a publication request locally")
    preflight.add_argument("--request", type=Path, required=True)
    preflight.add_argument("--home", type=Path, required=True)
    preflight.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args(argv)
    from .seraph_review_cardstore import LiveCardStoreGateway
    from .seraph_review_publisher import _validate_cards, _validate_evidence_artifact

    result = {
        "status": "blocked",
        "repository": SKGIT_REPOSITORY,
        "missing": [
            "authoritative_request_bound_capauth_verifier",
            "qualified_reviewer_credential_scope_attestor",
        ],
    }
    try:
        evidence = SeraphPassEvidence(**json.loads(args.request.read_text()))
        evidence.validate()
        _repository(evidence.repository)
        review = _validate_cards(evidence, LiveCardStoreGateway(args.home))
        _validate_evidence_artifact(evidence, review, args.evidence_root)
        result["local_evidence"] = "verified"
    except (OSError, TypeError, ValueError):
        result["local_evidence"] = "invalid_or_unavailable"
    print(json.dumps(result, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
