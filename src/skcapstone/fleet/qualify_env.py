"""Build and verify the clean, pinned prefix that runs sealed qualification.

Sealed test execution (``production_test_plan.PREFIX``) used to be the
operator's production ``~/.skenv``. That prefix differs on every host (tool
versions, host-specific editable installs whose ``.pth`` files Python still
processes under ``-I``), so a remote builder could never reproduce the
authority's runtime fingerprint, and sklegal tests only imported because
chiap08 happened to carry sklegal's third-party dependencies through those
editables.

This module owns a dedicated prefix instead, built the same way on every host
from inputs committed here:

* ``data/qualify-env.json``: the exact interpreter (path and sha256), the
  sklegal revision and the sha256 of its ``uv.lock``, the direct
  (non-index) requirements, and the sha256 of the resolved lock below.
* ``data/qualify-env.lock.txt``: every third-party distribution, pinned with
  hashes. It is sklegal's locked third-party set (all workspace members plus
  the dev group, workspace and local path packages excluded because candidate
  source supplies them through PYTHONPATH) unioned with skcapstone's own
  runtime and ``fleet-qualify`` dependencies. sklegal's pins override
  skcapstone's declared ranges where they disagree (``rich``). Regenerate it
  with the ``lock`` subcommand, never by hand.

Hosts never need the private sklegal repository to build: only ``lock`` reads
it. ``build`` installs the lock with ``--no-deps --require-hashes``, then the
direct pins, then skcapstone itself (non-editable, ``--no-deps``) from the
deploy checkout so the trusted executor running from this prefix
(``production_tests.service_argv``) is the deployed revision.

``PREFIX/qualify-manifest.json`` is written deterministically (no host, no
time, no skcapstone version) and is bound into the runtime and toolchain
fingerprints, so lock identity is part of every sealed plan while a routine
skcapstone rollout does not change the toolchain fingerprint.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .qualified_runtime import STATE_NAME, missing_dependencies, qualify_prefix

DATA = Path(__file__).resolve().parents[1] / "data"
MANIFEST = DATA / "qualify-env.json"
LOCK = DATA / "qualify-env.lock.txt"
MANIFEST_SCHEMA = "skfleet.qualify-env/v1"
STATE_SCHEMA = "skfleet.qualify-env-state/v1"
SOURCE_NAME = "skcapstone-source.json"
#: Distributions installed outside the hashed lock, by design.
DIRECT_ONLY = frozenset({"skcapstone"})
#: Installer startup shims uv writes into every venv; build removes them so
#: interpreter startup runs only bytes the fingerprint covers.
UV_SHIMS = ("_virtualenv.pth", "_virtualenv.py")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class QualifyEnvError(RuntimeError):
    """The clean qualification prefix cannot be built or is not trustworthy."""


def canonical(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_manifest(path: Path = MANIFEST, lock: Path = LOCK) -> dict:
    """Read and validate the committed manifest, including the lock digest."""
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise QualifyEnvError("invalid qualify-env manifest schema")
    python, sklegal = value.get("python"), value.get("sklegal")
    direct = value.get("direct")
    if (
        set(value) != {"schema", "python", "sklegal", "direct", "lock_sha256", "platform"}
        or not isinstance(python, dict)
        or set(python) != {"path", "version", "sha256"}
        or not str(python["path"]).startswith("/usr/bin/python3.")
        or not isinstance(sklegal, dict)
        or set(sklegal) != {"repository", "fetch", "revision", "uv_lock_sha256"}
        or not re.fullmatch(r"[0-9a-f]{40}", str(sklegal["revision"]))
        or not isinstance(direct, list)
        or any(not isinstance(item, str) or " @ git+https://" not in item for item in direct)
        or any(not re.search(r"@[0-9a-f]{40}$", item) for item in direct)
        or not isinstance(value["platform"], str)
    ):
        raise QualifyEnvError("invalid qualify-env manifest")
    for digest in (python["sha256"], sklegal["uv_lock_sha256"], value["lock_sha256"]):
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise QualifyEnvError("invalid qualify-env manifest digest")
    if sha256_bytes(lock.read_bytes()) != value["lock_sha256"]:
        raise QualifyEnvError("qualify-env lock does not match its manifest digest")
    return value


def lock_pins(raw: bytes) -> dict[str, str]:
    """Return ``{canonical name: version}`` from a hashed requirements lock."""
    pins: dict[str, str] = {}
    for line in raw.decode().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "--hash")):
            continue
        requirement = line.split(";", 1)[0].rstrip(" \\").strip()
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)", requirement)
        if match is None:
            raise QualifyEnvError("qualify-env lock entry is not an exact pin: " + line[:80])
        name = canonical(match.group(1))
        if name in pins:
            raise QualifyEnvError("qualify-env lock pins a distribution twice: " + name)
        pins[name] = match.group(2)
    if not pins:
        raise QualifyEnvError("qualify-env lock is empty")
    return pins


def direct_pins(manifest: dict) -> dict[str, str]:
    """Return ``{canonical name: commit}`` for the manifest's git requirements."""
    return {
        canonical(item.split("@", 1)[0].strip()): item.rsplit("@", 1)[1]
        for item in manifest["direct"]
    }


