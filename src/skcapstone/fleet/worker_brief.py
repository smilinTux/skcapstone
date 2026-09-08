"""Deterministic, non-secret evidence materialization for fleet worker briefs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_RE = re.compile(r"(?i)(?:credential|password|secret|token|authorization)")
CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:[\w-]*(?:password|passwd|secret|token|credential)|pwd|api[_-]?key|"
    r"authorization)[\\\"']*\s*[:=]\s*\S|\b(?:Bearer|Basic)\s+\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:gh[pousr]_|github_pat_|sk-)[\w-]{16,}|"
    r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"
)
SAFE_LINK_KEYS = {
    "artifact_sha256",
    "artifacts_sha256",
    "candidate_commit",
    "candidate_evidence_sha256",
    "candidate_patch_sha256",
    "candidate_tree",
    "evidence",
    "evidence_path",
    "evidence_sha256",
    "incident_receipt",
    "open_pr",
    "pr",
    "producer_identity",
    "pull_request",
    "repository_scope",
    "source_manifest_sha256",
    "source_scope",
    "supersedes",
}
DEFAULT_PROHIBITED_ACTIONS = (
    "capability-token-disclosure",
    "credential-access",
    "database-mutation",
    "deployment",
    "protected-content-access",
    "service-change",
    "unrestricted-tool-grant",
)


class BriefEvidenceError(ValueError):
    """Raised when an immutable artifact required by a brief is unavailable."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(json.dumps(report, sort_keys=True, separators=(",", ":")))
        self.report = report


def _unsafe_content(value: object) -> bool:
    """Recognize credential representations without rejecting source vocabulary."""
    if isinstance(value, dict):
        return any(_unsafe_content(item) for item in value.values())
    if isinstance(value, list):
        return any(_unsafe_content(item) for item in value)
    if not isinstance(value, str):
        return False
    decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), value)
    decoded = decoded.replace(r"\/", "/")
    for _ in range(4):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    else:
        return True
    if CREDENTIAL_RE.search(decoded):
        return True
    for raw_url in re.findall(r"(?:[a-zA-Z][\w+.-]*:)?//[^\s<>\"']+", decoded):
        try:
            url = urlsplit(raw_url)
            if url.scheme not in {"", "http", "https"} or "@" in url.netloc:
                return True
            if not url.hostname:
                return True
            for key, _ in parse_qsl(url.query) + parse_qsl(url.fragment):
                if (
                    SENSITIVE_RE.search(key)
                    or key.lower().endswith("signature")
                    or key.lower()
                    in {
                        "key",
                        "api_key",
                        "apikey",
                        "auth",
                        "sig",
                        "signature",
                        "code",
                    }
                ):
                    return True
        except ValueError:
            return True
    return bool(re.search(r"(?i)\b(?:javascript|data|file):", decoded))


def safe_brief_links(links: object) -> dict[str, str]:
    """Return only bounded evidence metadata approved for worker prompts."""
    if not isinstance(links, dict):
        return {}
    safe: dict[str, str] = {}
    for raw_key, raw_value in links.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not value or SENSITIVE_RE.search(key):
            continue
        is_hash = key.endswith("_sha256") or key.endswith("_commit") or key.endswith("_tree")
        if key not in SAFE_LINK_KEYS and not is_hash:
            continue
        if _unsafe_content(value):
            continue
        safe[key] = value
    return dict(sorted(safe.items()))


@contextmanager
def _directory_beneath(root: Path, parts: tuple[str, ...]):
    """Pin directory components with no-follow opens beneath the trusted real root."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptors.append(os.open(root, flags))
        for part in parts:
            if part in {"", ".", ".."} or "/" in part:
                raise ValueError("invalid artifact component")
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _file_digest(directory: int, name: str) -> str:
    """Hash a regular file through its pinned directory without following symlinks."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    descriptor = os.open(name, flags, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("artifact is not a regular file")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _artifact_paths(evidence_root: Path, parent_id: str, digests: set[str]) -> dict[str, str]:
    """Resolve hashes within one producer, pinning every directory through the read."""
    found: dict[str, str] = {}
    root = evidence_root.resolve(strict=True)

    def walk(directory: int, relative: Path) -> None:
        """Traverse descriptor-relative entries, never symlinked directories or files."""
        for name in sorted(os.listdir(directory)):
            mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                with _directory_beneath(root, (*relative.parts, name)) as child:
                    walk(child, relative / name)
            elif stat.S_ISREG(mode):
                digest = _file_digest(directory, name)
                if digest in digests and digest not in found:
                    found[digest] = str(root / relative / name)
            if len(found) == len(digests):
                return

    try:
        with _directory_beneath(root, ("work", parent_id)) as directory:
            walk(directory, Path("work") / parent_id)
    except FileNotFoundError:
        return {}
    return found


def write_missing_report(report: dict[str, Any], evidence_root: Path) -> tuple[Path, str]:
    """Write one content-addressed immutable pre-claim failure report."""
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    payload = dict(report, content_sha256=digest)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    directory = evidence_root / "worker-brief-preflight"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['card_id']}-{digest}.json"
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise ValueError("immutable worker brief report collision")
    return path, digest


