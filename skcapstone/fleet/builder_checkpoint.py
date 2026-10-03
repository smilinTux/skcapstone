"""Seal and reconstruct clean committed work after execution extinction.

The caller owns execution/claim fencing and the attempt namespace. This module
never releases claims, stops workers, cleans a source workspace or uses a remote
provider. Uncommitted work is retained in its original directory and refused.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

_SCHEMA = "skfleet.clean-checkpoint/v1"
_BINDING = ("request_id", "card_id", "repository", "base_ref", "base_revision")
_EXECUTION = (
    "request_id",
    "card_id",
    "owner",
    "claim_revision",
    "execution_host",
    "execution_boot_id",
    "execution_unit",
    "workspace",
)
_SHA = re.compile(r"[0-9a-f]{40}")


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("checkpoint identity requires bounded nonempty strings")
    return value


def _binding(request: dict) -> dict:
    result = {key: _text(request.get(key)) for key in _BINDING}
    url = urlsplit(result["repository"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not _SHA.fullmatch(result["base_revision"])
        or not re.fullmatch(r"[0-9a-f]{8}", result["card_id"])
    ):
        raise ValueError("checkpoint source binding is invalid")
    return result


def _git(workspace: Path, *args: str) -> str:
    """Run a bounded local Git operation without hooks or credential prompts."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
            cwd=workspace,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"cannot verify checkpoint Git operation {args[0]}") from exc
    if result.returncode:
        raise ValueError(f"cannot verify checkpoint Git operation {args[0]}")
    return result.stdout.strip()


def _inspect(workspace: Path, binding: dict) -> tuple[str, str]:
    if (
        not workspace.is_dir()
        or Path(_git(workspace, "rev-parse", "--show-toplevel")) != workspace
    ):
        raise ValueError("checkpoint workspace must be the repository root")
    if _git(workspace, "remote", "get-url", "origin") != binding["repository"]:
        raise ValueError("checkpoint repository differs from its source binding")
    if _git(workspace, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("checkpoint requires clean committed source; original work is preserved")
    head = _git(workspace, "rev-parse", "HEAD")
    tree = _git(workspace, "rev-parse", "HEAD^{tree}")
    if not _SHA.fullmatch(head) or not _SHA.fullmatch(tree):
        raise ValueError("checkpoint Git identity is invalid")
    try:
        _git(workspace, "merge-base", "--is-ancestor", binding["base_revision"], head)
    except ValueError as exc:
        raise ValueError("checkpoint ancestry does not include the exact original base") from exc
    return head, tree


def _publish(path: Path, payload: bytes) -> None:
    """Publish one immutable receipt; an exact replay is harmless."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".checkpoint.", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ValueError(
                    "existing checkpoint does not match this exact generation"
                ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _receipt(path: Path, payload: bytes, manifest: dict) -> dict:
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "request_id": manifest["binding"]["request_id"],
        "card_id": manifest["binding"]["card_id"],
        "owner": manifest["execution"]["owner"],
        "claim_revision": manifest["execution"]["claim_revision"],
        "head": manifest["head"],
        "tree": manifest["tree"],
    }


def _publish_workspace(source: Path, destination: Path) -> None:
    """Use Linux atomic no-replace rename; unsupported platforms keep custody."""
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise ValueError("atomic checkpoint destination publication is unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # AT_FDCWD and RENAME_NOREPLACE prevent even an empty raced target changing.
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ValueError("checkpoint destination appeared during reconstruction")
        raise ValueError(f"atomic checkpoint destination publication failed: {os.strerror(error)}")


def seal_clean_checkpoint(workspace: Path, request: dict, status: dict, destination: Path) -> dict:
    """Seal local committed source only after the caller proves writer extinction."""
    workspace = workspace.resolve(strict=True)
    binding = _binding(request)
    execution = {key: _text(status.get(key)) for key in _EXECUTION}
    if (
        execution["request_id"] != binding["request_id"]
        or execution["card_id"] != binding["card_id"]
        or Path(execution["workspace"]).resolve() != workspace
    ):
        raise ValueError("checkpoint execution identity differs from the requested source")
    head, tree = _inspect(workspace, binding)
    manifest = {
        "schema": _SCHEMA,
        "binding": binding,
        "execution": execution,
        "workspace": str(workspace),
        "head": head,
        "tree": tree,
    }
    destination = destination.absolute()
    if destination.resolve().is_relative_to(workspace):
        raise ValueError("checkpoint destination must be outside the original workspace")
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    _publish(destination, payload)
    return _receipt(destination, payload, manifest)


def restore_clean_checkpoint(receipt: dict, request: dict, destination: Path) -> Path:
    """Reconstruct a hash-bound local checkpoint without altering old work."""
    path = Path(_text(receipt.get("path")))
    if path.is_symlink():
        raise ValueError("checkpoint receipt must not be a symlink")
    with path.open("rb") as stream:
        payload = stream.read(32769)
    if len(payload) > 32768 or hashlib.sha256(payload).hexdigest() != receipt.get("sha256"):
        raise ValueError("checkpoint hash does not match its receipt")
    try:
        manifest = json.loads(payload)
        if manifest["schema"] != _SCHEMA or manifest["binding"] != _binding(request):
            raise ValueError("checkpoint does not match the current request")
        if _receipt(path, payload, manifest) != receipt:
            raise ValueError("checkpoint receipt identity differs from its hashed manifest")
        for key in _EXECUTION:
            _text(manifest["execution"][key])
        source = Path(_text(manifest["workspace"])).resolve(strict=True)
        if Path(manifest["execution"]["workspace"]).resolve() != source:
            raise ValueError("checkpoint workspace identity differs")
        expected = (_text(manifest["head"]), _text(manifest["tree"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint manifest is malformed") from exc
    target = destination.absolute()
    if target.exists() or target.is_symlink() or target.resolve().is_relative_to(source):
        raise ValueError("checkpoint destination must be a new isolated workspace")
    if _inspect(source, manifest["binding"]) != expected:
        raise ValueError("checkpoint source changed after sealing")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        _git(temporary, "init", "--quiet")
        _git(temporary, "remote", "add", "origin", request["repository"])
        _git(
            temporary,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            str(source),
            expected[0],
        )
        _git(temporary, "checkout", "--quiet", "--detach", expected[0])
        if _inspect(temporary, manifest["binding"]) != expected:
            raise ValueError("reconstructed checkpoint identity differs")
        if _inspect(source, manifest["binding"]) != expected:
            raise ValueError("checkpoint source changed during reconstruction")
        if target.exists() or target.is_symlink():
            raise ValueError("checkpoint destination appeared during reconstruction")
        _publish_workspace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target
