"""Governed selected-host AppArmor and Node prerequisites, plus read-only gates."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from .production_policy import _unique_object
from .qualification_tools import selected_host

DATA = Path(__file__).parents[1] / "data"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_NODE = 256 * 1024 * 1024
PROFILE_SHA = "309919f9d569a90e8e777d300727c90cfc047bc1cf207733a94467f759bb16a1"


def profile_bytes() -> bytes:
    """Return the unchanged deployed attachment containing both profiles."""
    data = (DATA / "skfleet-bwrap.apparmor").read_bytes()
    if len(data) != 951 or hashlib.sha256(data).hexdigest() != PROFILE_SHA:
        raise ValueError("packaged AppArmor profile checksum mismatch")
    return data


def node_asset(arch: str) -> dict:
    """Read fixed official archive and executable pins for a supported host."""
    if arch not in {"x86_64", "aarch64"}:
        raise ValueError("unsupported sandbox tool architecture")
    manifest = json.loads(
        (DATA / "fleet-sandbox-tools.json").read_bytes(), object_pairs_hook=_unique_object
    )
    if (
        manifest["schema"] != "skfleet.sandbox-tools-artifacts/v1"
        or manifest["profile_sha256"] != PROFILE_SHA
    ):
        raise ValueError("invalid sandbox artifact manifest")
    asset = manifest["architectures"][arch]
    name = "node-v22.23.3-linux-" + ("x64" if arch == "x86_64" else "arm64")
    if (
        asset["version"] != "22.23.3"
        or asset["member"] != name + "/bin/node"
        or asset["url"] != "https://nodejs.org/dist/v22.23.3/" + name + ".tar.xz"
    ):
        raise ValueError("invalid official Node artifact")
    for key in ("archive_sha256", "binary_sha256"):
        digest = asset[key]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid Node checksum")
    return asset


def download(url: str) -> bytes:
    """Fetch only the fixed credential-free official Node release."""
    allowed = {node_asset(arch)["url"] for arch in ("x86_64", "aarch64")}
    if url not in allowed:
        raise ValueError("Node URL is not a fixed upstream release")
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Node archive exceeds bound")
    return data


def unpack(data: bytes, asset: dict) -> bytes:
    """Verify the official archive before reading only its regular Node binary."""
    import tarfile

    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != asset["archive_sha256"]:
        raise ValueError("Node archive checksum mismatch or exceeds bound")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as archive:
        entry = archive.getmember(asset["member"])
        if not entry.isfile() or entry.size > MAX_NODE:
            raise ValueError("Node member must be regular and within bound")
        with archive.extractfile(entry) as stream:
            binary = stream.read(MAX_NODE + 1)
    if (
        not binary
        or len(binary) > MAX_NODE
        or hashlib.sha256(binary).hexdigest() != asset["binary_sha256"]
    ):
        raise ValueError("Node executable checksum mismatch or exceeds bound")
    return binary


def root_file_matches(path: Path, digest: str, mode: int) -> bool:
    """Require exact executable/profile bytes and root ownership without redirects."""
    if path.is_symlink() or path.parent.resolve() != path.parent:
        raise ValueError("system tool target is redirected")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("system tool target is not a regular single-link file")
        if (
            info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size > MAX_NODE
        ):
            return False
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return checksum == digest


def loaded_profiles(root: Path, runner: Callable) -> bool:
    """Read both enforcing attachments, using bounded read-only sudo if needed."""
    path = root / "sys/kernel/security/apparmor/profiles"
    try:
        try:
            text = path.read_text()
        except PermissionError:
            text = runner(
                ["sudo", "-n", "/usr/bin/cat", str(path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout
        lines = set(text.splitlines())
        return {"skfleet_bwrap (enforce)", "skfleet_unpriv_bwrap (enforce)"} <= lines
    except (OSError, subprocess.SubprocessError):
        return False


def readiness(
    python_bin: str, *, root: Path = Path("/"), runner: Callable = subprocess.run
) -> list[str]:
    """Grade profiles and the actual network-sealed Python/Node execution path."""
    from .production_test_node import system_node
    from .production_test_worker import sandbox_command

    failures = []
    try:
        if not root_file_matches(root / "etc/apparmor.d/skfleet-bwrap", PROFILE_SHA, 0o644):
            failures.append("AppArmor profile missing, changed or not root:root 0644")
    except (OSError, ValueError):
        failures.append("AppArmor profile inaccessible or redirected")
    if not loaded_profiles(root, runner):
        failures.append("AppArmor profiles not loaded in enforce mode")
    probe = (
        "import socket; from pathlib import Path; "
        "assert all(name == 'lo' for _,name in socket.if_nameindex()); "
        "status=Path('/proc/self/status').read_text(); "
        "assert int(status.split('CapEff:')[1].split()[0],16)==0"
    )
    with tempfile.TemporaryDirectory(prefix="skfleet-sandbox-probe-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / "output"
        source.mkdir()
        output.mkdir()
        for label, argv in (
            ("bwrap Python", [python_bin, "-I", "-c", probe]),
            ("Node major must be 22", [str(system_node(root)), "--version"]),
        ):
            command = sandbox_command(source, output, argv)
            try:
                result = runner(command, capture_output=True, text=True, timeout=10)
                if result.returncode or (
                    label.startswith("Node") and not result.stdout.strip().startswith("v22.")
                ):
                    failures.append(label + " refused or invalid")
            except (OSError, subprocess.SubprocessError):
                failures.append(label + " missing, refused or timed out")
    return failures


def _install(path: Path, data: bytes, mode: str, runner: Callable) -> None:
    """Stage root-owned bytes in the destination directory and atomically rename."""
    staged = path.with_name("." + path.name + ".skfleet-" + uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix="skfleet-system-tool-") as directory:
        source = Path(directory) / "verified-bytes"
        source.write_bytes(data)
        source.chmod(0o600)
        try:
            runner(
                [
                    "sudo",
                    "-n",
                    "/usr/bin/install",
                    "-o",
                    "root",
                    "-g",
                    "root",
                    "-m",
                    mode,
                    "--",
                    str(source),
                    str(staged),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            runner(
                ["sudo", "-n", "/usr/bin/mv", "-Tf", "--", str(staged), str(path)],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        finally:
            if staged.exists():
                runner(
                    ["sudo", "-n", "/usr/bin/rm", "-f", "--", str(staged)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )


def provision(
    allowlist: Path,
    host: str,
    arch: str,
    *,
    apply: bool = False,
    root: Path = Path("/"),
    fetch: Callable = download,
    runner: Callable = subprocess.run,
) -> dict:
    """Plan by default; apply only the explicit sandbox host selection."""
    if not selected_host(allowlist, host, "sandbox"):
        return {"state": "not-selected", "host": host}
    if platform.system() != "Linux":
        raise ValueError("sandbox tools require Linux")
    asset, profile = node_asset(arch), profile_bytes()
    destinations = [
        (root / "etc/apparmor.d/skfleet-bwrap", PROFILE_SHA, "0644", profile),
        (root / "usr/local/bin/node", asset["binary_sha256"], "0755", None),
    ]
    pending = [
        (path, mode, data)
        for path, digest, mode, data in destinations
        if not root_file_matches(path, digest, int(mode, 8))
    ]
    if not apply:
        return {
            "state": "planned",
            "host": host,
            "node_version": asset["version"],
            "changed": bool(pending),
        }
    # Archive and both target validations precede every privileged mutation.
    binary = (
        unpack(fetch(asset["url"]), asset) if any(data is None for _, _, data in pending) else None
    )
    for path, mode, data in pending:
        _install(path, binary if data is None else data, mode, runner)
    if not all(
        root_file_matches(path, digest, int(mode, 8)) for path, digest, mode, _ in destinations
    ):
        raise ValueError("installed sandbox artifact checksum/ownership verification failed")
    if pending or not loaded_profiles(root, runner):
        runner(
            ["sudo", "-n", "/usr/sbin/apparmor_parser", "-r", str(destinations[0][0])],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    from .production_test_plan import PREFIX

    # Probe the interpreter sealed tests actually run, from the clean prefix.
    failures = readiness(str(PREFIX / "bin/python"), root=root, runner=runner)
    if failures:
        raise ValueError(
            "installed sandbox prerequisites failed verification: " + "; ".join(failures)
        )
    return {
        "state": "installed" if pending else "unchanged",
        "host": host,
        "node_version": asset["version"],
    }
