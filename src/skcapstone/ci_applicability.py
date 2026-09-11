"""Bind repository CI policy to immutable Git objects and completion evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .ci_profile_registry import INITIAL_PROFILE_DIGESTS

_LIMIT = 64 * 1024
_MANIFEST = ".skcapstone/ci-profile.json"
_EVENT_BYTES = 8 * 1024 * 1024
_EVENT_ROWS = 100000
_EVENT_FILES = 1024
_LEGACY = frozenset(
    {
        "ci_check_docs",
        "ci_check_gitleaks",
        "ci_check_lint",
        "ci_check_shim_imports",
        "ci_check_python311",
        "ci_check_python312",
    }
)


def _object(value: object, keys: set[str] | None = None) -> dict:
    """Require an object with exactly the contract fields when supplied."""
    if not isinstance(value, dict) or (keys is not None and set(value) != keys):
        raise ValueError("CI applicability object has missing or unknown fields")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON keys at every nesting depth."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("CI applicability JSON contains duplicate fields")
        result[key] = value
    return result


def _json(text: object) -> dict:
    """Decode strict JSON objects without leaking their contents in errors."""
    if not isinstance(text, str):
        raise ValueError("CI applicability JSON must be text")
    try:
        return _object(json.loads(text, object_pairs_hook=_pairs))
    except (ValueError, RecursionError):
        raise ValueError("CI applicability JSON is invalid") from None


def _version(value: object) -> None:
    """Accept only the integer version 1, excluding booleans."""
    if type(value) is not int or value != 1:
        raise ValueError("unsupported CI applicability schema version")


def _hex(value: object, length: int) -> str:
    """Require an exact lowercase object ID or digest."""
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise ValueError("CI applicability requires exact lowercase revisions and digests")
    return value


def _repository(value: object) -> str:
    """Validate HTTPS repository identity and strip only documented suffixes."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("CI repository must be a credential-free HTTPS URL")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        parsed.port
    except ValueError:
        raise ValueError("CI repository must be a credential-free HTTPS URL") from None
    normalized = value.rstrip("/")
    return normalized.removesuffix(".git")


def _manifest(text: object) -> dict:
    """Validate the complete repository policy schema."""
    if not isinstance(text, str):
        raise ValueError("CI manifest must be UTF-8 text")
    try:
        if len(text.encode("utf-8")) > _LIMIT:
            raise ValueError("CI manifest exceeds 64 KiB")
    except UnicodeError:
        raise ValueError("CI manifest must be UTF-8 text") from None
    policy = _object(_json(text), {"schema_version", "repository", "checks"})
    _version(policy["schema_version"])
    _repository(policy["repository"])
    checks = _object(policy["checks"])
    if not _LEGACY.issubset(checks):
        raise ValueError("CI manifest must declare all six legacy check keys")
    for key, raw in checks.items():
        if not isinstance(key, str) or not re.fullmatch(r"ci_check_[a-z0-9_]+", key):
            raise ValueError("invalid CI manifest check key")
        check = _object(raw, {"expected", "reason"})
        state, reason = check["expected"], check["reason"]
        if not isinstance(reason, str) or state not in ("SUCCESS", "NOT_APPLICABLE"):
            raise ValueError("invalid CI manifest check declaration")
        if (state == "SUCCESS" and reason != "") or (
            state == "NOT_APPLICABLE" and not reason.strip()
        ):
            raise ValueError("CI manifest applicability reason does not match its state")
    if not any(check["expected"] == "SUCCESS" for check in checks.values()):
        raise ValueError("CI manifest must require at least one successful check")
    return policy


