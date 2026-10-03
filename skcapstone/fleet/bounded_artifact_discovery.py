"""Bounded SHA256 evidence lookup for fleet review workers.

Reviewers must verify candidate digests against reachable bytes. Unbounded
recursive hashing of ``~/.skcapstone`` (or the whole host) is a throughput
defect: card 4cd4dd62 spent 9h51m scanning the estate after a dependency
blocker was already recorded.

This module resolves digests from:

1. Explicit paths referenced on the card (links, criteria prose, meta).
2. Bounded configured roots (default: ``evidence/work/<card_id>/`` for the
   review card and any named producer/dependency cards).

It never walks the estate home, worktrees, or arbitrary filesystem trees.
Termination is deterministic via file-count and wall-clock budgets.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_DIGEST_RE = re.compile(r"\b([0-9a-f]{64})\b", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?:^|[\s\"'=])("
    r"~?/?(?:[\w.-]+/)*[\w.-]+\.(?:json|md|txt|patch|diff|tar|gz|tgz|zip|bin)"
    r"|/?home/[\w./-]+"
    r"|~/?\.skcapstone/evidence/[\w./-]+"
    r")",
    re.IGNORECASE,
)
_CARD_ID_RE = re.compile(r"\b(?:card:)?([0-9a-f]{8})\b", re.IGNORECASE)

DEFAULT_MAX_FILES = 256
DEFAULT_MAX_SECONDS = 5.0
DEFAULT_MAX_BYTES_PER_FILE = 32 * 1024 * 1024


@dataclass(frozen=True)
class DiscoveryBounds:
    """Hard limits for one discovery attempt."""

    max_files: int = DEFAULT_MAX_FILES
    max_seconds: float = DEFAULT_MAX_SECONDS
    max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE

    def __post_init__(self) -> None:
        if self.max_files < 1:
            raise ValueError("max_files must be >= 1")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be > 0")
        if self.max_bytes_per_file < 1:
            raise ValueError("max_bytes_per_file must be >= 1")


@dataclass
class DiscoveryResult:
    """Outcome of one bounded digest search."""

    digests: dict[str, str | None] = field(default_factory=dict)
    files_examined: int = 0
    bytes_hashed: int = 0
    roots_used: list[str] = field(default_factory=list)
    stopped_reason: str = "complete"
    elapsed_seconds: float = 0.0

    @property
    def bounded(self) -> bool:
        """True when discovery stopped for a budget, not for an estate walk."""
        return self.stopped_reason in {
            "complete",
            "max_files",
            "max_seconds",
            "missing_digest",
        }


def extract_digests(*texts: object) -> tuple[str, ...]:
    """Return unique lowercase SHA256 digests found in freeform text."""
    found: list[str] = []
    for text in texts:
        for match in _DIGEST_RE.finditer(str(text or "")):
            digest = match.group(1).lower()
            if digest not in found:
                found.append(digest)
    return tuple(found)


def extract_referenced_paths(*texts: object) -> tuple[str, ...]:
    """Return unique path-like references found in freeform text."""
    found: list[str] = []
    for text in texts:
        for match in _PATH_RE.finditer(str(text or "")):
            path = match.group(1).strip().rstrip(".,;:\"'")
            if path and path not in found:
                found.append(path)
    return tuple(found)


def extract_card_ids(*texts: object) -> tuple[str, ...]:
    """Return unique eight-hex card ids mentioned in freeform text."""
    found: list[str] = []
    for text in texts:
        for match in _CARD_ID_RE.finditer(str(text or "")):
            card_id = match.group(1).lower()
            if card_id not in found:
                found.append(card_id)
    return tuple(found)


def default_work_roots(home: Path, card_ids: Sequence[str]) -> tuple[Path, ...]:
    """Return the default bounded evidence roots for named cards."""
    base = Path(home).expanduser() / "evidence" / "work"
    return tuple(base / card_id for card_id in card_ids)


def card_reference_texts(card: Mapping[str, object] | None) -> tuple[str, ...]:
    """Flatten the card surfaces that may name digests or evidence paths."""
    if not isinstance(card, Mapping):
        return ()
    texts: list[str] = [
        str(card.get("description") or ""),
        str(card.get("title") or ""),
    ]
    criteria = card.get("acceptance_criteria") or []
    if isinstance(criteria, Sequence) and not isinstance(criteria, (str, bytes)):
        texts.extend(str(item) for item in criteria)
    for mapping_name in ("links", "meta"):
        mapping = card.get(mapping_name)
        if isinstance(mapping, Mapping):
            for key, value in mapping.items():
                texts.append(str(key))
                texts.append(str(value))
    return tuple(texts)


def _sha256_file(path: Path, max_bytes: int) -> str | None:
    """Hash one regular file, refusing oversized or unreadable paths."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        size = path.stat().st_size
        if size > max_bytes:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _iter_root_files(root: Path) -> Iterable[Path]:
    """Yield regular files under one root without following directory symlinks."""
    if root.is_file() and not root.is_symlink():
        yield root
        return
    if not root.is_dir() or root.is_symlink():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def find_digests(
    digests: Sequence[str],
    *,
    explicit_paths: Sequence[str | Path] = (),
    roots: Sequence[str | Path] = (),
    bounds: DiscoveryBounds | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DiscoveryResult:
    """Locate digests only under explicit paths and configured roots.

    Args:
        digests: SHA256 hex digests to locate.
        explicit_paths: Exact files or directories named by the card.
        roots: Bounded configured roots (for example evidence/work/<id>/).
        bounds: Time and file-count limits.
        clock: Monotonic clock used for the time budget.

    Returns:
        DiscoveryResult mapping each digest to a found path or None.
    """
    limits = bounds or DiscoveryBounds()
    wanted = {
        digest.lower()
        for digest in digests
        if isinstance(digest, str) and _DIGEST_RE.fullmatch(digest.strip())
    }
    result = DiscoveryResult(digests={digest: None for digest in sorted(wanted)})
    if not wanted:
        result.stopped_reason = "missing_digest"
        return result

    started = float(clock())
    remaining = set(wanted)
    seen: set[Path] = set()

    def budget_exhausted() -> str | None:
        if result.files_examined >= limits.max_files:
            return "max_files"
        if float(clock()) - started >= limits.max_seconds:
            return "max_seconds"
        return None

    def consider(path: Path) -> bool:
        """Hash one path; return False when a budget stops discovery."""
        resolved = path.expanduser()
        try:
            resolved = resolved.resolve(strict=False)
        except OSError:
            return True
        if resolved in seen:
            return True
        seen.add(resolved)
        stop = budget_exhausted()
        if stop:
            result.stopped_reason = stop
            return False
        digest = _sha256_file(resolved, limits.max_bytes_per_file)
        result.files_examined += 1
        if digest is None:
            return True
        try:
            result.bytes_hashed += resolved.stat().st_size
        except OSError:
            pass
        if digest in remaining:
            result.digests[digest] = str(resolved)
            remaining.remove(digest)
        return True

    search_roots: list[Path] = []
    for raw in list(explicit_paths) + list(roots):
        path = Path(raw).expanduser()
        search_roots.append(path)
        result.roots_used.append(str(path))

    for root in search_roots:
        if not remaining:
            break
        if root.is_file():
            if not consider(root):
                result.elapsed_seconds = float(clock()) - started
                return result
            continue
        for file_path in _iter_root_files(root):
            if not remaining:
                break
            if not consider(file_path):
                result.elapsed_seconds = float(clock()) - started
                return result

    result.elapsed_seconds = float(clock()) - started
    if remaining and result.stopped_reason == "complete":
        result.stopped_reason = "complete"
    return result


def discover_card_artifacts(
    home: Path,
    card: Mapping[str, object],
    *,
    extra_roots: Sequence[str | Path] = (),
    bounds: DiscoveryBounds | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DiscoveryResult:
    """Discover digests named by a card using only referenced and work roots.

    Args:
        home: Estate home (``~/.skcapstone``).
        card: Folded card mapping with links/meta/criteria.
        extra_roots: Additional configured roots (still bounded by budgets).
        bounds: Time and file-count limits.
        clock: Monotonic clock used for the time budget.

    Returns:
        DiscoveryResult for every digest mentioned on the card.
    """
    texts = card_reference_texts(card)
    digests = extract_digests(*texts)
    explicit = [Path(path).expanduser() for path in extract_referenced_paths(*texts)]
    links = card.get("links") if isinstance(card.get("links"), Mapping) else {}
    meta = card.get("meta") if isinstance(card.get("meta"), Mapping) else {}
    for mapping in (links, meta):
        for key in (
            "evidence",
            "candidate_path",
            "artifact_path",
            "candidate_evidence",
            "reverse_patch",
        ):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                explicit.append(Path(value).expanduser())

    card_id = str(card.get("id") or "").strip().lower()
    named_cards = list(extract_card_ids(*texts))
    if card_id and re.fullmatch(r"[0-9a-f]{8}", card_id) and card_id not in named_cards:
        named_cards.insert(0, card_id)
    for key in ("link_source_card", "parent", "source_card"):
        value = str(meta.get(key) or links.get(key) or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}", value) and value not in named_cards:
            named_cards.append(value)

    roots = list(default_work_roots(home, named_cards))
    roots.extend(Path(root).expanduser() for root in extra_roots)
    return find_digests(
        digests,
        explicit_paths=explicit,
        roots=roots,
        bounds=bounds,
        clock=clock,
    )
