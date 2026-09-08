"""SKWorld fleet rotation module with version tracking.

This module provides version tracking and a CLI entry point for skfleet-rotate.
The actual rotation logic lives in scripts/fleet/skfleet-rotate.py.

Card: 41f84c4f - SKFLEET-ROTATE-PACKAGING-01
"""

import json
import subprocess
import sys
from pathlib import Path

__version__ = "0.15.97"  # Synced with skcapstone package version


def get_version() -> str:
    """Return the version string of this rotation module."""
    return __version__


def get_version_info() -> dict:
    """Return detailed version information including package version."""
    try:
        import importlib.metadata as metadata
        try:
            pkg_version = metadata.version("skcapstone")
        except metadata.PackageNotFoundError:
            pkg_version = __version__
    except ImportError:
        pkg_version = __version__
    
    return {
        "rotation_module_version": __version__,
        "package_version": pkg_version,
        "file_path": __file__,
    }


def verify_version(expected_version: str | None = None) -> tuple[bool, str]:
    """Verify that the running rotation matches the expected version."""
    info = get_version_info()
    running_version = info["package_version"]
    
    if expected_version is None:
        return True, f"No version constraint, running {running_version}"
    
    if running_version == expected_version:
        return True, f"Version matches: {running_version}"
    else:
        return (False,
                f"VERSION MISMATCH: expected {expected_version}, running {running_version}. "
                f"Deploy the correct version before continuing.")


def _get_rotation_script_path() -> Path:
    """Find the path to the actual rotation script."""
    # First try relative to this module (development)
    module_dir = Path(__file__).parent
    script_path = module_dir.parent.parent.parent / "scripts" / "fleet" / "skfleet-rotate.py"
    
    if script_path.exists():
        return script_path
    
    # Try package data location (installed)
    try:
        import importlib.resources as resources
        with resources.files("skcapstone.fleet") as pkg_dir:
            script_path = pkg_dir / "scripts" / "fleet" / "skfleet-rotate.py"
            if script_path.is_file():
                return script_path
    except (ImportError, FileNotFoundError):
        pass
    
    # Fallback: look in data directories
    try:
        import skcapstone
        pkg_root = Path(skcapstone.__file__).parent
        script_path = pkg_root / "scripts" / "fleet" / "skfleet-rotate.py"
        if script_path.exists():
            return script_path
    except ImportError:
        pass
    
    # Last resort: system path
    return Path("scripts/fleet/skfleet-rotate.py")


def cli_main() -> None:
    """CLI entry point with version and verification support.
    
    This function handles --version, --verify, and --version-info flags,
    then executes the actual rotation script.
    """
    import argparse
    
    parser = argparse.ArgumentParser(
        description="SKWorld fleet rotation - keeps N ephemeral codex workers busy on READY cards",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,  # Don't add --help, let the rotation script handle it
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"skfleet-rotate {get_version_info()['package_version']}"
    )
    parser.add_argument(
        "--verify", "-V",
        metavar="EXPECTED_VERSION",
        help="Verify the running rotation matches EXPECTED_VERSION and exit"
    )
    parser.add_argument(
        "--version-info", "-i",
        action="store_true",
        help="Print detailed version information as JSON"
    )
    
    # Parse known args only, so rotation script's own args are preserved
    args, remaining = parser.parse_known_args()
    
    if args.version_info:
        info = get_version_info()
        info["cli_args"] = sys.argv[1:]
        print(json.dumps(info, indent=2, sort_keys=True))
        sys.exit(0)
    
    if args.verify:
        is_match, message = verify_version(args.verify)
        print(message)
        sys.exit(0 if is_match else 1)
    
    # Run the actual rotation script
    rotation_script = _get_rotation_script_path()
    
    if not rotation_script.exists():
        sys.stderr.write(f"ERROR: Rotation script not found at {rotation_script}\n")
        sys.exit(1)
    
    # Execute the rotation script using Python
    result = subprocess.run(
        [sys.executable, str(rotation_script)] + remaining,
        env=None,  # Inherit current environment
    )
    
    sys.exit(result.returncode)


if __name__ == "__main__":
    cli_main()
