"""Config kind: spec normalization and presence/drift/age conditions (Phase 7, step 1).

Pure Config-object model, mirroring the ModelServer spec/conditions split in
modelserver.py. No I/O: observed state is a plain dict passed in by the
caller (a later card). No secret material stored here; specs reference
skvault entry NAMES and expected file hashes only.
"""

from __future__ import annotations

from .conditions import _cond


class ConfigSpecError(ValueError):
    """A Config spec dict failed validation."""


def normalize_config_spec(spec: dict) -> dict:
    """Validate and fill defaults for a Config spec.

    Args:
        spec: Raw Config spec dict.

    Returns:
        Normalized dict with name, secrets, files, rotationDays, deleted.

    Raises:
        ConfigSpecError: name missing/non-str, secrets non-list or
            containing a non-empty-str skvault entry name, files non-dict,
            or rotationDays non-int when present.
    """
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigSpecError(f"config spec requires a non-empty str 'name', got {name!r}")
    secrets = spec.get("secrets", [])
    if not isinstance(secrets, list):
        raise ConfigSpecError(f"config 'secrets' must be a list, got {secrets!r}")
    for secret in secrets:
        if not isinstance(secret, str) or not secret:
            raise ConfigSpecError(
                f"config 'secrets' entries must be non-empty skvault entry names, got {secret!r}"
            )
    files = spec.get("files", {})
    if not isinstance(files, dict):
        raise ConfigSpecError(f"config 'files' must be a dict, got {files!r}")
    rotation_days = spec.get("rotationDays")
    if rotation_days is not None and not isinstance(rotation_days, int):
        raise ConfigSpecError(
            f"config 'rotationDays' must be an int when present, got {rotation_days!r}"
        )
    return {
        "name": name,
        "secrets": secrets,
        "files": files,
        "rotationDays": rotation_days,
        "deleted": bool(spec.get("deleted", False)),
    }


def config_conditions(spec: dict, observed: dict, now_iso: str) -> list[dict]:
    """Derive a Config's SecretPresent/ConfigDrift/RotationOverdue conditions."""
    present_secrets = observed.get("present_secrets", [])
    all_present = all(secret in present_secrets for secret in spec.get("secrets", []))
    file_hashes = observed.get("file_hashes", {})
    drifted = any(
        file_hashes.get(path) != sha256 for path, sha256 in spec.get("files", {}).items()
    )
    rotation_days = spec.get("rotationDays")
    age_days = observed.get("oldest_secret_age_days", 0)
    overdue = rotation_days is not None and age_days > rotation_days
    return [
        _cond(
            "SecretPresent",
            all_present,
            "AllSecretsPresent" if all_present else "SecretMissing",
            f"present_secrets is {present_secrets!r}",
            now_iso,
        ),
        _cond(
            "ConfigDrift",
            drifted,
            "HashMismatch" if drifted else "HashesMatch",
            f"file_hashes is {file_hashes!r}",
            now_iso,
        ),
        _cond(
            "RotationOverdue",
            overdue,
            "PastRotationWindow" if overdue else "WithinRotationWindow",
            f"oldest_secret_age_days is {observed.get('oldest_secret_age_days')!r}",
            now_iso,
        ),
    ]