def materialize_work_envelope(
    card: dict[str, Any],
    *,
    labels: list[str],
    lane: str,
    model: str,
    seat: str | None,
    evidence_root: Path,
) -> dict[str, Any]:
    """Build and validate the exact non-secret card payload used by a worker."""
    card_id = card.get("id") if isinstance(card, dict) else None
    if not isinstance(card_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", card_id
    ):
        card_id = "unknown"
    try:
        if not isinstance(card, dict) or card_id == "unknown":
            raise ValueError("invalid card")
        for values in (
            labels,
            card.get("acceptance_criteria") or [],
            card.get("dependencies") or [],
        ):
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError("invalid list field")
        return _materialize_work_envelope(
            card, labels=labels, lane=lane, model=model, seat=seat, evidence_root=evidence_root
        )
    except BriefEvidenceError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        # Never copy rejected input or exception text into selector logs or reports.
        raise BriefEvidenceError(
            {"card_id": card_id, "kind": "worker-brief-validation-failed"}
        ) from None


def _materialize_work_envelope(
    card: dict[str, Any],
    *,
    labels: list[str],
    lane: str,
    model: str,
    seat: str | None,
    evidence_root: Path,
) -> dict[str, Any]:
    """Validate declared hashes and local paths before composing prompt metadata.

    Every admitted *_sha256 link declares required bytes. Local evidence and
    evidence_path links require evidence_sha256 or candidate_evidence_sha256,
    or an inline '#sha256=<digest>' / ' sha256=<digest>' suffix. Paths must be
    absolute or relative to the authorized evidence root, within one producer.
    PR URLs and source scopes are metadata, never artifact verification.
    """
    card_id = str(card.get("id") or "")
    raw_links = card.get("links", {})
    if not isinstance(raw_links, dict):
        raise ValueError("invalid links")
    for key, value in raw_links.items():
        if not isinstance(key, str) or key != key.strip():
            raise ValueError("invalid link key")
        if SENSITIVE_RE.search(key):
            continue
        if key in SAFE_LINK_KEYS or key.endswith(("_sha256", "_commit", "_tree")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("empty or malformed link")
            if key.endswith("_sha256") and not SHA256_RE.fullmatch(value.lower()):
                raise ValueError("invalid digest")
            if key.endswith(("_commit", "_tree")) and not SHA1_RE.fullmatch(value.lower()):
                raise ValueError("invalid revision")
    links = safe_brief_links(raw_links)
    parents = sorted(label[7:] for label in labels if re.fullmatch(r"parent-[0-9a-f]{8}", label))
    required_digests = {value.lower() for key, value in links.items() if key.endswith("_sha256")}
    explicit_paths = []
    for key in ("evidence", "evidence_path"):
        if key not in links:
            continue
        value = links[key]
        inline = re.fullmatch(r"(.+?)(?:#|\s)sha256=([0-9a-fA-F]{64})", value)
        digest = links.get("evidence_sha256") or links.get("candidate_evidence_sha256")
        if inline:
            value, inline_digest = inline.groups()
            if digest and digest.lower() != inline_digest.lower():
                raise ValueError("conflicting artifact digest")
            digest = inline_digest
        if not digest or len(parents) != 1 or "://" in value:
            raise ValueError("unverified artifact path")
        root = evidence_root.resolve(strict=True)
        path = Path(value)
        path = path if path.is_absolute() else root / path
        relative = path.relative_to(root)
        if relative.parts[:2] != ("work", parents[0]) or ".." in relative.parts:
            raise ValueError("artifact outside producer")
        if not path.resolve(strict=True).is_relative_to(root / "work" / parents[0]):
            raise ValueError("artifact realpath outside producer")
        with _directory_beneath(root, relative.parts[:-1]) as directory:
            actual = _file_digest(directory, relative.name)
        if actual != digest.lower():
            raise ValueError("artifact digest mismatch")
        required_digests.add(actual)
        explicit_paths.append((actual, str(path)))
    evidence_paths: dict[str, str] = {}
    if required_digests and len(parents) == 1:
        evidence_paths = _artifact_paths(evidence_root, parents[0], required_digests)
    evidence_paths.update(explicit_paths)
    missing = sorted(required_digests - set(evidence_paths))
    if missing:
        raise BriefEvidenceError(
            {
                "card_id": card_id,
                "kind": "worker-brief-evidence-missing",
                "missing_paths": [
                    {
                        "expected_root": str(
                            evidence_root
                            / "work"
                            / (parents[0] if len(parents) == 1 else "<single-parent-required>")
                        ),
                        "sha256": digest,
                    }
                    for digest in missing
                ],
            }
        )
    envelope = {
        "acceptance_criteria": [str(value) for value in card.get("acceptance_criteria") or []],
        "card_id": card_id,
        "dependencies": [str(value) for value in card.get("dependencies") or []],
        "description": str(card.get("description") or ""),
        "evidence_links": links,
        "evidence_paths": evidence_paths,
        "omitted_link_keys": sorted(
            key for key in raw_links if key in SAFE_LINK_KEYS and key not in links
        ),
        "kind": str(card.get("kind") or "task"),
        "model_policy": {
            "lane": lane,
            "model": model,
            "qwen_first_exclusive": "qwen-first-exclusive" in labels,
            "seat": seat,
        },
        "producer_identity": links.get("producer_identity") or str(card.get("created_by") or ""),
        "prohibited_actions": list(DEFAULT_PROHIBITED_ACTIONS),
        "route_and_seat_labels": sorted(labels),
        "source_scope": [
            links[key] for key in ("repository_scope", "source_scope") if key in links
        ],
        "title": str(card.get("title") or ""),
    }
    if _unsafe_content(envelope):
        raise ValueError("worker brief contains credential-shaped content")
    return envelope


def format_work_envelope(envelope: dict[str, Any]) -> str:
    """Return stable bytes for inclusion in a worker prompt."""
    return json.dumps(envelope, indent=2, sort_keys=True) + "\n"
