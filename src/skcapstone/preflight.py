"""
Preflight system checks — detect and auto-install required tools.

Checks for:
  - Python (already running, but verify version)
  - GPG / GnuPG (required for encryption)
  - Git (optional — only needed for dev/repo installs)
  - Syncthing (needed for device sync, Path 2)

Each check returns a result with:
  - Whether the tool is installed
  - Current version (if installed)
  - Platform-specific auto-install command
  - Manual download URL as fallback

Auto-install uses the platform's native package manager:
  - Linux:   apt/dnf/pacman (via sudo)
  - macOS:   brew
  - Windows: winget / choco / direct download
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ToolStatus(str, Enum):
    """Status of a system tool."""
    INSTALLED = "installed"
    MISSING = "missing"
    OPTIONAL = "optional"


@dataclass
class ToolCheck:
    """Result of checking a single system tool."""

    name: str
    status: ToolStatus
    required: bool
    version: str = ""
    install_cmd: str = ""
    download_url: str = ""
    install_note: str = ""

    @property
    def installed(self) -> bool:
        """Whether the tool is installed."""
        return self.status == ToolStatus.INSTALLED

    @property
    def ok(self) -> bool:
        """Whether this check passes (installed, or optional and missing)."""
        return self.installed or not self.required


@dataclass
class PreflightResult:
    """Combined result of all preflight checks."""

    python: ToolCheck
    gpg: ToolCheck
    git: ToolCheck
    syncthing: ToolCheck

    @property
    def all_ok(self) -> bool:
        """True if all required tools pass."""
        return all(c.ok for c in [self.python, self.gpg, self.git, self.syncthing])

    @property
    def required_missing(self) -> list[ToolCheck]:
        """List of required tools that are missing."""
        return [c for c in [self.python, self.gpg, self.git, self.syncthing]
                if c.required and not c.installed]

    @property
    def optional_missing(self) -> list[ToolCheck]:
        """List of optional tools that are missing."""
        return [c for c in [self.python, self.gpg, self.git, self.syncthing]
                if not c.required and not c.installed]


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def _system() -> str:
    """Canonical platform name."""
    return platform.system()


def _has_pkg_manager(name: str) -> bool:
    """Check if a package manager is available."""
    return shutil.which(name) is not None


def _detect_linux_pkg_manager() -> Optional[str]:
    """Detect the Linux package manager."""
    for mgr in ("apt", "dnf", "pacman", "zypper", "apk"):
        if _has_pkg_manager(mgr):
            return mgr
    return None


# ---------------------------------------------------------------------------
# Individual tool checks
# ---------------------------------------------------------------------------

def check_python() -> ToolCheck:
    """Check Python version (we're already running it, but verify version).

    Returns:
        ToolCheck for Python.
    """
    import sys
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 10)
    return ToolCheck(
        name="Python",
        status=ToolStatus.INSTALLED if ok else ToolStatus.MISSING,
        required=True,
        version=version,
        download_url="https://python.org/downloads/",
        install_note="" if ok else "Python 3.10+ is required.",
    )


def check_gpg() -> ToolCheck:
    """Check if GnuPG is installed.

    Returns:
        ToolCheck for GPG.
    """
    system = _system()

    if shutil.which("gpg") or shutil.which("gpg2"):
        binary = "gpg2" if shutil.which("gpg2") else "gpg"
        version = ""
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                first_line = result.stdout.strip().split("\n")[0]
                version = first_line[:60]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ToolCheck(
            name="GnuPG",
            status=ToolStatus.INSTALLED,
            required=True,
            version=version,
        )

    # Not installed — provide platform-specific install commands
    if system == "Linux":
        mgr = _detect_linux_pkg_manager()
        cmds = {
            "apt": "sudo apt install -y gnupg",
            "dnf": "sudo dnf install -y gnupg2",
            "pacman": "sudo pacman -S --noconfirm gnupg",
            "zypper": "sudo zypper install -y gpg2",
            "apk": "sudo apk add gnupg",
        }
        install_cmd = cmds.get(mgr, "sudo apt install -y gnupg")
    elif system == "Darwin":
        install_cmd = "brew install gnupg" if _has_pkg_manager("brew") else ""
    elif system == "Windows":
        install_cmd = "winget install --id GnuPG.Gpg4win --accept-source-agreements --accept-package-agreements"
    else:
        install_cmd = ""

    return ToolCheck(
        name="GnuPG",
        status=ToolStatus.MISSING,
        required=True,
        install_cmd=install_cmd,
        download_url=_gpg_download_url(),
        install_note="GPG encrypts your files and identity. Required for all operations.",
    )


def check_git(required: bool = False) -> ToolCheck:
    """Check if Git is installed.

    Args:
        required: Whether Git is required for this install path.

    Returns:
        ToolCheck for Git.
    """
    system = _system()

    if shutil.which("git"):
        version = ""
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                version = result.stdout.strip().split("\n")[0][:60]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ToolCheck(
            name="Git",
            status=ToolStatus.INSTALLED,
            required=required,
            version=version,
        )

    if system == "Linux":
        mgr = _detect_linux_pkg_manager()
        cmds = {
            "apt": "sudo apt install -y git",
            "dnf": "sudo dnf install -y git",
            "pacman": "sudo pacman -S --noconfirm git",
            "zypper": "sudo zypper install -y git",
            "apk": "sudo apk add git",
        }
        install_cmd = cmds.get(mgr, "sudo apt install -y git")
    elif system == "Darwin":
        install_cmd = "xcode-select --install"
    elif system == "Windows":
        install_cmd = "winget install --id Git.Git --accept-source-agreements --accept-package-agreements"
    else:
        install_cmd = ""

    return ToolCheck(
        name="Git",
        status=ToolStatus.MISSING,
        required=required,
        install_cmd=install_cmd,
        download_url=_git_download_url(),
        install_note="Git is only needed for development. You can skip this.",
    )


def check_syncthing(required: bool = False) -> ToolCheck:
    """Check if Syncthing is installed.

    Args:
        required: Whether Syncthing is required for this install path.

    Returns:
        ToolCheck for Syncthing.
    """
    system = _system()

    if shutil.which("syncthing"):
        version = ""
        try:
            result = subprocess.run(
                ["syncthing", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                version = result.stdout.strip().split("\n")[0][:60]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ToolCheck(
            name="Syncthing",
            status=ToolStatus.INSTALLED,
            required=required,
            version=version,
        )

    if system == "Linux":
        mgr = _detect_linux_pkg_manager()
        cmds = {
            "apt": "sudo apt install -y syncthing",
            "dnf": "sudo dnf install -y syncthing",
            "pacman": "sudo pacman -S --noconfirm syncthing",
        }
        install_cmd = cmds.get(mgr, "sudo apt install -y syncthing")
    elif system == "Darwin":
        install_cmd = "brew install syncthing" if _has_pkg_manager("brew") else ""
    elif system == "Windows":
        install_cmd = "winget install --id Syncthing.Syncthing --accept-source-agreements --accept-package-agreements"
    else:
        install_cmd = ""

    return ToolCheck(
        name="Syncthing",
        status=ToolStatus.MISSING,
        required=required,
        install_cmd=install_cmd,
        download_url="https://syncthing.net/downloads/",
        install_note="Syncthing syncs your identity between devices. Needed for multi-device setup.",
    )


# ---------------------------------------------------------------------------
# Auto-install
# ---------------------------------------------------------------------------

def auto_install_tool(check: ToolCheck) -> bool:
    """Attempt to auto-install a tool using its platform install command.

    Args:
        check: ToolCheck with install_cmd populated.

    Returns:
        True if install succeeded.
    """
    if check.installed:
        return True
    if not check.install_cmd:
        return False

    try:
        result = subprocess.run(
            check.install_cmd.split(),
            capture_output=True, text=True, timeout=180,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# Full preflight
# ---------------------------------------------------------------------------

def run_preflight(
    require_git: bool = False,
    require_syncthing: bool = False,
) -> PreflightResult:
    """Run all preflight checks.

    Args:
        require_git: Whether Git is required (True for dev installs).
        require_syncthing: Whether Syncthing is required (True for Path 2).

    Returns:
        PreflightResult with all tool checks.
    """
    return PreflightResult(
        python=check_python(),
        gpg=check_gpg(),
        git=check_git(required=require_git),
        syncthing=check_syncthing(required=require_syncthing),
    )


# ---------------------------------------------------------------------------
# Download URLs
# ---------------------------------------------------------------------------

def _gpg_download_url() -> str:
    """Platform-specific GPG download URL."""
    system = _system()
    urls = {
        "Windows": "https://gpg4win.org/download.html",
        "Darwin": "https://sourceforge.net/p/gpgosx/docu/Download/",
        "Linux": "https://gnupg.org/download/",
    }
    return urls.get(system, "https://gnupg.org/download/")


def _git_download_url() -> str:
    """Platform-specific Git download URL."""
    system = _system()
    urls = {
        "Windows": "https://git-scm.com/download/win",
        "Darwin": "https://git-scm.com/download/mac",
        "Linux": "https://git-scm.com/download/linux",
    }
    return urls.get(system, "https://git-scm.com/downloads")


# ---------------------------------------------------------------------------
# Legacy compatibility — used by doctor.py and old code
# ---------------------------------------------------------------------------

GIT_DOWNLOAD_URLS = {
    "Windows": "https://git-scm.com/download/win",
    "Linux": "https://git-scm.com/download/linux",
    "Darwin": "https://git-scm.com/download/mac",
}
GIT_DOWNLOAD_DEFAULT = "https://git-scm.com/downloads"


@dataclass
class GitPreflightResult:
    """Legacy result object — kept for backward compatibility with doctor.py."""

    installed: bool
    platform_label: str
    message: str
    download_url: str

    @classmethod
    def run(cls) -> "GitPreflightResult":
        """Run Git check and return a result object."""
        check = check_git(required=False)
        system = _system()
        label = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}.get(
            system, system
        )
        return cls(
            installed=check.installed,
            platform_label=label,
            message=check.version or check.install_note,
            download_url=check.download_url,
        )


def git_install_hint_for_doctor() -> str:
    """Return a one-line fix hint for skcapstone doctor.

    Returns:
        Empty string if Git is installed, otherwise hint with URL.
    """
    check = check_git()
    if check.installed:
        return ""
    return f"Install Git: {check.download_url}"
