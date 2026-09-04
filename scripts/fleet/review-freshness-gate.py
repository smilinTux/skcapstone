#!/usr/bin/env python3
"""Prove a PR head contains current main and all checks are green on that head.

Why: a PASS verdict is only as good as the head it ran against. PR427's old
green checks ran on a stale base and were nearly accepted as current evidence.
Reviewers need one deterministic command that fails when the reviewed head no
longer contains current main, or when any check is not green on that exact
head. No PASS on a stale head.

Read-only. Prints one JSON verdict. Exit codes:
  0 fresh and green
  1 head is behind or diverged from current base
  2 checks not green on the exact head (failing or still pending)
  3 data or command error
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

GREEN_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def gh_json(*args) -> dict:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])} failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


def evaluate(pr: dict, compare: dict, repo: str, number) -> dict:
    head = pr.get("headRefOid", "")
    status = (compare.get("status") or "").lower()
    # compare status: ahead | behind | diverged | identical
    fresh = status in ("ahead", "identical")

    checks, pending, failed = [], [], []
    for c in pr.get("statusCheckRollup") or []:
        name = c.get("name") or c.get("context") or "?"
        state = (c.get("status") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        checks.append({"name": name, "status": state, "conclusion": conclusion})
        if state != "COMPLETED":
            pending.append(name)
        elif conclusion not in GREEN_CONCLUSIONS:
            failed.append(name)

    green = bool(checks) and not pending and not failed
    if not fresh:
        verdict, code = "BEHIND_CURRENT_MAIN", 1
    elif not green:
        verdict, code = "CHECKS_NOT_GREEN", 2
    else:
        verdict, code = "FRESH_AND_GREEN", 0

    return {
        "repo": repo,
        "pr": number,
        "state": pr.get("state"),
        "head": head,
        "base": pr.get("baseRefName"),
        "compare_status": status,
        "behind_by": compare.get("behind_by", 0),
        "contains_current_main": fresh,
        "check_count": len(checks),
        "pending": pending,
        "failed": failed,
        "all_checks_green_on_head": green,
        "verdict": verdict,
        "exit_code": code,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Freshness gate: head contains current main and checks green"
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="PR number")
    args = parser.parse_args(argv)

    try:
        pr = gh_json(
            "pr",
            "view",
            str(args.pr),
            "--repo",
            args.repo,
            "--json",
            "state,headRefOid,baseRefName,statusCheckRollup",
        )
        if pr.get("state") != "OPEN":
            print(
                json.dumps(
                    {"verdict": "NOT_OPEN", "state": pr.get("state"), "exit_code": 3}, indent=2
                )
            )
            return 3
        compare = gh_json(
            "api", f"repos/{args.repo}/compare/{pr['baseRefName']}...{pr['headRefOid']}"
        )
    except (RuntimeError, KeyError, ValueError) as exc:
        print(json.dumps({"verdict": "ERROR", "error": str(exc), "exit_code": 3}, indent=2))
        return 3

    verdict = evaluate(pr, compare, args.repo, args.pr)
    print(json.dumps(verdict, indent=2))
    return verdict["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
