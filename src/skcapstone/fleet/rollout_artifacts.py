"""Install checkout artifacts without enabling or starting any service."""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import tempfile
from pathlib import Path

from skcapstone.fleet.deployment_manifest import (
    CANONICAL_SYSTEMD_RELATIVE_DIR,
    PER_HOST_ARTIFACTS,
    PER_HOST_BIN_RELATIVE_DIR,
    PRODUCTION_CANONICAL_SCRIPTS,
    production_compatibility_shim,
)
from skcapstone.fleet.paths import paths_for_home

_SKNODED_HOST_KEYS = frozenset(
    {
        "SKFLEET_PI",
        "SKFLEET_PI_CARDSTORE_GUARD",
        "SKFLEET_PRODUCTION_POLICY",
        "SKFLEET_AUTHORITY_HOST",
    }
)


def unit_source_path(repo_root: Path, name: str, production: bool) -> Path:
    """Select the same canonical unit for installation and drift checking."""
    root = repo_root / CANONICAL_SYSTEMD_RELATIVE_DIR
    overlay = root / "production" / name
    return overlay if production and overlay.is_file() else root / name


def _write(path: Path, data: bytes, mode: int) -> None:
    """Replace bytes atomically, including existing compatibility symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_artifact(repo_root: Path, home: Path, name: str) -> None:
    """Install one declared artifact, using native delegation on production hosts."""
    relative = next((path for path in PER_HOST_ARTIFACTS if path.name == name), None)
    if relative is None:
        raise ValueError("undeclared rollout artifact")
    production = (paths_for_home(home).root / "production.json").is_file()
    data = (
        production_compatibility_shim(name, home)
        if production and name in PRODUCTION_CANONICAL_SCRIPTS
        else (repo_root / relative).read_bytes()
    )
    _write(home / PER_HOST_BIN_RELATIVE_DIR / name, data, 0o755)


def _preserve_sknoded_environment(home: Path, units_dir: Path) -> None:
    """Move known host settings into the EnvironmentFile the main unit loads."""
    values: dict[str, str] = {}
    sources = [units_dir / "sknoded.service"]
    sources.extend(sorted((units_dir / "sknoded.service.d").glob("*.conf")))
    for source in sources:
        if not source.is_file():
            continue
        section = ""
        for line in source.read_text().splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            if section != "[Service]" or not line.startswith("Environment="):
                continue
            assignments = shlex.split(line.partition("=")[2])
            if not assignments:
                values.clear()
            for assignment in assignments:
                key, separator, value = assignment.partition("=")
                if separator and key in _SKNODED_HOST_KEYS:
                    values[key] = value.replace("%h", str(home))
    environment = home / ".config/sknoded/operator-http.env"
    original = environment.read_bytes() if environment.exists() else b""
    existing = {
        line.strip().partition("=")[0]
        for line in original.decode().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    additions = []
    for key, value in sorted(values.items()):
        if key in existing:
            continue
        if any(ord(character) < 32 for character in value):
            raise ValueError("invalid control character in sknoded host environment")
        quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        additions.append(f"{key}={quoted}\n")
    if additions:
        separator = b"\n" if original and not original.endswith(b"\n") else b""
        _write(environment, original + separator + "".join(additions).encode(), 0o600)


def install_units(repo_root: Path, home: Path) -> None:
    """Copy canonical service/timer bytes, retaining host env and unit preimages."""
    root = repo_root / CANONICAL_SYSTEMD_RELATIVE_DIR
    sources = sorted(path for path in root.iterdir() if path.suffix in {".service", ".timer"})
    if not sources:
        raise RuntimeError("no shipped systemd units in rollout checkout")
    units_dir = home / ".config/systemd/user"
    _preserve_sknoded_environment(home, units_dir)
    production = (paths_for_home(home).root / "production.json").is_file()
    for source in sources:
        data = unit_source_path(repo_root, source.name, production).read_bytes()
        target = units_dir / source.name
        previous = target.read_bytes() if target.is_file() else None
        if previous == data:
            continue
        if previous is not None:
            backup = (
                paths_for_home(home).root
                / "rollout-unit-preimages"
                / hashlib.sha256(previous).hexdigest()
                / source.name
            )
            if not backup.exists():
                _write(backup, previous, 0o600)
        _write(target, data, 0o644)


def main() -> None:
    """Expose artifact installation to the existing staged rollout shell steps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("artifact", help="Declared script basename, or 'units'")
    args = parser.parse_args()
    if args.artifact == "units":
        install_units(args.repo_root, Path.home())
    else:
        install_artifact(args.repo_root, Path.home(), args.artifact)


if __name__ == "__main__":
    main()
