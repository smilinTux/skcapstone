#!/usr/bin/env python3
"""Build byte-reproducible SKCapstone wheel and source archives."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import subprocess
import sys
import tarfile
from pathlib import Path

VERSION_ENV = "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class BuildError(RuntimeError):
    """Raised when deterministic build provenance cannot be established."""


def _command(args: list[str], root: Path) -> str:
    """Run one metadata command and return stripped stdout."""
    try:
        result = subprocess.run(
            args,
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"metadata command failed: {' '.join(args)}") from exc
    return result.stdout.strip()


def _source_version(root: Path) -> str:
    """Return the exact setuptools-scm version for a clean source checkout."""
    version = _command([sys.executable, "-m", "setuptools_scm"], root)
    if not version:
        raise BuildError("setuptools-scm returned no version metadata")
    return version


def source_metadata(root: Path) -> tuple[int, str]:
    """Derive the fixed build epoch and exact version from committed source."""
    if _command(["git", "rev-parse", "--is-inside-work-tree"], root) != "true":
        raise BuildError("source is not a Git worktree")
    if _command(["git", "status", "--porcelain", "--untracked-files=no"], root):
        raise BuildError("source has tracked changes")
    raw_epoch = _command(["git", "show", "-s", "--format=%ct", "HEAD"], root)
    if not raw_epoch.isdecimal():
        raise BuildError("source commit timestamp is invalid")
    return int(raw_epoch), _source_version(root)


def build_environment(epoch: int, version: str) -> dict[str, str]:
    """Return the current environment with deterministic build inputs fixed."""
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    env[VERSION_ENV] = version
    return env


def normalize_sdist(path: Path, epoch: int) -> None:
    """Rewrite one sdist with stable gzip, tar, timestamp, and owner metadata."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with tarfile.open(path, "r:gz") as source, temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch) as zipped:
                with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as target:
                    for original in sorted(source.getmembers(), key=lambda item: item.name):
                        member = copy.copy(original)
                        member.mtime = epoch
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.pax_headers = {}
                        payload = source.extractfile(original) if original.isfile() else None
                        try:
                            target.addfile(member, payload)
                        finally:
                            if payload is not None:
                                payload.close()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_build(root: Path, outdir: Path, env: dict[str, str]) -> None:
    """Run the existing isolated PEP 517 build and propagate any failure."""
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir), str(root)],
        cwd=root,
        env=env,
        check=True,
    )


def build(root: Path, outdir: Path) -> tuple[int, str, list[Path]]:
    """Build and normalize exactly one wheel and one source distribution."""
    epoch, version = source_metadata(root)
    outdir.mkdir(parents=True, exist_ok=True)
    if any(outdir.iterdir()):
        raise BuildError("output directory must be empty")
    _run_build(root, outdir, build_environment(epoch, version))
    wheels = sorted(outdir.glob("*.whl"))
    sdists = sorted(outdir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise BuildError("build must produce exactly one wheel and one source distribution")
    normalize_sdist(sdists[0], epoch)
    return epoch, version, [wheels[0], sdists[0]]


def main(argv: list[str] | None = None) -> int:
    """Run the deterministic build CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        epoch, version, artifacts = build(SOURCE_ROOT, args.outdir.resolve())
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"reproducible build failed: {exc}", file=sys.stderr)
        return 1
    print(f"SOURCE_DATE_EPOCH={epoch}")
    print(f"{VERSION_ENV}={version}")
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
