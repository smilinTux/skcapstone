#!/usr/bin/env python3
"""Report open pull requests that have fallen behind current main.

The scan is read-only. It lists open PRs via ``gh``, compares each head with the
current main tip, and exits nonzero when any PR needs a refresh. It never merges,
pushes, reviews, or files cards.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

DEFAULT_REPOS = [
    "smilinTux/skcapstone",
    "smilinTux/sklegal",
    "smilinTux/skgateway",
]
REFRESH_RECOMMENDATION = (
    "merge current main into the PR branch, push, require checks green on "
    "fresh head, then request independent review"
)


def run_gh(arguments: list[str], context: str) -> Any:
    """Run a read-only gh command and parse its JSON output.

    Args:
        arguments: Arguments following the gh executable.
        context: Description included in an attributable failure.

    Returns:
        Parsed JSON from standard output.

    Raises:
        RuntimeError: If gh fails or emits invalid JSON.
    """
    result = subprocess.run(["gh", *arguments], capture_output=True, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {context} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh {context} returned invalid JSON: {exc}") from exc


def fetch_open_prs(repo: str) -> list[dict[str, Any]]:
    """Fetch open pull requests for one repository."""
    return run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,headRefOid,baseRefName,mergeStateStatus",
            "--limit",
            "500",
        ],
        f"pr list for {repo}",
    )


def fetch_comparison(repo: str, head_oid: str) -> dict[str, Any]:
    """Compare the current main tip with a PR head commit."""
    return run_gh(
        ["api", f"repos/{repo}/compare/main...{head_oid}"],
        f"compare for {repo}@{head_oid}",
    )


def scan(repos: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Scan repositories and return per-PR findings plus attributable errors."""
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    for repo in repos:
        try:
            prs = fetch_open_prs(repo)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        for pr in prs:
            head_oid = str(pr.get("headRefOid", ""))
            try:
                comparison = fetch_comparison(repo, head_oid)
                ahead = int(comparison["ahead_by"])
                behind = int(comparison["behind_by"])
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                errors.append(f"comparison unavailable for {repo}#{pr.get('number')}: {exc}")
                ahead = None
                behind = None

            merge_state = str(pr.get("mergeStateStatus") or "UNKNOWN").upper()
            refresh_needed = (
                behind is None
                or behind > 0
                or merge_state
                in {
                    "BEHIND",
                    "DIRTY",
                }
            )
            findings.append(
                {
                    "repo": repo,
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "head": head_oid,
                    "base": pr.get("baseRefName", ""),
                    "mergeStateStatus": merge_state,
                    "ahead": ahead,
                    "behind": behind,
                    "refresh": "REFRESH_NEEDED" if refresh_needed else "CURRENT",
                    "recommendation": (REFRESH_RECOMMENDATION if refresh_needed else "none"),
                }
            )
    return findings, errors


def main(argv: list[str] | None = None) -> int:
    """Run the fleet scan and emit JSON or a concise text report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    findings, errors = scan(args.repos or DEFAULT_REPOS)

    if args.as_json:
        print(json.dumps({"findings": findings, "errors": errors}, indent=2))
    else:
        for finding in findings:
            print(
                f"{finding['repo']}#{finding['number']} "
                f"mergeStateStatus={finding['mergeStateStatus']} "
                f"ahead={finding['ahead']} behind={finding['behind']} "
                f"refresh={finding['refresh']}"
            )
            print(f"  recommendation: {finding['recommendation']}")
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)

    if errors or any(item["refresh"] == "REFRESH_NEEDED" for item in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
