"""Validation for immutable source workspace bindings."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def source_binding_meta(
    labels: list[str] | tuple[str, ...],
    repository: object = None,
    base_ref: object = None,
    base_revision: object = None,
) -> dict[str, str]:
    """Validate an optional source binding and return normalized card metadata."""
    source_only = "source-only" in {str(value).strip().lower() for value in labels}
    values = (repository, base_ref, base_revision)
    if not source_only and all(value is None for value in values):
        return {}
    missing = [
        name
        for name, value in zip(("repository", "base_ref", "base_revision"), values, strict=True)
        if not str(value or "").strip()
    ]
    if missing:
        raise ValueError(
            "incomplete source binding; provide repository, named base_ref, and "
            "exact base_revision; missing: " + ", ".join(missing)
        )
    repository_value = str(repository).strip()
    parsed = urlsplit(repository_value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("repository must be a credential-free HTTPS URL")
    base_ref_value = str(base_ref).strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", base_ref_value):
        raise ValueError(
            "base_ref must be a branch or tag name, not a commit SHA; put the SHA in base_revision"
        )
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_ref_value) or base_ref_value.startswith("-"):
        raise ValueError("base_ref must be a bounded branch or tag name")
    revision_value = str(base_revision).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision_value):
        raise ValueError("base_revision must be an exact 40-hex commit SHA")
    return {
        "repository": repository_value,
        "base_ref": base_ref_value,
        "base_revision": revision_value,
    }
