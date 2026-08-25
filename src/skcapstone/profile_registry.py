"""Fail-closed, versioned human and service profile registry."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROFILE_SCHEMA_VERSION = "skcapstone.agent-profile.v1"
REGISTRY_SCHEMA_VERSION = "skcapstone.profile-registry.v1"
SCHEMA_REVISION = "1"
REGISTRY_PATH = Path("config/profile-registry.json")
PROFILE_FILENAME = "profile.json"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class _StrictModel(BaseModel):
    """Reject coercion and unversioned extension fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class AgentProfileV1(_StrictModel):
    """Canonical authority-bearing profile metadata."""

    schema_version: Literal["skcapstone.agent-profile.v1"]
    schema_revision: Literal["1"]
    profile_id: str = Field(pattern=_ID_PATTERN)
    profile_kind: Literal["human", "service"]
    selectable: bool
    fallback_eligible: bool
    memory_principal_id: str = Field(min_length=1)
    default_tools: list[str]
    capability_policy_ref: str = Field(min_length=1)
    profile_revision: str = Field(min_length=1)
    profile_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def enforce_kind_boundary(self) -> "AgentProfileV1":
        """Force every service profile to the noninteractive zero-tool boundary."""
        if self.profile_kind == "service" and (
            self.selectable or self.fallback_eligible or self.default_tools
        ):
            raise ValueError(
                "service profiles must be nonselectable, fallback-ineligible, zero-tool"
            )
        if any(not tool.strip() for tool in self.default_tools):
            raise ValueError("default_tools entries must be non-empty")
        if len(self.default_tools) != len(set(self.default_tools)):
            raise ValueError("default_tools entries must be unique")
        return self


class RegistryProfileV1(_StrictModel):
    """Identity, selection, revision, and hash binding for one profile."""

    profile_id: str = Field(pattern=_ID_PATTERN)
    profile_kind: Literal["human", "service"]
    selectable: bool
    fallback_eligible: bool
    memory_principal_id: str = Field(min_length=1)
    schema_revision: Literal["1"]
    profile_revision: str = Field(min_length=1)
    profile_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def enforce_service_selection_boundary(self) -> "RegistryProfileV1":
        """Reject registries that advertise a service as human-selectable."""
        if self.profile_kind == "service" and (self.selectable or self.fallback_eligible):
            raise ValueError("service registry entries cannot be selectable or fallback-eligible")
        return self


