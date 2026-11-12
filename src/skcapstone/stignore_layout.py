"""Shim plus shared-include layout for the sovereign folder's Syncthing ignore rules.

``~/.skcapstone`` is one Syncthing folder shared by every node. Syncthing never
syncs ``.stignore`` itself, so a per-node copy of the rules drifts: the ``.stignore``
on one node lacked the ``/secrets`` rule another had gained, and nothing noticed.
The layout this module implements (sk-standards ``SYNCTHING_IGNORE_STANDARD``)
puts the shared rules in ONE synced file and makes every node's ``.stignore`` a
three-line shim that includes it::

    // Node-local ignore rules. Shared rules live in .stignore-shared (synced).
    // Add a rule here only if it must apply to THIS node alone.
    #include .stignore-shared

Three kinds of ``.stignore`` exist, and :func:`classify` tells them apart:

``shim``
    Contains the include line. The standard layout.
``worker``
    No include, an allowlist (``!/path`` lines closed by a catch-all ``**``). A
    GPU worker holds no sovereign state and replicates only a few paths, so it is
    deliberately NOT a shim and the shared file is never replicated to it.
``legacy``
    No include, no allowlist: a full ruleset pasted into one node's file. This is
    the drifting layout the standard replaces.

The failure mode that dictates the install order: when ``.stignore`` includes a
file that does not exist, Syncthing refuses to load the ignore patterns and the
folder errors. So :func:`install` writes ``.stignore-shared`` FIRST and the shim
second, and never writes a shim over a missing shared file.

This module is stdlib-only on purpose. It is imported by the installer
(``skcapstone.ensure_skeleton``, ``syncthing_setup``), the sync policy audit and the
doctor, so it must not import any of them.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SHARED_NAME = ".stignore-shared"
STIGNORE_NAME = ".stignore"
INCLUDE_LINE = f"#include {SHARED_NAME}"

#: Syncthing caps include nesting; we stop well short of it and report the rest.
MAX_INCLUDE_DEPTH = 8

#: Rules whose absence from a full node's effective ruleset leaks a secret or
#: reintroduces a conflict generator that was already paid for in an incident.
CRITICAL_RULES: tuple[str, ...] = (
    "/secrets",
    "*.key",
    "*.pem",
    "**/private.*",
    "*.env",
    "config/*.env",
    "**/skwhisper/state.json",
    "**/skwhisper/whisper.md",
    "**/skwhisper/health.json",
    "**/skwhisper/patterns.json",
)

#: The worker allowlist ignores everything it does not name, so only the rules that
#: guard material inside the paths it DOES replicate (config, identity, capauth) matter.
WORKER_CRITICAL_RULES: tuple[str, ...] = ("*.key", "*.pem", "**/private.*", "*.env")

_PREFIX_RE = re.compile(r"^(?:\(\?[a-z]+\))+")


# ---------------------------------------------------------------------------
# Packaged data
# ---------------------------------------------------------------------------


def _defaults_dir() -> Path:
    return Path(__file__).resolve().parent / "defaults"


def packaged_shared_text() -> str:
    """The canonical ``.stignore-shared`` shipped as package data."""
    return (_defaults_dir() / SHARED_NAME).read_text(encoding="utf-8")


def packaged_shim_text() -> str:
    """The skeleton shim shipped as package data (``defaults/.stignore``)."""
    return (_defaults_dir() / STIGNORE_NAME).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def pattern_lines(text: str) -> list[str]:
    """Ignore patterns in a file: no blanks, ``//`` comments or ``#include`` directives."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#include"):
            continue
        out.append(line)
    return out


def _is_include(line: str) -> bool:
    return line.strip() == INCLUDE_LINE


def has_include(text: str) -> bool:
    """True when the text includes the shared file."""
    return any(_is_include(raw) for raw in text.splitlines())


def classify(text: str | None) -> str:
    """Classify a ``.stignore`` body as ``absent``, ``shim``, ``worker`` or ``legacy``."""
    if text is None:
        return "absent"
    if has_include(text):
        return "shim"
    lines = pattern_lines(text)
    allows = any(_PREFIX_RE.sub("", ln).startswith("!/") for ln in lines)
    if allows and "**" in lines:
        return "worker"
    return "legacy"