def site_packages(prefix: Path) -> Path:
    return prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"


def _site(prefix: Path) -> Path:
    return site_packages(prefix) / "site-packages"


def installed(prefix: Path) -> dict[str, dict]:
    """Every installed distribution: version and parsed direct_url.json."""
    found: dict[str, dict] = {}
    for info in sorted(_site(prefix).glob("*.dist-info")):
        name = version = None
        try:
            metadata = (info / "METADATA").read_text(errors="replace")
        except OSError:
            continue
        for line in metadata.splitlines():
            if not line:
                break
            if line.startswith("Name: ") and name is None:
                name = line[6:].strip()
            elif line.startswith("Version: ") and version is None:
                version = line[9:].strip()
        if not name or not version:
            continue
        direct = None
        try:
            direct = json.loads((info / "direct_url.json").read_bytes())
        except (OSError, ValueError):
            pass
        found[canonical(name)] = {"version": version, "direct_url": direct}
    return found


def state_for(prefix: Path, manifest: dict) -> dict:
    """The deterministic build record bound into the qualification fingerprints."""
    packages = installed(prefix)
    return {
        "schema": STATE_SCHEMA,
        "python_sha256": manifest["python"]["sha256"],
        "sklegal_revision": manifest["sklegal"]["revision"],
        "sklegal_uv_lock_sha256": manifest["sklegal"]["uv_lock_sha256"],
        "lock_sha256": manifest["lock_sha256"],
        "direct": sorted(manifest["direct"]),
        "packages": sorted(
            f"{name}=={row['version']}"
            for name, row in packages.items()
            if name not in DIRECT_ONLY
        ),
    }