class ProfileRegistryV1(_StrictModel):
    """Canonical registry envelope."""

    schema_version: Literal["skcapstone.profile-registry.v1"]
    schema_revision: Literal["1"]
    registry_revision: str = Field(min_length=1)
    profiles: list[RegistryProfileV1]
    registry_hash: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def reject_duplicate_profiles(self) -> "ProfileRegistryV1":
        """Reject ambiguous profile or memory identity bindings."""
        identifiers = [entry.profile_id for entry in self.profiles]
        principals = [entry.memory_principal_id for entry in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("registry profile_id entries must be unique")
        if len(principals) != len(set(principals)):
            raise ValueError("registry memory_principal_id entries must be unique")
        return self


class ResolvedProfile(_StrictModel):
    """A safe runtime projection; denied states never inherit identity or tools."""

    profile_id: str
    state: str
    healthy: bool
    profile_kind: Literal["human", "service"] | None = None
    selectable: bool = False
    fallback_eligible: bool = False
    memory_principal_id: str | None = None
    default_tools: list[str] = Field(default_factory=list)
    profile_revision: str | None = None
    profile_hash: str | None = None


def _canonical_hash(document: dict[str, Any], hash_field: str) -> str:
    """Hash a canonical JSON object while excluding its self-hash field.

    Args:
        document: JSON-compatible object to hash.
        hash_field: Top-level self-hash field to omit.

    Returns:
        A lowercase ``sha256:`` content digest.
    """
    payload = {key: value for key, value in document.items() if key != hash_field}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def profile_content_hash(document: dict[str, Any]) -> str:
    """Return the canonical hash for an agent profile document."""
    return _canonical_hash(document, "profile_hash")


def registry_content_hash(document: dict[str, Any]) -> str:
    """Return the canonical hash for a registry document."""
    return _canonical_hash(document, "registry_hash")


def conformance_pack_hash(document: dict[str, Any]) -> str:
    """Return the canonical hash for a public conformance fixture pack."""
    return _canonical_hash(document, "pack_hash")


def _read_object(path: Path) -> dict[str, Any]:
    """Read one JSON object, raising on absent, corrupt, or non-object data."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _denied(profile_id: str, state: str) -> ResolvedProfile:
    """Return the immutable zero-tool/no-memory failure projection."""
    return ResolvedProfile(profile_id=profile_id, state=state, healthy=False)


def resolve_profile(root: str | Path, profile_id: str) -> ResolvedProfile:
    """Resolve one registered profile without permissive fallback.

    Args:
        root: Shared SKCapstone root containing ``config`` and ``agents``.
        profile_id: Exact profile identifier to resolve.

    Returns:
        A healthy human projection, a bounded service projection, or a denied
        zero-tool projection. Missing, malformed, stale, hash-mismatched, and
        unknown-version inputs never borrow a memory principal.
    """
    if not re.fullmatch(_ID_PATTERN, profile_id):
        return _denied(profile_id, "unknown_profile")

    base = Path(root).expanduser()
    try:
        registry_raw = _read_object(base / REGISTRY_PATH)
    except FileNotFoundError:
        return _denied(profile_id, "missing_registry")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _denied(profile_id, "corrupt_registry")

    if (
        registry_raw.get("schema_version") != REGISTRY_SCHEMA_VERSION
        or registry_raw.get("schema_revision") != SCHEMA_REVISION
    ):
        return _denied(profile_id, "unknown_registry_version")
    try:
        registry = ProfileRegistryV1.model_validate(registry_raw)
    except ValidationError:
        return _denied(profile_id, "corrupt_registry")
    if registry.registry_hash != registry_content_hash(registry_raw):
        return _denied(profile_id, "registry_hash_mismatch")

    matches = [entry for entry in registry.profiles if entry.profile_id == profile_id]
    if not matches:
        return _denied(profile_id, "unknown_profile")
    binding = matches[0]

    try:
        profile_raw = _read_object(base / "agents" / profile_id / PROFILE_FILENAME)
    except FileNotFoundError:
        return _denied(profile_id, "missing_profile")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return _denied(profile_id, "corrupt_profile")

    if (
        profile_raw.get("schema_version") != PROFILE_SCHEMA_VERSION
        or profile_raw.get("schema_revision") != SCHEMA_REVISION
    ):
        return _denied(profile_id, "unknown_profile_version")
    try:
        profile = AgentProfileV1.model_validate(profile_raw)
    except ValidationError:
        return _denied(profile_id, "corrupt_profile")
    if profile.profile_id != profile_id:
        return _denied(profile_id, "profile_id_mismatch")
    if profile.profile_hash != profile_content_hash(profile_raw):
        return _denied(profile_id, "profile_hash_mismatch")
    if (
        binding.profile_kind != profile.profile_kind
        or binding.selectable != profile.selectable
        or binding.fallback_eligible != profile.fallback_eligible
        or binding.memory_principal_id != profile.memory_principal_id
        or binding.schema_revision != profile.schema_revision
        or binding.profile_revision != profile.profile_revision
        or binding.profile_hash != profile.profile_hash
    ):
        return _denied(profile_id, "stale_profile")

    if profile.profile_kind == "service":
        return ResolvedProfile(
            profile_id=profile_id,
            state="service_profile",
            healthy=True,
            profile_kind="service",
            memory_principal_id=profile.memory_principal_id,
            profile_revision=profile.profile_revision,
            profile_hash=profile.profile_hash,
        )
    return ResolvedProfile(
        profile_id=profile_id,
        state="healthy",
        healthy=True,
        profile_kind="human",
        selectable=profile.selectable,
        fallback_eligible=profile.fallback_eligible,
        memory_principal_id=profile.memory_principal_id,
        default_tools=list(profile.default_tools),
        profile_revision=profile.profile_revision,
        profile_hash=profile.profile_hash,
    )


def profile_is_eligible(root: str | Path, profile_id: str, *, fallback: bool = False) -> bool:
    """Return whether a human profile is selectable for the requested mode."""
    profile = resolve_profile(root, profile_id)
    return bool(
        profile.healthy
        and profile.profile_kind == "human"
        and profile.selectable
        and (not fallback or profile.fallback_eligible)
    )


def _eligibility_main(argv: list[str]) -> int:
    """Parse ``eligible ROOT PROFILE_ID MODE`` without exposing a public CLI."""
    if len(argv) != 5 or argv[1] != "eligible" or argv[4] not in {"selectable", "fallback"}:
        return 2
    return 0 if profile_is_eligible(argv[2], argv[3], fallback=argv[4] == "fallback") else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the shell picker
    raise SystemExit(_eligibility_main(sys.argv))
