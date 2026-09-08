#!/usr/bin/env python3
"""Deploy skfleet-rotate across the fleet with version verification.

This script handles deploying the rotation script as a versioned package
artifact with drift detection. It replaces the previous scp-based deployment.

Usage:
    python scripts/fleet/deploy-rotation.py --version 0.15.97 [--hosts HOST1,HOST2,...]

Card: 41f84c4f - SKFLEET-ROTATE-PACKAGING-01
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple


ROTATION_HOSTS = ["chiap01", "chiap02", "chiap03", "chiap04", "chiap08"]


def run_ssh(host: str, command: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command on a remote host via SSH."""
    ssh_cmd = ["ssh", host, command]
    if capture:
        return subprocess.run(ssh_cmd, capture_output=True, text=True)
    else:
        return subprocess.run(ssh_cmd)


def check_rotation_version(host: str) -> Tuple[str, str, str]:
    """Check the rotation version on a remote host.

    Returns:
        (stdout, stderr, combined) tuple
    """
    result = run_ssh(host, "skfleet-rotate --version-info")
    return result.stdout, result.stderr, result.stdout + result.stderr


def deploy_to_host(host: str, expected_version: str, dry_run: bool = False) -> Dict:
    """Deploy rotation to a single host with verification.

    Returns:
        Dict with deployment status and details
    """
    result = {
        "host": host,
        "expected_version": expected_version,
        "status": "unknown",
        "message": "",
        "installed_version": None,
    }

    # Check current version
    stdout, stderr, combined = check_rotation_version(host)

    if "command not found" in combined.lower() or "not found" in combined.lower():
        result["status"] = "not_installed"
        result["message"] = "skfleet-rotate not found, will install"
    else:
        try:
            version_info = json.loads(stdout)
            installed_version = version_info.get("package_version", "unknown")
            result["installed_version"] = installed_version

            if installed_version == expected_version:
                result["status"] = "uptodate"
                result["message"] = f"Already running {expected_version}"
                return result
            else:
                result["status"] = "mismatch"
                result["message"] = f"Version mismatch: have {installed_version}, need {expected_version}"
        except (json.JSONDecodeError, Exception) as e:
            result["status"] = "error"
            result["message"] = f"Failed to parse version info: {e}"
            return result

    # Deploy the package
    if dry_run:
        result["status"] = "dryrun"
        result["message"] = f"Would deploy {expected_version}"
        return result

    # Install/upgrade the package
    # This assumes skcapstone is installed via pip in a virtualenv
    install_cmd = "source ~/.skenv/bin/activate && pip install --upgrade skcapstone"
    install_result = run_ssh(host, install_cmd)

    if install_result.returncode != 0:
        result["status"] = "install_failed"
        result["message"] = f"Install failed: {install_result.stderr[:200]}"
        return result

    # Verify the deployed version
    stdout, stderr, combined = check_rotation_version(host)

    try:
        version_info = json.loads(stdout)
        installed_version = version_info.get("package_version", "unknown")
        result["installed_version"] = installed_version

        if installed_version == expected_version:
            result["status"] = "deployed"
            result["message"] = f"Successfully deployed {expected_version}"
        else:
            result["status"] = "verify_failed"
            result["message"] = f"Deployed but version wrong: {installed_version}"
    except Exception as e:
        result["status"] = "verify_error"
        result["message"] = f"Verification failed: {e}"

    return result


def deploy_to_all_hosts(
    hosts: List[str],
    expected_version: str,
    dry_run: bool = False,
    fail_on_drift: bool = True,
) -> Dict:
    """Deploy rotation to multiple hosts and report drift.

    Returns:
        Dict with overall deployment status and per-host details
    """
    results = {
        "expected_version": expected_version,
        "dry_run": dry_run,
        "hosts": [],
        "summary": {
            "total": len(hosts),
            "uptodate": 0,
            "deployed": 0,
            "failed": 0,
            "mismatch": 0,
        },
    }

    for host in hosts:
        host_result = deploy_to_host(host, expected_version, dry_run)
        results["hosts"].append(host_result)
        results["summary"][host_result["status"]] = (
            results["summary"].get(host_result["status"], 0) + 1
        )

    # Check for drift
    versions = set(h["installed_version"] for h in results["hosts"] if h["installed_version"])
    if len(versions) > 1:
        results["drift_detected"] = True
        results["drift_details"] = {v: [h["host"] for h in results["hosts"] if h["installed_version"] == v] for v in versions}
    else:
        results["drift_detected"] = False

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Deploy skfleet-rotate across the fleet with version verification"
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Expected version to deploy (e.g., 0.15.97)"
    )
    parser.add_argument(
        "--hosts",
        default=",".join(ROTATION_HOSTS),
        help=f"Comma-separated list of hosts (default: {','.join(ROTATION_HOSTS)})"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check versions, do not deploy"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON"
    )

    args = parser.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]

    if args.check_only:
        # Just check versions
        results = {"hosts": []}
        for host in hosts:
            stdout, stderr, combined = check_rotation_version(host)
            if "command not found" in combined.lower():
                results["hosts"].append({"host": host, "status": "not_installed"})
            else:
                try:
                    info = json.loads(stdout)
                    results["hosts"].append({
                        "host": host,
                        "status": "installed",
                        "version": info.get("package_version"),
                        "path": info.get("file_path"),
                    })
                except:
                    results["hosts"].append({"host": host, "status": "error"})
    else:
        results = deploy_to_all_hosts(
            hosts, args.version, dry_run=args.dry_run
        )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"Deployment version: {args.version}")
        print(f"Hosts: {', '.join(hosts)}")
        print()

        if args.dry_run:
            print("DRY RUN MODE - no changes made")
            print()

        for host_result in results.get("hosts", []):
            print(f"{host_result['host']}: {host_result['status']}")
            if host_result.get("message"):
                print(f"  {host_result['message']}")
            print()

        if "drift_detected" in results and results["drift_detected"]:
            print("WARNING: Version drift detected!")
            for version, host_list in results.get("drift_details", {}).items():
                print(f"  {version}: {', '.join(host_list)}")
            print()

        if "summary" in results:
            summary = results["summary"]
            print(f"Summary: {summary.get('uptodate', 0)} up-to-date, "
                  f"{summary.get('deployed', 0)} deployed, "
                  f"{summary.get('failed', 0)} failed, "
                  f"{summary.get('mismatch', 0)} mismatched")

        # Exit with error if drift detected and fail_on_drift is implied
        if results.get("drift_detected"):
            sys.exit(1)


if __name__ == "__main__":
    main()