def _git(repo: Path, *args: str) -> bytes:
    """Run bounded offline Git inspection without shell or ambient Git overrides."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1",
        # An empty allowlist overrides repository-specific protocol permissions.
        GIT_ALLOW_PROTOCOL="",
    )
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=True,
            timeout=10,
            env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError("CI profile Git inspection failed or timed out") from None


def _blob(repo: Path, revision: str) -> bytes:
    """Inspect mode and size before reading the bounded, fixed-path blob."""
    entry = _git(repo, "ls-tree", "-z", revision, "--", _MANIFEST)
    match = re.fullmatch(
        rb"(100644|100755) blob ([0-9a-f]{40})\t\.skcapstone/ci-profile\.json\x00", entry
    )
    if match is None:
        raise ValueError("CI manifest must exist as a normal Git blob")
    oid = match.group(2).decode("ascii")
    try:
        size = int(_git(repo, "cat-file", "-s", oid))
    except ValueError:
        raise ValueError("CI manifest Git blob size could not be verified") from None
    if not 0 <= size <= _LIMIT:
        raise ValueError("CI manifest exceeds 64 KiB")
    data = _git(repo, "cat-file", "blob", oid)
    if len(data) != size:
        raise ValueError("CI manifest Git blob size changed")
    return data


def bind_ci_profile(meta: dict, request: dict) -> dict:
    """Return the verified immutable capsule, or raise ValueError before writes."""
    _object(meta)
    _object(request, {"candidate_revision", "profile_sha256", "repository_path"})
    repository = _repository(meta.get("repository"))
    if not isinstance(meta.get("base_ref"), str) or not meta["base_ref"].strip():
        raise ValueError("CI profile requires a complete immutable source binding")
    base = _hex(meta.get("base_revision"), 40)
    candidate = _hex(request["candidate_revision"], 40)
    digest = _hex(request["profile_sha256"], 64)
    if "link_head_revision" in meta and meta["link_head_revision"] != candidate:
        raise ValueError("CI candidate does not match the review head")
    path = request["repository_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("CI repository_path must be an absolute Git top-level directory")
    try:
        repo = Path(path).resolve(strict=True)
        top = Path(_git(repo, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve(
            strict=True
        )
        if not repo.is_dir() or top != repo:
            raise ValueError("CI repository_path must select the Git top-level directory")
        origins = (
            _git(repo, "config", "--get-all", "remote.origin.url").decode("utf-8").splitlines()
        )
        if len(origins) != 1 or _repository(origins[0]) != repository:
            raise ValueError("CI repository origin does not match the source binding")
        for revision in (base, candidate):
            if _git(repo, "cat-file", "-t", revision) != b"commit\n":
                raise ValueError("CI revisions must identify exact commit objects")
        _git(repo, "merge-base", "--is-ancestor", base, candidate)
        data = _blob(repo, candidate)
        base_entry = _git(repo, "ls-tree", "-z", base, "--", _MANIFEST)
        if not base_entry:
            if INITIAL_PROFILE_DIGESTS.get(repository) != digest:
                raise ValueError("Initial CI policy requires a registered repository digest")
            changed = _git(
                repo,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--ignore-submodules=none",
                "--name-only",
                "-z",
                base,
                candidate,
                "--",
            )
            if changed != _MANIFEST.encode("ascii") + b"\x00":
                raise ValueError("Initial CI enrollment may change only the policy manifest")
        elif data != _blob(repo, base):
            raise ValueError("CI policy changed between the approved base and candidate")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("CI profile digest does not match committed policy bytes")
        text = data.decode("utf-8")
    except (OSError, UnicodeError, RuntimeError):
        raise ValueError("CI profile filesystem or UTF-8 inspection failed") from None
    policy = _manifest(text)
    if _repository(policy["repository"]) != repository:
        raise ValueError("CI manifest repository does not match the source binding")
    capsule = {
        "schema_version": 1,
        "repository": meta["repository"],
        "base_revision": base,
        "candidate_revision": candidate,
        "profile_sha256": digest,
        "manifest_text": text,
    }
    if not base_entry:
        capsule["initial_enrollment"] = True
    return capsule


def _capsule(core: dict) -> tuple[dict, dict]:
    """Cross-check stored policy against immutable source birth metadata."""
    meta = _object(_object(core).get("meta"))
    stored = _object(meta.get("ci_profile"))
    enrollment = "initial_enrollment" in stored
    capsule = _object(
        stored,
        {
            "schema_version",
            "repository",
            "base_revision",
            "candidate_revision",
            "profile_sha256",
            "manifest_text",
        }
        | ({"initial_enrollment"} if enrollment else set()),
    )
    if enrollment and (
        capsule["initial_enrollment"] is not True
        or INITIAL_PROFILE_DIGESTS.get(_repository(capsule["repository"]))
        != capsule["profile_sha256"]
    ):
        raise ValueError("Initial CI enrollment requires literal true and a registered digest")
    _version(capsule["schema_version"])
    for field, size in (("base_revision", 40), ("candidate_revision", 40), ("profile_sha256", 64)):
        _hex(capsule[field], size)
    if (
        _repository(capsule["repository"]) != _repository(meta.get("repository"))
        or capsule["base_revision"] != meta.get("base_revision")
        or (
            "link_head_revision" in meta
            and capsule["candidate_revision"] != meta["link_head_revision"]
        )
    ):
        raise ValueError("CI capsule does not match immutable source metadata")
    policy = _manifest(capsule["manifest_text"])
    if hashlib.sha256(capsule["manifest_text"].encode("utf-8")).hexdigest() != capsule[
        "profile_sha256"
    ] or _repository(policy["repository"]) != _repository(capsule["repository"]):
        raise ValueError("CI capsule policy bytes or repository were tampered with")
    return capsule, policy


def _card_event_rows(card_id: str, home: Path) -> list[dict]:
    """Read only bounded, strict, hash-linked evidence for one exact card."""
    if not isinstance(card_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", card_id
    ):
        raise ValueError("CI evidence requires a non-path card identifier")
    directory = -1
    rows = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory = os.open(home, flags)
            for name in ("cards", card_id, "events"):
                child = os.open(name, flags, dir_fd=directory)
                os.close(directory)
                directory = child
        except FileNotFoundError:
            return []
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if "sync-conflict" in entry.name:
                    raise ValueError("CI card evidence has an unresolved sync conflict")
                names.append(entry.name)
                if len(names) > _EVENT_FILES:
                    raise ValueError("CI card evidence has too many files")
        remaining = _EVENT_BYTES
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("CI card evidence must be regular single-link files")
                if info.st_size > remaining:
                    raise ValueError("CI card evidence exceeds the byte limit")
                chunks = []
                while True:
                    chunk = os.read(descriptor, min(65536, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("CI card evidence exceeds the byte limit")
                    chunks.append(chunk)
            finally:
                os.close(descriptor)
            previous = ""
            for raw in b"".join(chunks).decode("utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                if len(rows) >= _EVENT_ROWS:
                    raise ValueError("CI card evidence exceeds the row limit")
                row = _json(line)
                if "card_id" in row and row["card_id"] != card_id:
                    raise ValueError("CI event identity conflicts with its card directory")
                if "prev_hash" in row and row["prev_hash"] != previous:
                    raise ValueError("CI card evidence hash chain is invalid")
                previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
                rows.append(row)
    except (OSError, UnicodeError, TypeError, RuntimeError, AttributeError):
        raise ValueError("CI applicability evidence files are unreadable") from None
    finally:
        if directory >= 0:
            os.close(directory)
    return rows


def _latest_receipt(card_id: str, home: Path, key: str = "ci_applicability") -> dict:
    """Select one whole receipt by timezone-aware instant, rejecting ambiguity."""
    receipts = []
    for row in _card_event_rows(card_id, home):
        row_key = row.get("link_key") or row.get("key") or row.get("raw_key")
        if row_key != key:
            continue
        if row.get("action") != "link":
            raise ValueError("invalid CI applicability evidence action")
        stamp = row.get("ts")
        if not isinstance(stamp, str):
            raise ValueError("CI applicability receipt requires a timestamp")
        try:
            instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise ValueError("CI applicability timestamp must include a timezone") from None
        receipt = _json(row.get("link_value") or row.get("value"))
        receipts.append((instant, receipt))
    if not receipts:
        raise ValueError(f"no {key} receipt was recorded")
    latest = max(stamp for stamp, _ in receipts)
    selected = next(value for stamp, value in receipts if stamp == latest)
    if any(
        json.dumps(value, sort_keys=True) != json.dumps(selected, sort_keys=True)
        for stamp, value in receipts
        if stamp == latest
    ):
        raise ValueError("conflicting CI applicability receipts at the latest timestamp")
    return selected


def _evidence(value: object) -> bool:
    """Require a content digest or a credential-free immutable CI run/job URL."""
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
        return False
    if re.fullmatch(r"sha256:[0-9a-f]{64}(?::[^\s]+)?", value):
        return True
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and bool(re.search(r"/(?:runs?|jobs?|builds?)/[^/]+", url.path))
            and not re.search(r"/(?:latest|current)(?:/|$)", url.path)
        )
    except ValueError:
        return False


def validate_profile_completion(card_id: str, home: Path, core: dict) -> None:
    """Validate immutable policy and its latest pinned whole-result receipt."""
    capsule, policy = _capsule(core)
    enrollment = capsule.get("initial_enrollment") is True
    key = "ci_profile_enrollment" if enrollment else "ci_applicability"
    receipt = _object(
        _latest_receipt(card_id, Path(home), key),
        {
            "schema_version",
            "repository",
            "candidate_revision",
            "profile_sha256",
            "evidence" if enrollment else "checks",
        },
    )
    _version(receipt["schema_version"])
    if any(
        receipt[key] != capsule[key]
        for key in ("repository", "candidate_revision", "profile_sha256")
    ):
        raise ValueError("CI applicability receipt does not match immutable candidate pins")
    if enrollment:
        if not _evidence(receipt["evidence"]):
            raise ValueError("CI profile enrollment lacks immutable evidence")
        return
    results = _object(receipt["checks"], set(policy["checks"]))
    for key, declaration in policy["checks"].items():
        result = _object(results[key], {"state", "reason", "evidence"})
        if result["state"] != declaration["expected"] or result["reason"] != declaration["reason"]:
            raise ValueError(f"CI applicability check {key} does not satisfy repository policy")
        if declaration["expected"] == "NOT_APPLICABLE":
            valid = result["evidence"] == "sha256:" + capsule["profile_sha256"]
        else:
            valid = _evidence(result["evidence"])
        if not valid:
            raise ValueError(f"CI applicability check {key} lacks immutable evidence")
