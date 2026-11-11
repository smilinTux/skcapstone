"""Pinned standalone binaries an install profile can require (`binaries` items).

Packages and units cover what the SK repos ship. Some fleet tooling is a
third-party static binary instead (worktrunk's `wt` was the first, gitleaks the second). This
module is the catalog of those: an exact version and a sha256 per
architecture, both pinned IN CODE, so a binary only changes on a node when a
reviewed commit changes it here. There is no "latest" and no trust in the
release's own checksum file: the hash the backend verifies is the one below.

Presence is version-exact. A node with `wt` at an older version reads as
missing the required binary, so bumping `version` here IS the upgrade path:
the next `skfleet install --apply` replaces it.

Layout: binaries land in ``~/.local/bin`` (on PATH for the user manager and
for bash on every node). A catalog entry may also lay down a user config file,
written only when absent or when it still carries the sk-managed marker, so an
operator who takes a file over by deleting the marker keeps their edits.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

#: Marker line that makes a laid-down config file fleet-owned.
SK_MANAGED = "# sk-managed"

_WORKTRUNK_CONFIG = f"""{SK_MANAGED}: written by skfleet install (binaries: wt).
# Delete the line above to take this file over.
# One flat root for every agent worktree (AGENTS.md "Worktrees" convention).
worktree-path = "~/skworld-worktrees/{{{{ repo }}}}.{{{{ branch | sanitize }}}}"

# Merging happens through PRs + CI (the release tag must be the tested commit).
# `wt merge` fast-forwards a LOCAL default branch and would bypass review.
pre-merge = "echo 'wt merge is off on SK repos: push and open a PR (gh pr create)' >&2; exit 1"

[list]
timeout-ms = 20000

