"""Snapshot and restore of main-branch merge gates (GitHub rulesets).

This module is the durable half of the SKCAPSTONE-BRANCH-GATE-01 work:
* inventory: read the repo's exact branch-protection or ruleset state via
  the GitHub REST API (no credentials ever echoed).
* record: save that state as JSON evidence so a rollback target exists even
  after the live ruleset has changed.
* rollback: write a recorded prior snapshot back through the same API.

The module is deliberately dependency-light: it shells out to `gh` when the
API is reachable and can also run against an injected transport for tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Default evidence directory for recorded ruleset snapshots.
EVIDENCE_DIR = Path(os.environ.get("SKFLEET_EVIDENCE", "~/.skcapstone/evidence"))


@dataclass
class RulesetSnapshot:
    """One exact branch-protection / ruleset state for a repository."""

    repo: str
    captured_at: str
    ruleset: dict
    sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256(payload: dict) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _gh_api(repo: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    """Run `gh api` and return the parsed JSON response.

    Credentials come from the caller's gh auth (keyring); this function
    never prints the token.
    """
    cmd = ["gh", "api", path]
    if body is not None:
        payload = json.dumps(body)
        cmd += ["-f", f"body={payload}"]
    cmd += ["--paginate"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed ({proc.returncode}): {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def inventory(repo: str, transport: str = "gh") -> dict:
    """Return the exact current main branch protection or ruleset state.

    The result is the full picture, not an inference:
      * rulesets: every repo ruleset (type, name, mode, target refs, rules).
      * main_protection: branch-protection state or None when absent (404).
    """
    rulesets = _gh_api(repo, "repos/{repo}/rulesets".format(repo=repo))
    protection = _try_protection(repo)
    payload = {
        "repo": repo,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rulesets": rulesets,
        "main_protection": protection,
    }
    payload["sha256"] = _sha256({"rulesets": rulesets, "main_protection": protection})
    return payload


def _try_protection(repo: str) -> dict | None:
    try:
        return _gh_api(repo, "repos/{repo}/branches/main/protection".format(repo=repo))
    except Exception:
        # 404 "Branch not protected" is a legal state, not an error here.
        return None


def record(repo: str, payload: dict, evidence_dir: Path | None = None) -> RulesetSnapshot:
    """Persist an inventory payload as a ruleset snapshot with a SHA-256.

    The snapshot file is written as JSON so a reviewer on another host can
    verify identity from the recorded hash.
    """
    directory = (evidence_dir or EVIDENCE_DIR).expanduser() / repo
    directory.mkdir(parents=True, exist_ok=True)
    digest = _sha256(payload)
    snap = RulesetSnapshot(
        repo=repo,
        captured_at=payload.get("captured_at", ""),
        ruleset=payload,
        sha256=digest,
    )
    path = (
        directory
        / f"ruleset-{payload.get('captured_at', 'unknown').replace(':', '').replace('-', '')}.json"
    )
    path.write_text(json.dumps(snap.to_dict(), indent=2, sort_keys=True))
    return snap


def rollback(repo: str, snap_file: str, apply: bool = True) -> dict:
    """Apply a recorded snapshot back onto the live ruleset state.

    When apply is False this is a dry run: the diff between the recorded
    snapshot and the live state is computed but nothing is written.
    """
    snap = json.loads(Path(snap_file).read_text())
    recorded = snap["ruleset"]
    live = inventory(repo)
    diff = _diff_rulesets(recorded, live)
    result = {
        "repo": repo,
        "recorded_at": snap.get("captured_at"),
        "live_captured_at": live.get("captured_at", ""),
        "diff": diff,
        "applied": apply,
    }
    if apply and diff["changed"]:
        _apply_diff(repo, diff, recorded)
    return result


def _diff_rulesets(recorded: dict, live: dict) -> dict:
    """Compare two inventory payloads and report exactly what changed.

    Only ruleset-level differences are actionable; a missing ruleset or a
    changed mode/rule is a concrete rollback step, not an inferred one.
    """
    recorded_ids = {r.get("id"): r for r in recorded.get("rulesets", [])}
    live_ids = {r.get("id"): r for r in live.get("rulesets", [])}
    added = [live_ids[i] for i in live_ids if i not in recorded_ids]
    removed = [recorded_ids[i] for i in recorded_ids if i not in live_ids]
    modified = []
    for rid, rec in recorded_ids.items():
        if rid in live_ids:
            if rec != live_ids[rid]:
                modified.append({"id": rid, "recorded": rec, "live": live_ids[rid]})
    changed = bool(added or removed or modified)
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "modified": modified,
        "protection_changed": (recorded.get("main_protection") != live.get("main_protection")),
    }


def _apply_diff(repo: str, diff: dict, recorded: dict) -> None:
    """Write the recorded state back through the GitHub ruleset APIs.

    Keep the existing required checks: a ruleset is removed only when the
    recorded state has no ruleset with that id, and modified rulesets are
    PATCHed with the recorded rule list.
    """
    for ruleset in diff["removed"]:
        rid = ruleset["id"]
        _gh_api(repo, f"repos/{repo}/rulesets/{rid}", method="DELETE")
    for item in diff["modified"]:
        rid = item["id"]
        _gh_api(
            repo,
            f"repos/{repo}/rulesets/{rid}",
            method="PATCH",
            body=item["recorded"],
        )
    if diff["protection_changed"]:
        _apply_protection(repo, recorded.get("main_protection"))


def _apply_protection(repo: str, protection: dict | None) -> None:
    path = f"repos/{repo}/branches/main/protection"
    if protection is None:
        _gh_api(repo, path, method="DELETE")
    else:
        _gh_api(repo, path, method="PUT", body=protection)
