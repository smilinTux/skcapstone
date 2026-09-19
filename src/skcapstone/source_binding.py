"""Validation for immutable source workspace bindings, and the safety label
vocabulary that used to be tangled up with them."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Labels that assert the safety constraint "take no external action".
#
# "source-only" is the deprecated spelling. It is kept, and kept first-class,
# because 2,612 chi cards already carry it and state the constraint verbatim in
# their acceptance criteria: "Source-only. No live database write, provider,
# Inbox, mailing, deployment, push, or external action." That label ALSO used to
# be the only thing that made a dispatcher demand a repository binding, so a
# single token meant both "materialize a pinned workspace" and "touch nothing
# live". Triage could not correct one without silently dropping the other.
#
# "no-external-action" is the constraint on its own, with no routing sense.
# Routing is now driven by the binding itself (see
# scripts/fleet/skfleet-rotate.py:_complete_source_binding), so a card that only
# needs the safety constraint no longer has to invent a repository to get it,
# and a card that carries a binding gets it verified whether or not it is
# labelled. Neither direction can lose the safety meaning by accident: nothing
# in the routing path reads these labels to decide whether to check a binding.
NO_EXTERNAL_ACTION_LABELS = ("no-external-action", "source-only")


def declares_no_external_action(labels: list[str] | tuple[str, ...]) -> bool:
    """True when a card asserts the no-external-action safety constraint."""
    normalized = {str(value).strip().lower() for value in labels}
    return bool(normalized.intersection(NO_EXTERNAL_ACTION_LABELS))


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
