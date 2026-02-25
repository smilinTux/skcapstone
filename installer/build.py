"""
Build script — create platform-specific installers.

Produces:
  - Windows: SovereignSetup.exe (one-file, GUI, no console)
  - macOS:   SovereignSetup.app (one-dir bundle)
  - Linux:   sovereign-setup (one-file CLI binary)

Requirements:
  pip install pyinstaller

Usage:
  python installer/build.py           # Build for current platform
  python installer/build.py --all     # Show instructions for all platforms
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
ENTRY_POINT = SRC_DIR / "skcapstone" / "gui_installer.py"
CLI_ENTRY = SRC_DIR / "skcapstone" / "_cli_monolith.py"

PRODUCT_NAME = "SovereignSetup"
ICON_WIN = SCRIPT_DIR / "icon.ico"
ICON_MAC = SCRIPT_DIR / "icon.icns"


def build_windows() -> None:
    """Build Windows .exe with GUI (no console window)."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", PRODUCT_NAME,
        "--add-data", f"{SRC_DIR / 'skcapstone'};skcapstone",
    ]
    if ICON_WIN.exists():
        cmd.extend(["--icon", str(ICON_WIN)])
    cmd.append(str(ENTRY_POINT))

    print(f"Building Windows installer: {PRODUCT_NAME}.exe")
    print(f"Command: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))
    print(f"\nOutput: dist/{PRODUCT_NAME}.exe")


def build_macos() -> None:
    """Build macOS .app bundle."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--windowed",
        "--name", PRODUCT_NAME,
        "--add-data", f"{SRC_DIR / 'skcapstone'}:skcapstone",
    ]
    if ICON_MAC.exists():
        cmd.extend(["--icon", str(ICON_MAC)])
    cmd.append(str(ENTRY_POINT))

    print(f"Building macOS installer: {PRODUCT_NAME}.app")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))
    print(f"\nOutput: dist/{PRODUCT_NAME}.app")


def build_linux() -> None:
    """Build Linux standalone binary (CLI, no GUI needed)."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "sovereign-setup",
        "--add-data", f"{SRC_DIR / 'skcapstone'}:skcapstone",
        str(CLI_ENTRY),
    ]

    print("Building Linux binary: sovereign-setup")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))
    print("\nOutput: dist/sovereign-setup")


def main() -> None:
    """Build for the current platform."""
    system = platform.system()

    if "--all" in sys.argv:
        print("Platform build instructions:")
        print()
        print("  Windows:  python installer/build.py    → dist/SovereignSetup.exe")
        print("  macOS:    python installer/build.py    → dist/SovereignSetup.app")
        print("  Linux:    python installer/build.py    → dist/sovereign-setup")
        print()
        print("Requirements: pip install pyinstaller")
        print()
        print("For cross-platform builds, run on each target OS")
        print("or use GitHub Actions (see .github/workflows/).")
        return

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required. Install it:")
        print("  pip install pyinstaller")
        sys.exit(1)

    if system == "Windows":
        build_windows()
    elif system == "Darwin":
        build_macos()
    elif system == "Linux":
        build_linux()
    else:
        print(f"Unknown platform: {system}")
        sys.exit(1)


if __name__ == "__main__":
    main()
