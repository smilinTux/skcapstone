#!/usr/bin/env python3
"""
SKCapstone Bundle Update Checker
Checks for updates to all SK packages and reports status
"""

import subprocess
import sys
import json
from datetime import datetime
from pathlib import Path

# Package configuration
PACKAGES = {
    "skcapstone": {
        "name": "skcapstone",
        "path": "~/clawd/skcapstone",
        "pypi_name": "skcapstone",
    },
    "skmemory": {
        "name": "skmemory",
        "path": "~/clawd/skcapstone-repos/skmemory",
        "pypi_name": "skmemory",
    },
    "sksecurity": {
        "name": "sksecurity",
        "path": "~/clawd/skcapstone-repos/sksecurity",
        "pypi_name": "sksecurity",
    },
    "cloud9-protocol": {
        "name": "cloud9-protocol",
        "path": "~/clawd/skcapstone-repos/cloud9-python",
        "pypi_name": "cloud9-protocol",
    },
}


def get_installed_version(package_name):
    """Get currently installed version of a package."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.split("\n"):
            if line.startswith("Version:"):
                return line.split(":")[1].strip()
    except subprocess.CalledProcessError:
        return None
    return None


def get_latest_version(package_name):
    """Get latest version from PyPI."""
    try:
        import urllib.request
        import json

        url = f"https://pypi.org/pypi/{package_name}/json"
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read())
            return data["info"]["version"]
    except Exception:
        return None


def check_git_updates(package_name, package_path):
    """Check if local repo has uncommitted changes or is behind remote."""
    path = Path(package_path).expanduser()

    if not path.exists():
        return {"error": "Repository not found"}

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True
        )
        has_changes = len(result.stdout.strip()) > 0

        # Check if behind remote
        subprocess.run(["git", "-C", str(path), "fetch", "--quiet"], capture_output=True)

        result = subprocess.run(
            ["git", "-C", str(path), "rev-list", "HEAD..@{upstream}", "--count"],
            capture_output=True,
            text=True,
        )
        behind_count = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0

        # Get current commit
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        current_commit = result.stdout.strip()

        return {
            "has_changes": has_changes,
            "behind_count": behind_count,
            "current_commit": current_commit,
        }
    except Exception as e:
        return {"error": str(e)}


def check_package(package_key):
    """Check status of a single package."""
    config = PACKAGES[package_key]

    status = {
        "name": config["name"],
        "installed": None,
        "latest_pypi": None,
        "git_status": None,
    }

    # Check installed version
    status["installed"] = get_installed_version(config["pypi_name"])

    # Check PyPI version
    status["latest_pypi"] = get_latest_version(config["pypi_name"])

    # Check git status
    status["git_status"] = check_git_updates(config["name"], config["path"])

    return status


def print_status_report(results):
    """Print formatted status report."""
    print("\n" + "=" * 70)
    print("SKCapstone Bundle Update Checker")
    print(f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    updates_available = []
    git_updates_available = []

    for package_key, status in results.items():
        print(f"\n📦 {status['name']}")
        print("-" * 70)

        # Installed version
        if status["installed"]:
            print(f"   Installed: {status['installed']}")
        else:
            print(f"   Installed: ❌ Not installed")

        # PyPI version
        if status["latest_pypi"]:
            print(f"   PyPI:      {status['latest_pypi']}")
            if status["installed"] and status["installed"] != status["latest_pypi"]:
                print(f"   ⚠️  Update available on PyPI!")
                updates_available.append(status["name"])
        else:
            print(f"   PyPI:      (unable to check)")

        # Git status
        git = status["git_status"]
        if git:
            if "error" in git:
                print(f"   Git:       ❌ {git['error']}")
            else:
                print(f"   Git commit: {git['current_commit']}")
                if git["has_changes"]:
                    print(f"   ⚠️  Uncommitted changes detected")
                if git["behind_count"] > 0:
                    print(f"   ⚠️  {git['behind_count']} commit(s) behind remote")
                    git_updates_available.append(status["name"])

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if updates_available:
        print(f"\n🔄 PyPI updates available: {', '.join(updates_available)}")
        print("   Run: pip install --upgrade " + " ".join(updates_available))
    else:
        print("\n✅ All packages up-to-date on PyPI")

    if git_updates_available:
        print(f"\n🔄 Git updates available: {', '.join(git_updates_available)}")
        print("   Run: git pull in respective repositories")
    else:
        print("✅ All repositories up-to-date")

    print("\n" + "=" * 70)


def save_check_results(results):
    """Save results to JSON file for programmatic access."""
    results_dir = Path("~/.skcapstone/logs").expanduser()
    results_dir.mkdir(parents=True, exist_ok=True)

    results_file = results_dir / f"update-check-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    with open(results_file, "w") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "results": results}, f, indent=2)

    print(f"\n📄 Detailed results saved to: {results_file}")


def main():
    """Main entry point."""
    print("Checking for updates to all SK packages...")
    print("This may take a moment...")

    results = {}
    for package_key in PACKAGES:
        results[package_key] = check_package(package_key)

    print_status_report(results)
    save_check_results(results)

    # Exit with error code if updates available (useful for automation)
    has_updates = any(
        r["installed"] != r["latest_pypi"]
        for r in results.values()
        if r["installed"] and r["latest_pypi"]
    )

    return 1 if has_updates else 0


if __name__ == "__main__":
    sys.exit(main())
