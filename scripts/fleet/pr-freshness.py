#!/usr/bin/env python3
"""Report open pull requests that have fallen behind current main.

Why: PR427 (card c6eeed44) fell behind a fast-moving main twice. Its stale-head
green checks were nearly accepted as current evidence, and two reviews returned
BLOCKED over drift that came from the old base, not the candidate. Freshness
has to be visible before anyone treats checks or reviews as current.

Read-only. Lists open PRs via gh, classifies merge state, exits 1 when any PR
needs a current-main refresh. Never merges, never pushes, never files cards.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

DEFAULT_REPOS = [
    "smilinTux/skcapstone",
    "smilinTux/sklegal",
    "smilinTux/skgateway",
]

# Head does not contain the current base tip.
REFRESH_STATES = {"BEHIND", "DIRTY"}
# Human reads why. BLOCKED is often just "review required", which is fine.
ATTENTION_STATES = {"BLOCKED", "UNKNOWN"}


def classify_pr(pr: dict) -> str:
    state = (pr.get("mergeStateStatus") or "UNKNOWN").upper()
    if state in REFRESH_STATES:
        return "REFRESH_NEEDED"
    if state in ATTENTION_STATES:
        return "NEEDS_ATTENTION"
    return "CLEAN"


def fetch_open_prs(repo: str) -> list:
    cmd = [
        "gh",
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
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr list failed for {repo}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def scan(repos: list) -> tuple:
    findings, errors = [], []
    for repo in repos:
        try:
            prs = fetch_open_prs(repo)
        except (RuntimeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        for pr in prs:
            findings.append(
                {
                    "repo": repo,
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "head": pr.get("headRefOid", ""),
                    "base": pr.get("baseRefName", ""),
                    "mergeStateStatus": pr.get("mergeStateStatus") or "UNKNOWN",
                    "verdict": classify_pr(pr),
                }
            )
    return findings, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Report open PRs behind current main")
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="owner/name to scan; repeatable; defaults to the fleet repos",
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object instead of lines"
    )
    args = parser.parse_args(argv)

    repos = args.repo or DEFAULT_REPOS
    findings, errors = scan(repos)

    if args.json:
        print(json.dumps({"findings": findings, "errors": errors}, indent=2))
    else:
        for f in findings:
            print(
                f"{f['verdict']:<15} {f['repo']}#{f['number']} "
                f"[{f['mergeStateStatus']}] {f['title'][:60]}"
            )
        bad = sum(1 for f in findings if f["verdict"] == "REFRESH_NEEDED")
        att = sum(1 for f in findings if f["verdict"] == "NEEDS_ATTENTION")
        print(
            f"scan complete: {len(findings)} open, {bad} refresh needed, "
            f"{att} need attention, {len(errors)} errors"
        )
    for e in errors:
        print(f"error: {e}", file=sys.stderr)

    return 1 if any(f["verdict"] == "REFRESH_NEEDED" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
