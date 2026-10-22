"""Separate read-only identity attestation from repository-scoped approval."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .forgejo import SKGIT_ORIGIN, SKGIT_REPOSITORY, ForgejoClient, _pages
from .seraph_review_contracts import (
    BranchProtection,
    ConnectorCapabilities,
    ReviewPublicationError,
)

_LOGIN = "seraph-review-bot"
_REPOSITORY = "smilinTux/sklegal"
_PROTECTION_PATH = "/api/v1/repos/smilinTux/sklegal/branch_protections/main"
_PROTECTION_FILE_KEYS = frozenset({"GITHUB_USER", "GITHUB_TOKEN", "GH_TOKEN", "GH_URL"})
_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class ProvisionedToken:
    """Owner-only administrator ceremony metadata, never a caller assertion."""

    token: str = field(repr=False)
    token_sha256: str
    token_id: int
    token_name: str
    scopes: list[str]
    repositories: list[str]
    account_id: int
    account_login: str


@dataclass(frozen=True)
class ReviewerCredentials:
    """Two tokens issued to the same dedicated independent account."""

    identity: ProvisionedToken
    writer: ProvisionedToken

    def validate(self) -> None:
        """Reject changed, swapped, or broader provisioning bindings."""
        for token, name, scopes, repositories in (
            (self.identity, "sklegal-seraph-identity", ["read:organization", "read:user"], []),
            (self.writer, "sklegal-seraph-review-publisher", ["write:repository"], [_REPOSITORY]),
        ):
            if (
                not isinstance(token.token, str)
                or not token.token
                or any(character.isspace() for character in token.token)
                or not isinstance(token.token_sha256, str)
                or not hmac.compare_digest(
                    hashlib.sha256(token.token.encode()).hexdigest(), token.token_sha256
                )
                or type(token.token_id) is not int
                or token.token_id < 1
                or type(token.account_id) is not int
                or token.account_id < 1
                or token.account_login != _LOGIN
                or token.token_name != name
                or token.scopes != scopes
                or token.repositories != repositories
            ):
                raise ReviewPublicationError("forge_credential_binding_invalid")
        if (
            self.identity.account_id != self.writer.account_id
            or self.identity.token_id == self.writer.token_id
            or self.identity.token_sha256 == self.writer.token_sha256
        ):
            raise ReviewPublicationError("forge_credential_account_binding_invalid")


def _unique_object(pairs):
    """Reject duplicate JSON keys instead of silently selecting a binding."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewPublicationError("forge_credential_file_invalid")
        result[key] = value
    return result


def read_credentials(path: Path) -> ReviewerCredentials:
    """Load exact version-two metadata from a regular owner-only file."""
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ReviewPublicationError("forge_credential_file_invalid")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ReviewPublicationError("forge_credential_file_permissions_invalid")
    try:
        value = json.loads(path.read_text(), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"version", "identity", "writer"}:
            raise ValueError
        if type(value["version"]) is not int or value["version"] != 2:
            raise ValueError
        credentials = ReviewerCredentials(
            ProvisionedToken(**value["identity"]), ProvisionedToken(**value["writer"])
        )
        credentials.validate()
        return credentials
    except (TypeError, ValueError, UnicodeError):
        raise ReviewPublicationError("forge_credential_file_invalid") from None


def read_protection_token(path: Path) -> str:
    """Read one effective token from the existing non-shell operator file."""
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise ReviewPublicationError("forge_protection_credential_file_invalid")
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ReviewPublicationError("forge_protection_credential_file_permissions_invalid")
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = raw.partition("=")
            if (
                not separator
                or key != key.strip()
                or value != value.strip()
                or key not in _PROTECTION_FILE_KEYS
                or key in values
                or not value
            ):
                raise ReviewPublicationError("forge_protection_credential_file_invalid")
            values[key] = value
    except UnicodeError:
        raise ReviewPublicationError("forge_protection_credential_file_invalid") from None
    token = values.get("GH_TOKEN")
    if (
        set(values) != _PROTECTION_FILE_KEYS
        or token != values.get("GITHUB_TOKEN")
        or not isinstance(token, str)
        or not _TOKEN.fullmatch(token)
        or values["GH_URL"] != SKGIT_ORIGIN
    ):
        raise ReviewPublicationError("forge_protection_credential_file_invalid")
    return token


class IdentityReadClient(ForgejoClient):
    """Confine the attestation credential to identity and team GETs."""

    def request(self, method: str, path: str, payload: dict | None = None):
        """Reject repository access and writes before the HTTP transport."""
        if (
            method != "GET"
            or payload is not None
            or urlsplit(path).path not in {"/api/v1/user", "/api/v1/user/teams"}
        ):
            raise ReviewPublicationError("forge_identity_request_not_authorized")
        return super().request(method, path)


class ProtectionReadClient(ForgejoClient):
    """Confine the operator credential to one exact branch-policy read."""

    def request(self, method: str, path: str, payload: dict | None = None):
        """Reject every request except the payload-free exact policy GET."""
        if method != "GET" or path != _PROTECTION_PATH or payload is not None:
            raise ReviewPublicationError("forge_protection_request_not_authorized")
        return super().request(method, path)

    def read(self, repository: str, base_branch: str) -> BranchProtection:
        """Use the existing policy parser while retaining transport confinement."""
        from .seraph_forgejo import ForgejoReviewConnector

        return ForgejoReviewConnector(self).read(repository, base_branch)


def attest_credentials(
    identity_client: IdentityReadClient,
    writer_client: ForgejoClient,
    credentials: ReviewerCredentials,
    repository: str,
) -> ConnectorCapabilities:
    """Join live identity/team facts to the sealed repository writer binding."""
    if repository != SKGIT_REPOSITORY:
        raise ReviewPublicationError("forge_repository_not_authorized")
    credentials.validate()
    user = identity_client.request("GET", "/api/v1/user")
    teams = _pages(identity_client, "/api/v1/user/teams")
    accessible = writer_client.request("GET", "/api/v1/repos/" + _REPOSITORY)
    if (
        not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user.get("id") != credentials.identity.account_id
        or user.get("login") != _LOGIN
        or user.get("is_admin") is not False
        or not isinstance(accessible, dict)
        or accessible.get("full_name") != _REPOSITORY
        or accessible.get("private") is not True
    ):
        raise ReviewPublicationError("forge_credential_identity_invalid")
    if len(teams) != 1:
        raise ReviewPublicationError("forge_credential_team_invalid")
    team = teams[0]
    units = team.get("units_map")
    if (
        team.get("name") != "sklegal-seraph-reviewers"
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
        _LOGIN,
        frozenset(credentials.identity.scopes + credentials.writer.scopes),
        "forgejo",
        frozenset({SKGIT_REPOSITORY}),
    )
