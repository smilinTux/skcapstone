"""Read-only drift detection for checked-in user systemd units.

The effective unit returned by ``systemctl --user cat`` includes local drop-ins,
which are precisely the configuration most likely to make production differ from
the repository. This module never reloads, enables, starts, or writes units.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UnitDrift:
    """Difference between a desired source unit and its effective definition."""

    unit: str
    missing: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    unavailable: str = ""

    @property
    def clean(self) -> bool:
        """Return whether the effective unit exactly matches checked-in source."""
        return not (self.missing or self.changed or self.extra or self.unavailable)


def parse_unit(text: str) -> dict[str, tuple[str, ...]]:
    """Parse explicit unit directives without interpreting shell-like values."""
    section = ""
    parsed: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if not section or "=" not in line:
            continue
        key, value = line.split("=", 1)
        qualified = f"{section}.{key.strip()}"
        value = value.strip()
        if value == "":
            parsed[qualified] = []
        else:
            parsed.setdefault(qualified, []).append(value)
    return {key: tuple(values) for key, values in parsed.items()}


def compare_unit(unit: str, desired_text: str, effective_text: str) -> UnitDrift:
    """Compare explicit desired and effective directives."""
    desired = parse_unit(desired_text)
    effective = parse_unit(effective_text)
    missing = tuple(sorted(set(desired) - set(effective)))
    extra = tuple(sorted(set(effective) - set(desired)))
    changed = tuple(
        sorted(key for key in set(desired) & set(effective) if desired[key] != effective[key])
    )
    return UnitDrift(unit=unit, missing=missing, changed=changed, extra=extra)


def effective_unit(unit: str) -> str:
    """Read a user's effective unit, including drop-ins, without changing state."""
    result = subprocess.run(
        ["systemctl", "--user", "cat", unit], capture_output=True, text=True, timeout=10
    )
    if result.returncode:
        raise RuntimeError((result.stderr or "systemctl --user cat failed").strip())
    return result.stdout


def audit(source_dir: Path, units: tuple[str, ...]) -> tuple[UnitDrift, ...]:
    """Audit checked-in units against their effective user-systemd definitions."""
    results: list[UnitDrift] = []
    for unit in units:
        source = source_dir / unit
        if not source.is_file():
            results.append(UnitDrift(unit=unit, unavailable="desired source missing"))
            continue
        try:
            effective = effective_unit(unit)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            results.append(UnitDrift(unit=unit, unavailable=str(exc)))
            continue
        results.append(compare_unit(unit, source.read_text(encoding="utf-8"), effective))
    return tuple(results)