def _key(rule: str) -> str:
    """Comparison key for a rule: flag prefixes and a leading slash do not matter.

    Syncthing reports expanded rules in its own normal form, so the doctor compares
    keys, not raw lines. Dropping the leading slash widens ``/secrets`` to
    ``secrets`` at any depth, which only errs toward "present", the safe direction
    for a rule that guards a secret being judged by name.
    """
    return _PREFIX_RE.sub("", rule.strip()).lstrip("/")


# ---------------------------------------------------------------------------
# Include expansion (the effective ruleset Syncthing would load)
# ---------------------------------------------------------------------------


def _expand(path: Path, root: Path, depth: int, missing: list[str]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        missing.append(path.name)
        return []
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#include"):
            rel = stripped[len("#include") :].strip()
            target = (path.parent / rel).resolve() if rel else None
            if target is None or not target.is_relative_to(root.resolve()):
                missing.append(rel or "(empty include)")
                continue
            if depth >= MAX_INCLUDE_DEPTH:
                missing.append(f"{rel} (include nesting deeper than {MAX_INCLUDE_DEPTH})")
                continue
            out.extend(_expand(target, root, depth + 1, missing))
            continue
        out.append(raw)
    return out


def read_effective_text(root: Path) -> tuple[str, list[str]]:
    """Read ``<root>/.stignore`` with ``#include`` lines expanded in place.

    Order is preserved, which matters because Syncthing is first-match-wins.

    Returns:
        ``(text, missing)``: the expanded text and the include targets that could not
        be read. A missing include makes Syncthing error the folder, so callers must
        treat it as a finding, never as "no extra rules".

    Raises:
        OSError: ``<root>/.stignore`` itself is unreadable.
    """
    stignore = root / STIGNORE_NAME
    stignore.read_text(encoding="utf-8")  # raise OSError for an absent or unreadable file
    missing: list[str] = []
    lines = _expand(stignore, root, 0, missing)
    return "\n".join(lines) + "\n", missing


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    """What :func:`install` did, and what it found but deliberately left alone.

    Attributes:
        shared: ``written``, ``present``, ``skipped`` (worker) or ``error``.
        stignore: ``written`` (a new shim), ``shim`` (already one), ``worker`` (left
            alone), ``legacy`` (left alone), ``migrated``, ``refused`` (migration
            declined, file untouched) or ``error``.
        backup: Backup of a legacy ``.stignore``, when one was taken.
        gained: Shared patterns a legacy file lacks (what migrating would add).
        local_only: Legacy patterns that are not in the shared file (they would be
            kept node-local on migration).
        messages: Human-readable lines for the caller to log or print.
    """

    shared: str = "present"
    stignore: str = "shim"
    backup: Path | None = None
    gained: list[str] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when a file under the root was written."""
        return self.shared == "written" or self.stignore in ("written", "migrated")


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary sibling so Syncthing never reads a half-written file."""
    tmp = path.with_name(f"{path.name}.part-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_legacy(stignore: Path, text: str) -> Path | None:
    """Back up a legacy ``.stignore`` once: an identical existing backup is reused.

    ``ensure_skeleton`` runs on every daemon start, so a fresh backup per call
    would be a new stray file per start.
    """
    for existing in sorted(stignore.parent.glob(f"{STIGNORE_NAME}.bak-*")):
        try:
            if existing.read_text(encoding="utf-8") == text:
                return existing
        except OSError:
            continue
    backup = stignore.with_name(f"{STIGNORE_NAME}.bak-{_stamp()}")
    try:
        backup.write_text(text, encoding="utf-8")
    except OSError:
        return None
    return backup


def _migrated_text(legacy: str, shared_patterns: set[str]) -> str:
    """Shim header plus the legacy lines the shared file does not already cover.

    A kept pattern keeps the comment lines directly above it, because those comments
    are the only record of why the rule exists.
    """
    kept: list[str] = []
    pending: list[str] = []
    for raw in legacy.splitlines():
        line = raw.strip()
        if not line:
            pending = []
        elif line.startswith("//"):
            pending.append(raw)
        elif line in shared_patterns:
            pending = []
        else:
            kept.extend(pending)
            kept.append(raw)
            pending = []
    head = packaged_shim_text().rstrip("\n")
    if not kept:
        return head + "\n"
    return (
        head
        + "\n\n// Node-local rules carried over from the previous .stignore by"
        + " `skcapstone sync stignore --migrate`.\n"
        + "\n".join(kept)
        + "\n"
    )


def install(root: Path, *, migrate: bool = False) -> InstallResult:
    """Install the shim plus shared layout under a sovereign root. Never destructive.

    Order is load-bearing: the shared file is written before any shim that includes
    it, because a shim over a missing include errors the Syncthing folder.

    * no ``.stignore``       -> write ``.stignore-shared`` if absent, then the shim
    * already a shim         -> write ``.stignore-shared`` if absent; touch nothing else
    * worker allowlist       -> leave everything alone (no shared file is created)
    * legacy (no include)    -> write ``.stignore-shared`` if absent, back the old file
      up, report the difference, leave ``.stignore`` untouched. With ``migrate`` it
      becomes a shim that keeps every legacy rule not already in the shared file,
      except when the legacy file has a ``!`` rule: order decides whether a negation
      applies, so moving lines around an include could silently change the meaning
      and migration is refused instead.

    Args:
        root: The sovereign folder root (``~/.skcapstone``).
        migrate: Rewrite a legacy ``.stignore`` as a shim, keeping node-local rules.

    Returns:
        An :class:`InstallResult`. Filesystem errors are reported in it, not raised,
        because the callers (skeleton creation) are best-effort.
    """
    result = InstallResult()
    stignore = root / STIGNORE_NAME
    shared = root / SHARED_NAME

    try:
        text = stignore.read_text(encoding="utf-8") if stignore.exists() else None
    except OSError as exc:
        result.shared = result.stignore = "error"
        result.messages.append(f"{stignore} is unreadable, left alone: {exc}")
        return result

    kind = classify(text)
    if kind == "worker":
        result.shared = "skipped"
        result.stignore = "worker"
        result.messages.append("allowlist (worker) .stignore found: left alone, no shared file")
        return result

    try:
        root.mkdir(parents=True, exist_ok=True)
        if shared.exists():
            result.shared = "present"
        else:
            _atomic_write(shared, packaged_shared_text())
            result.shared = "written"
            result.messages.append(f"wrote {shared}")
    except OSError as exc:
        result.shared = result.stignore = "error"
        result.messages.append(f"could not write {shared}: {exc}")
        return result

    try:
        if kind == "absent":
            _atomic_write(stignore, packaged_shim_text())
            result.stignore = "written"
            result.messages.append(f"wrote shim {stignore}")
        elif kind == "shim":
            result.stignore = "shim"
        else:
            _handle_legacy(stignore, shared, text or "", migrate, result)
    except OSError as exc:
        result.stignore = "error"
        result.messages.append(f"could not update {stignore}: {exc}")
    return result


def _handle_legacy(
    stignore: Path, shared: Path, text: str, migrate: bool, result: InstallResult
) -> None:
    shared_patterns = set(pattern_lines(shared.read_text(encoding="utf-8")))
    legacy_patterns = pattern_lines(text)
    result.local_only = sorted({p for p in legacy_patterns if p not in shared_patterns})
    result.gained = sorted(shared_patterns - set(legacy_patterns))
    result.backup = _backup_legacy(stignore, text)

    if not migrate:
        result.stignore = "legacy"
        result.messages.append(
            f"{stignore} is not a shim: left untouched. It lacks {len(result.gained)} shared "
            f"rule(s) and has {len(result.local_only)} rule(s) the shared file does not. "
            "Run `skcapstone sync stignore --migrate` to convert it."
        )
        return
    if result.backup is None:
        result.stignore = "refused"
        result.messages.append("could not back up the old .stignore, so it was not migrated")
        return
    if any(_PREFIX_RE.sub("", p).startswith("!") for p in legacy_patterns):
        result.stignore = "refused"
        result.messages.append(
            "migration refused: the file has a `!` rule and rule order decides whether it "
            "applies, so it must be rearranged by hand around the include"
        )
        return
    _atomic_write(stignore, _migrated_text(text, shared_patterns))
    result.stignore = "migrated"
    result.messages.append(
        f"migrated {stignore} to a shim; {len(result.local_only)} node-local rule(s) kept, "
        f"old file saved as {result.backup.name}"
    )


# ---------------------------------------------------------------------------
# Effective rules from the Syncthing daemon
# ---------------------------------------------------------------------------

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _syncthing_endpoint() -> tuple[str | None, str | None]:
    """GUI base URL and API key: environment first, then ``~/.config/syncthing``."""
    base = os.environ.get("SYNCTHING_API_URL") or None
    key = os.environ.get("SYNCTHING_API_KEY") or None
    if not (base and key):
        try:
            from .service_health import _load_syncthing_config

            found_base, found_key = _load_syncthing_config()
        except Exception:  # noqa: BLE001  best-effort discovery
            found_base = found_key = None
        base = base or found_base
        key = key or found_key
    return base, key


def _folder_id(root: Path) -> str | None:
    """The Syncthing folder id whose path is ``root``, or None when none is."""
    try:
        from .sync_policy.discovery import discover_folders

        folders, _ = discover_folders(Path.home())
        resolved = root.resolve()
        for folder in folders:
            if folder.path == resolved:
                return folder.folder_id
    except Exception:  # noqa: BLE001  best-effort discovery
        pass
    return None


def fetch_effective_rules(root: Path, *, timeout: float = 3.0) -> tuple[list[str] | None, str]:
    """Ask the local Syncthing daemon for the folder's effective, expanded rules.

    ``GET /rest/db/ignores`` returns the rules with every ``#include`` expanded, which
    is the one answer that reflects what the daemon actually loaded. The API key is
    only ever sent to a loopback address.

    Returns:
        ``(rules, note)``. ``rules`` is None when the daemon could not answer; ``note``
        says why, or carries the daemon's own ignore-load error.
    """
    base, key = _syncthing_endpoint()
    if not base or not key:
        return None, "no Syncthing API endpoint or key configured"
    parsed = urllib.parse.urlparse(base if "://" in base else f"http://{base}")
    if (parsed.hostname or "") not in _LOOPBACK:
        return None, "Syncthing API is not on loopback, not contacted"
    folder = _folder_id(root)
    if folder is None:
        # Never answer for a root the daemon does not serve: a test or a second home
        # would otherwise be judged on some other folder's rules.
        return None, f"no Syncthing folder is rooted at {root}"
    url = f"{parsed.scheme}://{parsed.netloc}/rest/db/ignores?" + urllib.parse.urlencode(
        {"folder": folder}
    )
    context = None
    if parsed.scheme == "https":
        # Loopback only: the daemon's GUI certificate is self-signed by default.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"Syncthing answered HTTP {exc.code} for folder {folder}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"Syncthing API unreachable: {exc}"
    if not isinstance(payload, dict):
        return None, "Syncthing API returned an unexpected payload"
    if payload.get("error"):
        return None, f"Syncthing reports an ignore error for folder {folder}: {payload['error']}"
    rules = payload.get("expanded") or payload.get("ignore") or []
    return [str(r) for r in rules], f"syncthing API, folder {folder}"


# ---------------------------------------------------------------------------
# Diagnosis (report only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One thing the doctor noticed. ``code`` is stable, ``detail`` is for people."""

    code: str
    severity: str  # "error" | "warn"
    detail: str


@dataclass
class Diagnosis:
    """The result of :func:`diagnose`.

    Attributes:
        kind: ``absent``, ``shim``, ``worker`` or ``legacy``.
        source: Where the effective rules came from: the Syncthing API or the files.
        findings: Every problem found; empty means the node conforms.
        note: Why the API was not used, or which folder it answered for.
    """

    kind: str
    source: str
    findings: list[Finding] = field(default_factory=list)
    note: str = ""

    def codes(self) -> set[str]:
        """The finding codes, for quick membership tests."""
        return {f.code for f in self.findings}


def _critical_missing(rules: list[str], kind: str) -> list[str]:
    keys = [_key(r) for r in rules]
    if kind == "worker":
        # classify() only returns "worker" when the catch-all `**` is present.
        missing = []
        first_allow = next(
            (i for i, r in enumerate(rules) if _PREFIX_RE.sub("", r).startswith("!")), len(rules)
        )
        for wanted in WORKER_CRITICAL_RULES:
            if _key(wanted) not in keys[:first_allow]:
                missing.append(wanted)
        return missing
    return [wanted for wanted in CRITICAL_RULES if _key(wanted) not in keys]


def _rules_from_files(root: Path, missing_includes: list[str]) -> list[str]:
    try:
        text, missing = read_effective_text(root)
    except OSError:
        return []
    missing_includes.extend(missing)
    return pattern_lines(text)


def diagnose(root: Path, *, use_api: bool = True) -> Diagnosis:
    """Check a sovereign root against the standard. Reads only, changes nothing.

    Args:
        root: The sovereign folder root.
        use_api: Read the effective rules from the Syncthing daemon when it answers,
            falling back to parsing the files.
    """
    stignore = root / STIGNORE_NAME
    shared = root / SHARED_NAME
    try:
        text = stignore.read_text(encoding="utf-8") if stignore.exists() else None
    except OSError:
        text = None
    kind = classify(text)
    findings: list[Finding] = []

    if kind == "absent":
        findings.append(Finding("not_shim", "error", f"{stignore} is missing"))
    elif kind == "legacy":
        findings.append(
            Finding(
                "not_shim",
                "error",
                ".stignore is not a shim (no `#include .stignore-shared`), so its rules drift "
                "from the other nodes; run `skcapstone sync stignore --migrate`",
            )
        )

    packaged = None
    if kind != "worker":
        if not shared.is_file():
            findings.append(
                Finding(
                    "shared_missing",
                    "error",
                    f"{shared} is missing; Syncthing errors the folder while the include "
                    "dangles. Run `skcapstone sync stignore`",
                )
            )
        else:
            try:
                installed = shared.read_text(encoding="utf-8")
                packaged = packaged_shared_text()
            except OSError:
                installed = packaged = None
            if installed is not None and packaged is not None and installed != packaged:
                only_installed = len(set(pattern_lines(installed)) - set(pattern_lines(packaged)))
                only_packaged = len(set(pattern_lines(packaged)) - set(pattern_lines(installed)))
                findings.append(
                    Finding(
                        "shared_drift",
                        "warn",
                        f".stignore-shared differs from the packaged canonical version "
                        f"({only_packaged} rule(s) only packaged, {only_installed} only here); "
                        "report only, reconcile by editing the shared file once",
                    )
                )

    rules: list[str] | None = None
    source, note = "files", ""
    if use_api and kind != "absent":
        rules, note = fetch_effective_rules(root)
        if rules is not None:
            source = "syncthing-api"
        elif note.startswith("Syncthing reports an ignore error"):
            findings.append(Finding("daemon_ignore_error", "error", note))
    if rules is None:
        missing_includes: list[str] = []
        rules = _rules_from_files(root, missing_includes)
        if missing_includes and kind == "shim" and shared.is_file():
            findings.append(
                Finding("include_unreadable", "error", f"cannot read: {', '.join(missing_includes)}")
            )

    if kind != "absent":
        absent = _critical_missing(rules, kind)
        if absent:
            findings.append(
                Finding(
                    "critical_missing",
                    "error",
                    "critical rule(s) absent from the effective ruleset: " + ", ".join(absent),
                )
            )
    return Diagnosis(kind=kind, source=source, findings=findings, note=note)