def state_bytes(state: dict) -> bytes:
    return (json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()


def verify(prefix: Path, manifest: dict | None = None, lock: Path = LOCK) -> list[str]:
    """Return every reason ``prefix`` is not the clean pinned runtime (empty when it is)."""
    manifest = load_manifest(lock=lock) if manifest is None else manifest
    findings: list[str] = []
    if prefix.is_symlink() or not prefix.is_dir():
        return ["qualify-env prefix is missing or redirected"]
    root = prefix.resolve()
    python = prefix / "bin/python"
    try:
        target = python.resolve(strict=True)
        if sha256_bytes(target.read_bytes()) != manifest["python"]["sha256"]:
            findings.append("interpreter differs from the pinned python sha256")
    except OSError:
        findings.append("interpreter is missing")
    try:
        config = (prefix / "pyvenv.cfg").read_text()
        if not re.search(r"(?m)^include-system-site-packages\s*=\s*false\s*$", config):
            findings.append("prefix exposes system site-packages")
    except OSError:
        findings.append("pyvenv.cfg is missing")
    site = _site(prefix)
    if not site.is_dir():
        return [*findings, "site-packages is missing"]
    for entry in sorted(site.iterdir()):
        if not entry.resolve().is_relative_to(root):
            findings.append("site-packages entry resolves outside the prefix: " + entry.name)
        if entry.suffix != ".pth":
            continue
        if entry.name.startswith("__editable__"):
            findings.append("editable startup file: " + entry.name)
            continue
        if entry.name in UV_SHIMS:
            findings.append("installer startup shim present: " + entry.name)
            continue
        try:
            lines = entry.read_text(errors="replace").splitlines()
        except OSError:
            findings.append("unreadable startup file: " + entry.name)
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", "import ", "import\t")):
                continue
            if not (site / line).resolve().is_relative_to(root):
                findings.append("startup path escapes the prefix: " + entry.name)
    findings.extend("missing required package: " + name for name in missing_dependencies(prefix))
    packages = installed(prefix)
    pins = lock_pins(lock.read_bytes())
    direct = direct_pins(manifest)
    for name, version in sorted(pins.items()):
        row = packages.get(name)
        if row is None:
            findings.append(f"locked distribution missing: {name}=={version}")
        elif row["version"] != version:
            findings.append(f"locked distribution drifted: {name} {row['version']} != {version}")
        elif (row["direct_url"] or {}).get("dir_info", {}).get("editable"):
            findings.append("editable distribution: " + name)
    for name, commit in sorted(direct.items()):
        row = packages.get(name)
        vcs = ((row or {}).get("direct_url") or {}).get("vcs_info", {})
        if row is None or vcs.get("commit_id") != commit:
            findings.append(f"direct distribution missing or not at {commit[:12]}: {name}")
    for name, row in sorted(packages.items()):
        if name not in pins and name not in direct and name not in DIRECT_ONLY:
            findings.append(f"unexpected distribution: {name}=={row['version']}")
        elif name in DIRECT_ONLY and (row["direct_url"] or {}).get("dir_info", {}).get("editable"):
            findings.append("editable distribution: " + name)
    try:
        if (prefix / STATE_NAME).read_bytes() != state_bytes(state_for(prefix, manifest)):
            findings.append("qualify manifest is stale")
    except OSError:
        findings.append("qualify manifest is missing")
    return findings


def find_uv() -> str:
    """Locate uv: PATH first, then the two estate install locations."""
    for candidate in (
        shutil.which("uv"),
        str(Path.home() / ".local/bin/uv"),
        str(Path.home() / ".skenv/bin/uv"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise QualifyEnvError("uv is not installed")


def _run(argv: list[str], timeout: int = 900) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHON"))}
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()[-6:]
        raise QualifyEnvError(f"{Path(argv[0]).name} {argv[1]} failed: " + " | ".join(tail))


def _source_record(source: Path) -> dict:
    """Identify the skcapstone checkout installed into the prefix."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()

    try:
        head = git("rev-parse", "--verify", "HEAD")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise QualifyEnvError("skcapstone source is not a readable git checkout") from exc
    return {"head": head, "dirty": dirty}


def _install_skcapstone(uv: str, prefix: Path, source: Path) -> None:
    _run(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            str(prefix / "bin/python"),
            "--no-deps",
            "--reinstall-package",
            "skcapstone",
            str(source),
        ]
    )


def _build_fresh(uv: str, staging: Path, manifest: dict, lock: Path, source: Path) -> None:
    python = str(staging / "bin/python")
    _run(
        [
            uv,
            "venv",
            "--no-config",
            "--relocatable",
            "--python",
            manifest["python"]["path"],
            "--python-preference",
            "only-system",
            str(staging),
        ]
    )
    _run(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            python,
            "--no-deps",
            "--require-hashes",
            "-r",
            str(lock),
        ]
    )
    _run(
        [uv, "pip", "install", "--no-config", "--python", python, "--no-deps", *manifest["direct"]]
    )
    _install_skcapstone(uv, staging, source)
    for shim in UV_SHIMS:
        (_site(staging) / shim).unlink(missing_ok=True)


def build(
    prefix: Path,
    source: Path,
    *,
    manifest_path: Path = MANIFEST,
    lock: Path = LOCK,
    uv: str | None = None,
) -> dict:
    """Idempotently build ``prefix``; rebuild atomically when the pinned inputs change."""
    manifest = load_manifest(manifest_path, lock)
    interpreter = Path(manifest["python"]["path"])
    if sha256_bytes(interpreter.read_bytes()) != manifest["python"]["sha256"]:
        raise QualifyEnvError("host interpreter differs from the pinned python sha256")
    if prefix.is_symlink() or prefix.parent.is_symlink():
        raise QualifyEnvError("qualify-env prefix is redirected")
    source = source.resolve()
    record = _source_record(source)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    uv = uv or find_uv()
    lock_fd = os.open(prefix.parent / ("." + prefix.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = "unchanged"
        base_ok = False
        if prefix.is_dir():
            try:
                previous = json.loads((prefix / STATE_NAME).read_bytes())
                base_ok = (
                    previous.get("lock_sha256") == manifest["lock_sha256"]
                    and previous.get("python_sha256") == manifest["python"]["sha256"]
                    and previous.get("direct") == sorted(manifest["direct"])
                    and previous.get("sklegal_uv_lock_sha256")
                    == manifest["sklegal"]["uv_lock_sha256"]
                    and not [
                        finding
                        for finding in verify(prefix, manifest, lock)
                        if finding != "qualify manifest is stale"
                    ]
                )
            except (OSError, ValueError):
                base_ok = False
        if base_ok:
            try:
                current = json.loads((prefix / SOURCE_NAME).read_bytes())
            except (OSError, ValueError):
                current = None
            if record["dirty"] or current != record:
                _install_skcapstone(uv, prefix, source)
                state = "updated"
            target = prefix
        else:
            staging = prefix.parent / f".{prefix.name}.build-{uuid.uuid4().hex[:12]}"
            try:
                _build_fresh(uv, staging, manifest, lock, source)
                (staging / STATE_NAME).write_bytes(state_bytes(state_for(staging, manifest)))
                (staging / SOURCE_NAME).write_text(json.dumps(record, sort_keys=True) + "\n")
                findings = verify(staging, manifest, lock)
                if findings:
                    raise QualifyEnvError(
                        "fresh qualify-env failed verification: " + "; ".join(findings)
                    )
                retired = None
                if prefix.exists():
                    retired = prefix.parent / f".{prefix.name}.old-{uuid.uuid4().hex[:12]}"
                    os.rename(prefix, retired)
                os.rename(staging, prefix)
                if retired is not None:
                    shutil.rmtree(retired, ignore_errors=True)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            state = "built"
            target = prefix
        (target / STATE_NAME).write_bytes(state_bytes(state_for(target, manifest)))
        (target / SOURCE_NAME).write_text(json.dumps(record, sort_keys=True) + "\n")
        findings = verify(target, manifest, lock)
        if findings:
            raise QualifyEnvError("qualify-env failed verification: " + "; ".join(findings))
    return {
        "state": state,
        "prefix": str(prefix),
        "state_sha256": sha256_bytes((prefix / STATE_NAME).read_bytes()),
        "skcapstone_head": record["head"],
    }


def generate_lock(
    sklegal: Path,
    source: Path,
    *,
    manifest_path: Path = MANIFEST,
    lock: Path = LOCK,
    uv: str | None = None,
) -> dict:
    """Maintainer step: regenerate the hashed lock from the pinned sklegal checkout."""
    manifest = json.loads(manifest_path.read_bytes())
    uv_lock = sha256_bytes((sklegal / "uv.lock").read_bytes())
    if uv_lock != manifest["sklegal"]["uv_lock_sha256"]:
        raise QualifyEnvError("sklegal uv.lock differs from the pinned digest")
    if (sklegal / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(sklegal), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        if head != manifest["sklegal"]["revision"]:
            raise QualifyEnvError("sklegal checkout is not at the pinned revision")
    uv = uv or find_uv()
    python_version = ".".join(manifest["python"]["version"].split(".")[:2])
    with tempfile.TemporaryDirectory(prefix="qualify-env-lock-") as directory:
        exported = Path(directory) / "sklegal.txt"
        _run(
            [
                uv,
                "export",
                "--project",
                str(sklegal),
                "--frozen",
                "--all-packages",
                "--group",
                "dev",
                "--no-emit-workspace",
                "--no-emit-local",
                "--no-hashes",
                "--no-header",
                "--no-annotate",
                "-o",
                str(exported),
            ]
        )
        request = Path(directory) / "request.txt"
        request.write_text(f"skcapstone[fleet-qualify] @ {source.resolve().as_uri()}\n")
        resolved = Path(directory) / "resolved.txt"
        _run(
            [
                uv,
                "pip",
                "compile",
                "--no-config",
                str(request),
                str(exported),
                "--override",
                str(exported),
                "--python-version",
                python_version,
                "--python-platform",
                manifest["platform"],
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "-o",
                str(resolved),
            ]
        )
        kept, skip = [], False
        for line in resolved.read_text().splitlines():
            if not line.startswith(" "):
                skip = " @ " in line
            if not skip:
                kept.append(line)
    header = (
        "# Generated by: python -m skcapstone.fleet.qualify_env lock\n"
        f"# sklegal {manifest['sklegal']['revision']} uv.lock {uv_lock}\n"
        "# Do not edit by hand. Direct git pins live in qualify-env.json.\n"
    )
    raw = (header + "\n".join(kept).rstrip() + "\n").encode()
    lock_pins(raw)
    lock.write_bytes(raw)
    manifest["lock_sha256"] = sha256_bytes(raw)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"lock_sha256": manifest["lock_sha256"], "distributions": len(lock_pins(raw))}


def fingerprints(prefix: Path) -> dict:
    """Print the values remote qualification compares, for cross-host parity checks."""
    from . import production_test_plan as plan

    plan.PREFIX = prefix
    return {
        "host": socket.gethostname().split(".")[0].lower(),
        "prefix": str(prefix),
        "state_sha256": sha256_bytes((prefix / STATE_NAME).read_bytes()),
        "toolchain_sha256": plan.toolchain_fingerprint(),
        "runtime_sha256": plan.runtime_fingerprint(),
        "python_sha256": plan.sha((prefix / "bin/python").read_bytes()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "fingerprint", "lock"):
        command = sub.add_parser(name)
        command.add_argument("--prefix", type=Path, default=qualify_prefix(Path.home()))
        command.add_argument("--manifest", type=Path, default=MANIFEST)
        command.add_argument("--lock", type=Path, default=LOCK)
        if name in {"build", "lock"}:
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--uv")
        if name == "lock":
            command.add_argument("--sklegal", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build(
                args.prefix, args.source, manifest_path=args.manifest, lock=args.lock, uv=args.uv
            )
        elif args.command == "verify":
            findings = verify(args.prefix, load_manifest(args.manifest, args.lock), args.lock)
            print(json.dumps({"prefix": str(args.prefix), "findings": findings}, sort_keys=True))
            return int(bool(findings))
        elif args.command == "fingerprint":
            result = fingerprints(args.prefix)
        else:
            result = generate_lock(
                args.sklegal, args.source, manifest_path=args.manifest, lock=args.lock, uv=args.uv
            )
    except (QualifyEnvError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
