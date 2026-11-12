#!/usr/bin/env python3
"""Can every workflow ref in this repo actually be resolved?

THE INVARIANT THIS ENFORCES
---------------------------
A green that can be produced by ABSENCE is not a green. Any signal a consumer
treats as a pass must be able to tell "observed and healthy" apart from "not
observed". See CONTRIBUTING.md, "Observability invariant".

WHY THIS FILE EXISTS
--------------------
On 2026-09-19 `docs / docs-check` stopped publishing a check run at all, twice,
for two different reasons that produced the identical symptom:

  1. The reusable-workflow ref was pinned to a squash-merged PR's head commit.
     The branch was deleted, so the sha became reachable from nothing and
     `uses:` could not resolve it.
  2. The same pin was then replaced with a 12-character ABBREVIATED sha.
     `uses:` requires a full 40-hex sha; abbreviated refs do not resolve.

A workflow that fails to resolve its `uses:` dies BEFORE creating any job, and
a workflow that never creates a job publishes NO check run. So the required
gate did not go red. It went ABSENT, `gh pr checks` simply stopped listing it,
and every consumer rendered "10 checks, 0 failing" as healthy. Between 08:46Z
and 09:29Z every merge to main ran with zero docs enforcement, tier 3 included.

Counting checks cannot catch this and neither can the gate itself: a guard that
lives only inside docs-check cannot observe a broken docs-check ref. That is
why the static half of this script runs under `unit tests (py3.12)` and the
resolve half runs under `shim-imports`, two required contexts whose workflows
have NO cross-repo `uses:` of their own and therefore still run when a
cross-repo ref is broken.

CHECKS
------
static (offline, no network):
  A. No `uses:` ref is an abbreviated hex sha (7-39 hex chars). Full 40 or a
     real branch/tag name; an abbreviated sha is never resolvable.
  B. Every CROSS-REPO reusable-workflow call (`owner/repo/.github/workflows/x.yml@REF`)
     is pinned to a full 40-hex sha. A branch or tag pin is mutable and can be
     deleted out from under us, which is failure mode 1 with extra steps.
  C. No ref-shaped `with:` input (a key ending in ref/sha/rev/commit) carries an
     abbreviated hex sha. This covers `standards-ref`, which selects the actual
     validator and drifts independently of the `uses:` pin.

resolve (network, needs `gh`):
  D. Every sha from B and C is REACHABLE FROM the target repo's default branch:
     `compare/<default>...<sha>` must be `identical` or `behind`. `ahead` and
     `diverged` both mean the commit lives only on a branch, which is exactly
     the state that evaporates when the branch is deleted.

EXIT CODES
----------
  0  every ref checked and good.
  1  a definite violation. This is the red you want.
  2  COULD NOT DETERMINE (no `gh`, no network, API error). Deliberately NOT 0.
     "I could not look" must never render as "I looked and it was fine" -- that
     conversion is the entire defect this script exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised on minimal runners
    yaml = None  # type: ignore[assignment]

ABBREV_SHA = re.compile(r"^[0-9a-f]{7,39}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REF_INPUT = re.compile(r"(?:^|[-_])(ref|sha|rev|commit)$", re.I)
# owner/repo/.github/workflows/<file>@<ref>
REUSABLE = re.compile(r"^(?P<owner>[^/@]+)/(?P<repo>[^/@]+)/\.github/workflows/[^@]+@(?P<ref>.+)$")

OK, BAD = "  ok   ", "  FAIL "
UNKNOWN = "  ????  "

# Reachable-from-default-branch. Anything else means the commit is only on a
# branch and disappears with it.
GOOD_COMPARE = {"identical", "behind"}


class Finding:
    def __init__(self, level: str, where: str, msg: str) -> None:
        self.level = level  # "fail" or "unknown"
        self.where = where
        self.msg = msg

    def render(self) -> str:
        tag = BAD if self.level == "fail" else UNKNOWN
        return f"{tag}{self.where}: {self.msg}"


def workflow_files(repo: Path) -> list[Path]:
    d = repo / ".github" / "workflows"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.suffix in (".yml", ".yaml"))


def _jobs(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    jobs = doc.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def collect(repo: Path) -> tuple[list[dict], list[Finding]]:
    """Return (pins, parse_findings).

    A pin is {'where', 'owner', 'repo', 'sha', 'kind'} for anything that must
    resolve remotely. Parse failures are findings, never silently skipped: a
    workflow we could not read is a workflow we did not check.
    """
    pins: list[dict] = []
    findings: list[Finding] = []

    for wf in workflow_files(repo):
        rel = f".github/workflows/{wf.name}"
        try:
            doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # unreadable != fine
            findings.append(Finding("fail", rel, f"could not parse as YAML: {exc}"))
            continue

        for job_name, job in _jobs(doc).items():
            if not isinstance(job, dict):
                continue
            where = f"{rel}:{job_name}"
            uses_list: list[tuple[str, str, dict]] = []

            # job-level `uses:` == a reusable workflow call
            if isinstance(job.get("uses"), str):
                uses_list.append((where, job["uses"], job.get("with") or {}))

            # step-level `uses:` == an action
            steps = job.get("steps")
            if isinstance(steps, list):
                for i, step in enumerate(steps):
                    if isinstance(step, dict) and isinstance(step.get("uses"), str):
                        uses_list.append(
                            (f"{where}.steps[{i}]", step["uses"], step.get("with") or {})
                        )

            for w, uses, with_block in uses_list:
                pins.extend(_check_one(w, uses, with_block, findings))

    return pins, findings


def _check_one(where: str, uses: str, with_block: Any, findings: list[Finding]) -> list[dict]:
    """Static checks A, B, C for a single `uses:`. Returns pins needing resolve."""
    pins: list[dict] = []
    local = uses.startswith("./")
    ref = uses.split("@", 1)[1] if "@" in uses else None
    m = REUSABLE.match(uses)

    # --- A: no abbreviated sha anywhere in a `uses:` ref
    if ref is not None and ABBREV_SHA.match(ref):
        findings.append(
            Finding(
                "fail",
                where,
                f"`uses: {uses}` is pinned to an ABBREVIATED sha ({len(ref)} chars). "
                "GitHub resolves only a full 40-hex sha, a branch, or a tag. An "
                "abbreviated pin does not resolve, the workflow dies before creating "
                "a job, and the check goes ABSENT rather than red.",
            )
        )

    # --- B: cross-repo reusable workflow must be a full sha
    if m and not local:
        if not FULL_SHA.match(ref or ""):
            findings.append(
                Finding(
                    "fail",
                    where,
                    f"reusable workflow `{uses}` is not pinned to a full 40-hex sha. "
                    "A branch or tag pin is mutable and deletable; when it vanishes "
                    "this gate goes ABSENT, not red.",
                )
            )
        else:
            pins.append(
                {
                    "where": where,
                    "owner": m.group("owner"),
                    "repo": m.group("repo"),
                    "sha": ref,
                    "kind": "uses",
                }
            )

    # --- C: ref-shaped `with:` inputs
    if isinstance(with_block, dict):
        for key, val in with_block.items():
            if not isinstance(val, str) or not REF_INPUT.search(str(key)):
                continue
            if ABBREV_SHA.match(val):
                findings.append(
                    Finding(
                        "fail",
                        where,
                        f"input `{key}: {val}` is an ABBREVIATED sha ({len(val)} chars). "
                        "It selects which upstream code actually runs and will not "
                        "resolve.",
                    )
                )
            elif FULL_SHA.match(val) and m:
                pins.append(
                    {
                        "where": f"{where} (input {key})",
                        "owner": m.group("owner"),
                        "repo": m.group("repo"),
                        "sha": val,
                        "kind": "input",
                    }
                )
    return pins


# ------------------------------------------------------------------ resolve
def _gh(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        return 127, "", "gh not found"
    except subprocess.TimeoutExpired:
        return 124, "", "gh timed out"
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _default_branch(owner: str, repo: str, cache: dict) -> tuple[str | None, str]:
    key = f"{owner}/{repo}"
    if key in cache:
        return cache[key], ""
    rc, out, err = _gh(["api", f"repos/{key}", "--jq", ".default_branch"])
    if rc != 0 or not out:
        return None, err or "could not read default branch"
    cache[key] = out
    return out, ""


def resolve(pins: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    cache: dict[str, str] = {}

    for pin in pins:
        owner, repo, sha = pin["owner"], pin["repo"], pin["sha"]
        base, err = _default_branch(owner, repo, cache)
        if base is None:
            findings.append(
                Finding(
                    "unknown",
                    pin["where"],
                    f"could not read {owner}/{repo} default branch ({err}). NOT a pass.",
                )
            )
            continue

        status, level, detail = _compare(owner, repo, base, sha)
        if level == "ok" and status in GOOD_COMPARE:
            print(f"{OK}{pin['where']}: {sha[:12]}.. is `{status}` {base} in {owner}/{repo}")
        elif level == "ok":
            # We got a DEFINITE answer and the answer is bad. `ahead` and
            # `diverged` both mean the commit is not reachable from the default
            # branch, so it exists only on a branch and evaporates when that
            # branch is deleted. This is exit 1, not exit 2: we looked, and it
            # is wrong.
            findings.append(
                Finding(
                    "fail",
                    pin["where"],
                    f"{sha} is `{status}` vs {owner}/{repo}@{base}. It is NOT reachable "
                    "from the default branch, so it lives only on a branch and dies "
                    "with it. Repin to a sha that is `identical` or `behind` "
                    f"{base}.",
                )
            )
        elif level == "fail":
            findings.append(
                Finding(
                    "fail",
                    pin["where"],
                    f"{sha} vs {owner}/{repo}@{base}: {detail}.",
                )
            )
        else:
            findings.append(
                Finding("unknown", pin["where"], f"could not compare {sha}: {detail}. NOT a pass.")
            )
    return findings


def _compare(owner: str, repo: str, base: str, sha: str) -> tuple[str | None, str, str]:
    """(status, level, detail). level is 'ok' | 'fail' | 'unknown'.

    A 404 is DEFINITE: the commit is not in the repo. Anything else that goes
    wrong is 'unknown', which is its own exit path and never a pass.
    """
    last = ""
    for attempt in range(3):
        rc, out, err = _gh(
            ["api", f"repos/{owner}/{repo}/compare/{base}...{sha}", "--jq", ".status"]
        )
        if rc == 0 and out:
            return out, "ok", out
        last = err or out or f"gh exited {rc}"
        if "404" in last or "Not Found" in last:
            return None, "fail", "not found in the repo at all (404)"
        if attempt < 2:
            time.sleep(2 * (attempt + 1))  # transient 5xx / rate limit
    return None, "unknown", last


# ------------------------------------------------------------------ self-test
def self_test() -> bool:
    """Negative control. A guard never seen failing is a guess, not a guard.

    Builds the two shapes that actually broke main on 2026-09-19 and asserts
    each is caught.
    """
    import tempfile

    cases = {
        "abbreviated sha (the 2026-09-19 09:29Z break)": (
            "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
            "docs-check.yml@8a799322af9f\n"
        ),
        "branch pin instead of a sha": (
            "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
            "docs-check.yml@main\n"
        ),
        "abbreviated ref-shaped input (the standards-ref twin)": (
            "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
            "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n"
            "    with:\n      standards-ref: 8a799322af9f\n"
        ),
        "unparseable workflow": ("jobs:\n  docs:\n   uses: [\n"),
    }
    all_caught = True
    for label, body in cases.items():
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / ".github" / "workflows"
            d.mkdir(parents=True)
            (d / "x.yml").write_text(body, encoding="utf-8")
            _, findings = collect(Path(td))
            caught = any(f.level == "fail" for f in findings)
            print(f"  {'correctly FAILED' if caught else 'WRONGLY PASSED'}: {label}")
            all_caught = all_caught and caught

    # and the positive control: a correct pin must NOT be flagged
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / "x.yml").write_text(
            "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
            "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n",
            encoding="utf-8",
        )
        pins, findings = collect(Path(td))
        clean = not findings and len(pins) == 1
        print(f"  {'correctly passed' if clean else 'WRONGLY FAILED'}: a proper full-sha pin")
        all_caught = all_caught and clean

    print()
    print("negative control:", "PASS (the guard can fail)" if all_caught else "BROKEN")
    return all_caught


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--resolve", action="store_true", help="also check refs resolve (needs gh)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        if yaml is None:
            print("  ????  PyYAML not importable; the negative control could not run (exit 2).")
            return 2
        return 0 if self_test() else 1

    if yaml is None:
        # The docs-check runner is deliberately minimal and has no PyYAML.
        # Exiting 1 here would claim we found a violation; exiting 0 would
        # claim we checked. Neither is true. This is exactly the distinction
        # the whole script exists to preserve.
        print(
            "  ????  PyYAML is not importable, so no workflow file was parsed. "
            "Nothing was checked.\nCOULD NOT DETERMINE (exit 2). Run this where "
            "the package deps are installed: `unit tests` and `shim-imports` "
            "both have them."
        )
        return 2

    repo = Path(args.repo).resolve()
    pins, findings = collect(repo)

    n_wf = len(workflow_files(repo))
    if n_wf == 0:
        # Zero workflows is not "all clean". It is "nothing observed".
        print(f"{BAD}no workflow files under {repo}/.github/workflows -- nothing was checked")
        return 2

    if not findings:
        print(f"{OK}static: {n_wf} workflow file(s), all `uses:` refs well-formed")

    if args.resolve:
        findings.extend(resolve(pins))
    elif pins:
        print(f"  note   {len(pins)} pin(s) not resolve-checked (--resolve not given)")

    for f in findings:
        print(f.render())

    if args.json:
        print(
            json.dumps(
                {
                    "workflows": n_wf,
                    "pins": len(pins),
                    "resolved": args.resolve,
                    "findings": [
                        {"level": f.level, "where": f.where, "msg": f.msg} for f in findings
                    ],
                },
                indent=2,
            )
        )

    if any(f.level == "fail" for f in findings):
        return 1
    if any(f.level == "unknown" for f in findings):
        print(
            "\nCOULD NOT DETERMINE. This is exit 2, not exit 0, on purpose: an "
            "unverified ref must never render as a verified one."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
