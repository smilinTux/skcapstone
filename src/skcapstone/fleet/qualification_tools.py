"""Provision pinned source-suite tools through the existing operator rollout."""

from __future__ import annotations

import argparse
import bz2
import hashlib
import io
import json
import os
import platform
import socket
import stat
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable

from .paths import paths_for_home, valid_name
from .production_policy import _unique_object
from .rollout_artifacts import _write

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024
MANIFEST = Path(__file__).parents[1] / "data/fleet-qualification-tools.json"


def load_artifacts(arch: str) -> dict:
    """Read fixed upstream pins from the installed distribution's manifest."""
    if arch not in {"x86_64", "aarch64"}:
        raise ValueError("unsupported qualification tool architecture")
    value = json.loads(MANIFEST.read_bytes(), object_pairs_hook=_unique_object)
    if value.get("schema") != "skfleet.qualification-tools-artifacts/v1":
        raise ValueError("invalid qualification tool artifact manifest")
    assets = value["architectures"][arch]
    if set(assets) != {"restic", "shellcheck"}:
        raise ValueError("invalid qualification tool artifact set")
    for name, asset in assets.items():
        version = "0.19.1" if name == "restic" else "0.11.0"
        if asset["version"] != version:
            raise ValueError("invalid qualification tool version")
        for key in ("archive_sha256", "binary_sha256"):
            digest = asset[key]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("invalid qualification tool checksum")
    return assets


def selected_host(path: Path, host: str, toolchain: str = "skstacks") -> bool:
    """Require an owned private bounded exact host/toolchain allowlist."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("qualification tool allowlist must be an owned private file")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("qualification tool allowlist exceeds bound")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "hosts"}
        or value["schema"] != "skfleet.qualification-tools/v1"
        or not isinstance(value["hosts"], dict)
        or len(value["hosts"]) > 128
        or not isinstance(host, str)
        or not valid_name(host)
    ):
        raise ValueError("invalid qualification tool allowlist")
    for name, toolchains in value["hosts"].items():
        if (
            not valid_name(name)
            or len(name) > 253
            or not isinstance(toolchains, list)
            or not toolchains
            or any(
                not isinstance(item, str) or item not in {"skstacks", "sandbox"}
                for item in toolchains
            )
            or len(toolchains) != len(set(toolchains))
        ):
            raise ValueError("invalid qualification tool allowlist entry")
    return toolchain in value["hosts"].get(host, [])


def download(url: str) -> bytes:
    """Fetch bounded public release bytes; never use a credential-bearing URL."""
    if not url.startswith("https://github.com/"):
        raise ValueError("qualification tool URL is not a fixed upstream release")
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("qualification tool archive exceeds bound")
    return data


def unpack(data: bytes, asset: dict) -> bytes:
    """Verify an archive and read only its pinned regular executable member."""
    if len(data) > MAX_ARCHIVE:
        raise ValueError("qualification tool archive exceeds bound")
    if hashlib.sha256(data).hexdigest() != asset["archive_sha256"]:
        raise ValueError("qualification tool archive checksum mismatch")
    if asset["format"] == "bz2":
        decoder = bz2.BZ2Decompressor()
        binary = decoder.decompress(data, max_length=MAX_BINARY + 1)
        if not decoder.eof or decoder.unused_data:
            raise ValueError("qualification tool binary exceeds bound or has trailing data")
    elif asset["format"] == "tar.gz":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            entry = archive.getmember(asset["member"])
            if not entry.isfile():
                raise ValueError("qualification tool archive member must be regular")
            if entry.size > MAX_BINARY:
                raise ValueError("qualification tool binary exceeds bound")
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError("qualification tool archive member is missing")
            with stream:
                binary = stream.read(MAX_BINARY + 1)
    else:
        raise ValueError("unsupported qualification tool archive")
    if not binary or len(binary) > MAX_BINARY:
        raise ValueError("qualification tool binary exceeds bound")
    if hashlib.sha256(binary).hexdigest() != asset["binary_sha256"]:
        raise ValueError("qualification tool binary checksum mismatch")
    return binary


def current_binary(path: Path, prefix: Path, expected: str) -> bool:
    """Return an executable-byte match without following redirected target files."""
    if path.is_symlink() or not path.parent.resolve().is_relative_to(prefix.resolve()):
        raise ValueError("qualification tool target is redirected")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("qualification tool target is not a regular file")
        data = stream.read(MAX_BINARY + 1)
    return bool(
        info.st_mode & 0o111
        and len(data) <= MAX_BINARY
        and hashlib.sha256(data).hexdigest() == expected
    )


def provision(
    prefix: Path,
    allowlist: Path,
    host: str,
    arch: str,
    *,
    apply: bool = False,
    fetch: Callable[[str], bytes] = download,
) -> dict:
    """Plan by default; download and replace tools only on selected worker hosts."""
    if not selected_host(allowlist, host):
        return {"state": "not-selected", "host": host, "tools": []}
    if platform.system() != "Linux":
        raise ValueError("qualification tools require Linux on selected hosts")
    if prefix.is_symlink() or (prefix / "bin").is_symlink():
        raise ValueError("qualification tool prefix is redirected")
    assets = load_artifacts(arch)
    rows = [
        {
            "name": name,
            "version": asset["version"],
            "binary_sha256": asset["binary_sha256"],
            "changed": not current_binary(prefix / "bin" / name, prefix, asset["binary_sha256"]),
        }
        for name, asset in sorted(assets.items())
    ]
    if not apply:
        return {"state": "planned", "host": host, "tools": rows}
    if not prefix.is_dir() or not (prefix / "bin").is_dir():
        raise ValueError("qualification tool interpreter prefix must already exist")
    # Validate every replacement before changing either existing executable.
    pending = {
        row["name"]: unpack(fetch(assets[row["name"]]["url"]), assets[row["name"]])
        for row in rows
        if row["changed"]
    }
    for name, binary in pending.items():
        target = prefix / "bin" / name
        current_binary(target, prefix, assets[name]["binary_sha256"])
        _write(target, binary, 0o755)
    return {"state": "installed" if pending else "unchanged", "host": host, "tools": rows}


def main() -> None:
    """Expose opt-in tool provisioning to the existing governed rollout command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the operator host allowlist")
    args = parser.parse_args()
    home = Path.home()
    result = provision(
        home / ".skenv",
        paths_for_home(home).root / "qualification-tools.json",
        socket.gethostname().split(".")[0].lower(),
        platform.machine(),
        apply=args.apply,
    )
    from .sandbox_tools import provision as provision_sandbox

    sandbox = provision_sandbox(
        paths_for_home(home).root / "qualification-tools.json",
        socket.gethostname().split(".")[0].lower(),
        platform.machine(),
        apply=args.apply,
    )
    print(json.dumps({"source_suite": result, "sandbox": sandbox}, sort_keys=True))


if __name__ == "__main__":
    main()