[remove]
# Only branches worktrunk proves integrated (squash-aware); -D is needed otherwise.
delete-branch = true
"""


@dataclass(frozen=True)
class Binary:
    """One pinned binary release.

    Attributes:
        name: Catalog key and the executable probed for its version.
        version: Exact version; presence requires `<name> --version` to report it.
        url: Release asset URL template, `{arch}` substituted.
        sha256: Pinned archive hash per `platform.machine()` value.
        members: Executables extracted from the archive into the bin dir.
        config: Optional (path relative to $HOME, content) user config.
        post_install: Names of best-effort steps in POST_INSTALL run after a
            successful install. A failed step is reported, never fatal: the
            binary itself is installed and verified by then.
        asset_arch: Optional `platform.machine()` -> release-asset arch word,
            for upstreams that do not name assets by the kernel's spelling
            (gitleaks uses x64 and arm64). Unmapped arches pass through.
    """

    name: str
    version: str
    url: str
    sha256: dict[str, str]
    members: tuple[str, ...]
    config: tuple[str, str] | None = None
    post_install: tuple[str, ...] = ()
    asset_arch: dict[str, str] | None = None


CATALOG: dict[str, Binary] = {
    "wt": Binary(
        name="wt",
        version="0.81.0",
        url=(
            "https://github.com/max-sixty/worktrunk/releases/download/v0.81.0/"
            "worktrunk-{arch}-unknown-linux-musl.tar.xz"
        ),
        sha256={
            "x86_64": "0c253f216985ce9cd7b202a11606b5ce6aa934b1019fd1fbb9b7ce2b47b1c74a",
            "aarch64": "7a334d2e64c577afd0247e2411eff673360ab1f18897564f2d2e5d5374fbd74b",
        },
        members=("wt", "git-wt"),
        config=(".config/worktrunk/config.toml", _WORKTRUNK_CONFIG),
        post_install=("worktree-root", "claude-plugin"),
    ),
    # Filesystem secret sweep (docs/fleet/GITLEAKS.md). v8.30.1, released
    # 2026-03-21; hashes from the release's gitleaks_8.30.1_checksums.txt and
    # re-verified against the downloaded archives.
    "gitleaks": Binary(
        name="gitleaks",
        version="8.30.1",
        url=(
            "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
            "gitleaks_8.30.1_linux_{arch}.tar.gz"
        ),
        sha256={
            "x86_64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
            "aarch64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
        },
        members=("gitleaks",),
        asset_arch={"x86_64": "x64", "aarch64": "arm64"},
    ),
}


#: Per-node override for where worktrees physically live. The wt config always
#: says ~/skworld-worktrees; on a node with a fast work volume (chiap08:
#: /srv/work on NVMe) that path becomes a symlink to this root instead.
WORKTREE_ROOT_ENV = "SKFLEET_WORKTREE_ROOT"

#: The worktrunk Claude Code plugin, vendored at the pinned tag (see VENDORED.md).
CLAUDE_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "data" / "worktrunk-claude-plugin"
CLAUDE_MARKETPLACE = "sk-worktrunk"


def ensure_worktree_root(home: Path, root: str | None = None) -> str:
    """Make ~/skworld-worktrees exist, as a symlink to SKFLEET_WORKTREE_ROOT if set.

    Never moves or deletes anything: an existing non-empty directory that is
    not already the requested symlink is left alone and reported.

    Returns:
        A short note for the install detail.
    """
    link = home / "skworld-worktrees"
    target = (root if root is not None else os.environ.get(WORKTREE_ROOT_ENV, "")).strip()
    if not target:
        link.mkdir(parents=True, exist_ok=True)
        return f"worktree root {link}"
    dest = Path(target).expanduser()
    if link.is_symlink() and Path(os.readlink(link)) == dest:
        return f"worktree root {link} -> {dest}"
    if link.exists() and not link.is_symlink() and any(link.iterdir()):
        return f"worktree root {link} is a non-empty dir; NOT relinked to {dest} (move it by hand)"
    dest.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        link.rmdir()
    link.symlink_to(dest)
    return f"worktree root {link} -> {dest}"


def install_claude_plugin(run: Callable = subprocess.run) -> str:
    """Install the vendored worktrunk plugin into Claude Code, if Claude is here.

    Idempotent: "already added/installed" answers are fine. Returns a note.
    """
    claude = shutil.which("claude")
    if claude is None:
        return "claude-plugin skipped (no claude on PATH)"
    if not (CLAUDE_PLUGIN_DIR / ".claude-plugin" / "marketplace.json").is_file():
        return f"claude-plugin skipped (vendored plugin missing at {CLAUDE_PLUGIN_DIR})"
    steps = (
        [claude, "plugin", "marketplace", "add", str(CLAUDE_PLUGIN_DIR), "--scope", "user"],
        [claude, "plugin", "install", f"worktrunk@{CLAUDE_MARKETPLACE}", "--scope", "user"],
    )
    for cmd in steps:
        try:
            out = run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except Exception as exc:
            return f"claude-plugin failed: {exc}"
        text = f"{getattr(out, 'stdout', '')}{getattr(out, 'stderr', '')}".lower()
        if getattr(out, "returncode", 1) != 0 and "already" not in text:
            return f"claude-plugin failed at `{' '.join(cmd[1:4])}`: {text.strip()[-200:]}"
    return f"claude-plugin worktrunk@{CLAUDE_MARKETPLACE} installed"


POST_INSTALL: dict[str, Callable[..., str]] = {
    "worktree-root": lambda home, run: ensure_worktree_root(home),
    "claude-plugin": lambda home, run: install_claude_plugin(run),
}


def bin_dir() -> Path:
    """Where catalog binaries are installed (``SKFLEET_BIN_DIR`` overrides)."""
    env = os.environ.get("SKFLEET_BIN_DIR", "").strip()
    return Path(env).expanduser() if env else Path("~/.local/bin").expanduser()


def probe(
    names: list[str] | None = None,
    *,
    runner: Callable | None = None,
    directory: Path | None = None,
) -> dict[str, str]:
    """Observed versions of catalog binaries present in the bin dir.

    Degrade-safe like the rest of nodeinventory: a binary that is absent,
    not executable, or prints nothing parseable is simply not reported.

    Args:
        names: Catalog names to probe; None probes the whole catalog.
        runner: subprocess.run-compatible callable (tests inject one).
        directory: Bin dir override; defaults to bin_dir().

    Returns:
        {name: version} for each binary that answered, sorted by name.
    """
    run = runner if runner is not None else subprocess.run
    where = directory if directory is not None else bin_dir()
    found: dict[str, str] = {}
    for name in sorted(names if names is not None else CATALOG):
        exe = where / name
        if not exe.is_file():
            continue
        try:
            out = run([str(exe), "--version"], capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        if getattr(out, "returncode", 1) != 0:
            continue
        words = (getattr(out, "stdout", "") or "").split()
        if words:
            found[name] = words[-1].lstrip("v")
    return found


def pinned(name: str) -> str | None:
    """The pinned version of a catalog binary, or None if it is not catalogued."""
    entry = CATALOG.get(name)
    return entry.version if entry else None


def _fetch(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out)


def install(
    name: str,
    *,
    dry_run: bool = False,
    fetch: Callable[[str, Path], None] = _fetch,
    arch: str | None = None,
    directory: Path | None = None,
    home: Path | None = None,
    run: Callable = subprocess.run,
    post_install: bool = True,
) -> tuple[str, str]:
    """Download, verify and install one catalog binary (plus its config).

    Args:
        name: Catalog key.
        dry_run: Report what would happen; touch nothing, fetch nothing.
        fetch: Downloader (url, dest); injected by tests.
        arch: Override `platform.machine()`.
        directory: Bin dir override; defaults to bin_dir().
        home: $HOME override for the config file and post-install steps.
        run: subprocess.run-compatible callable for post-install commands.
        post_install: Run the entry's post-install steps (tests turn it off).

    Returns:
        A backend-style (status, detail): ok | would-write | failed.
    """
    entry = CATALOG.get(name)
    if entry is None:
        return "failed", f"{name}: not in the binaries catalog"
    machine = arch or platform.machine()
    expected = entry.sha256.get(machine)
    if expected is None:
        return "failed", f"{name}: no pinned asset for arch {machine}"
    url = entry.url.format(arch=(entry.asset_arch or {}).get(machine, machine))
    where = directory if directory is not None else bin_dir()
    if dry_run:
        return (
            "would-write",
            f"fetch {url} (sha256 {expected[:12]}) -> {where}/{','.join(entry.members)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "asset.tar"  # tarfile sniffs xz or gz
        try:
            fetch(url, archive)
        except Exception as exc:
            return "failed", f"{name}: download failed: {exc}"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != expected:
            return "failed", f"{name}: sha256 mismatch (got {digest}, pinned {expected})"
        extracted: dict[str, Path] = {}
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                base = Path(member.name).name
                if member.isfile() and base in entry.members:
                    src = tar.extractfile(member)
                    if src is None:
                        continue
                    target = Path(tmp) / base
                    target.write_bytes(src.read())
                    extracted[base] = target
        missing = sorted(set(entry.members) - set(extracted))
        if missing:
            return "failed", f"{name}: archive lacks {missing}"
        where.mkdir(parents=True, exist_ok=True)
        for base, path in extracted.items():
            staged = where / f".{base}.skfleet-new"
            shutil.copyfile(path, staged)
            staged.chmod(0o755)
            os.replace(staged, where / base)  # atomic: a running `wt` keeps its inode

    return "ok", finish(entry, home=home, run=run, post_install=post_install)


def finish(
    entry: Binary,
    *,
    home: Path | None = None,
    run: Callable = subprocess.run,
    post_install: bool = True,
) -> str:
    """Lay down the entry's config and run its post-install steps. Never raises.

    Split out of install() so an operator (or a node whose binary is already
    at the pin) can converge config + plugins without re-downloading.

    Returns:
        "; "-joined notes ("" when there was nothing to report).
    """
    where = home if home is not None else Path.home()
    notes: list[str] = []
    if entry.config is not None:
        rel, content = entry.config
        cfg = where / rel
        if not cfg.exists() or SK_MANAGED in cfg.read_text(encoding="utf-8", errors="replace"):
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(content, encoding="utf-8")
        else:
            notes.append(f"{cfg} is operator-owned (no {SK_MANAGED!r} marker); left as is")
    if post_install:
        for step in entry.post_install:
            try:
                notes.append(POST_INSTALL[step](where, run))
            except Exception as exc:
                notes.append(f"{step} failed: {exc}")
    return "; ".join(n for n in notes if n)
