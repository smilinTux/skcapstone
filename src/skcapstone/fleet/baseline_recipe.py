"""Derive a baseline recipe from a failed full-suite qualification.

A repository qualified with ``{"pytest_all": True}`` must pass its entire
suite inside the network-less sandbox at the card's base revision. skcapstone's
suite carries host-dependent tests (rollout drift, nested bwrap, systemd), so
every skcapstone card failed qualification forever (chi, 2026-10-11), and the
base revision is pinned per card, so fixing tests on main cannot help.

The trusted harness already supports the contract that fixes this: a
directory target plus a ``deselect`` baseline of exact node IDs, with the
selection plugin proving what ran and what was excluded. Operators wrote such
recipes by hand. This module writes the same recipe from the sealed evidence
of the failed run: every test that failed, errored or skipped at the base is
deselected, and everything else, including any test the candidate adds, must
pass. The recipe is attached as the card's ``test_profile_recipe`` link, which
both the authority and the builder already honor, so the next qualification is
a new attempt and runs it.

It refuses rather than guesses: only runs whose pytest exited 1 (tests ran;
never collection errors or interruption), whose every failing case resolves to
a real test file in the sealed workspace, and whose recipe validates and fits
in a card link.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from . import production_test_plan as plan
from .production_pytest_recipe import recipe_checks, validate_source

MAX_LINK = 16384
RECENT_SECONDS = 24 * 3600
FULL_SUITE = {"pytest_all": True}


class BaselineError(ValueError):
    """The failed run cannot yield an exact baseline."""


def _skcapstone() -> str:
    return shutil.which("skcapstone") or str(Path.home() / ".skenv/bin/skcapstone")


def node_id(classname: str, name: str, workspace: Path) -> str:
    """Map a JUnit identity to an exact pytest node ID inside ``workspace``."""
    parts = [part for part in (classname or "").split(".") if part]
    if not parts or not name:
        raise BaselineError("collection error or anonymous test case")
    for split in range(len(parts), 0, -1):
        path = "/".join(parts[:split]) + ".py"
        target = workspace / path
        if target.is_file() and not target.is_symlink():
            return "::".join([path, *parts[split:], name])
    raise BaselineError("test case does not resolve to a sealed test file")


def baseline_from_junit(raw: bytes, workspace: Path) -> list[str]:
    """Every failed, errored or skipped case as an exact node ID; refuse anything else."""
    if len(raw) > plan.MAX_OUTPUT or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise BaselineError("invalid JUnit size or entities")
    root = ElementTree.fromstring(raw)
    baseline = set()
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            baseline.add(node_id(case.get("classname", ""), case.get("name", ""), workspace))
    return sorted(baseline)


def baseline_recipe(deselect: list[str], workspace: Path) -> str:
    """The validated recipe text, or BaselineError."""
    if not deselect:
        raise BaselineError("failed run has no failing test cases")
    recipe = {
        "pytest": {"tests": 1},
        "deselect": deselect,
        "compile": [],
        "lint": [],
        "changelog": False,
    }
    try:
        recipe_checks(recipe)
        validate_source(recipe, workspace)
    except plan.TestEvidenceError as exc:
        raise BaselineError(str(exc)) from exc
    text = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_LINK:
        raise BaselineError("baseline exceeds the card link bound")
    return text


def _failed_run(home: Path, job: dict) -> tuple[bytes, Path]:
    """The failed run's raw report and sealed workspace, only for a full-suite failure."""
    if (job.get("initial_profile_value") or {}).get("recipe") != FULL_SUITE:
        raise BaselineError("not a full-suite qualification")
    _plan, _path, plan_sha = plan.load_plan(
        home,
        job["binding"],
        require_current=False,
        allow_completed=True,
        allow_remote_host=True,
    )
    directory = home / "fleet/test-runs" / plan_sha
    receipt = json.loads(plan.read_private(directory / "receipt.json"))
    checks = receipt.get("checks") or []
    if [check.get("id") for check in checks] != ["pytest"] or checks[0].get("exit_code") != 1:
        raise BaselineError("pytest did not run to completion with test failures")
    workspace = Path(job["workspace"])
    if not workspace.is_dir() or workspace.is_symlink():
        raise BaselineError("sealed qualification workspace is unavailable")
    return plan.read_private(directory / "pytest.xml"), workspace


def curate_failed_qualifications(
    home: Path, *, actor: str = "niobe", limit: int = 4, run=subprocess.run
) -> list[dict]:
    """Attach a baseline recipe to cards whose full-suite qualification failed."""
    import time

    from skcoord.card_store import CardStore

    home = Path(home)
    root = home / "fleet/profile-requalifications"
    store = CardStore(home)
    results, seen, linked_count = [], set(), 0
    failures = sorted(
        root.glob("*.job.failed.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for failed in failures:
        # Count only links: refusals must not starve later cards (cf. #1106).
        if linked_count >= limit or len(results) >= 4 * limit + 16:
            break
        if time.time() - failed.stat().st_mtime > RECENT_SECONDS:
            break
        try:
            job = json.loads(
                plan.read_private(failed.with_name(failed.name.replace(".failed", "")))
            )
        except (OSError, ValueError):
            continue
        card_id = str(job.get("card") or "")
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        card = store.fold(card_id)
        if card is None or card.archived or (card.links or {}).get("test_profile_recipe"):
            continue
        try:
            raw, workspace = _failed_run(home, job)
            text = baseline_recipe(baseline_from_junit(raw, workspace), workspace)
        except (BaselineError, plan.TestEvidenceError, OSError, ValueError, KeyError) as exc:
            results.append({"card": card_id, "state": "refused", "reason": str(exc)[:120]})
            continue
        linked = run(
            [
                _skcapstone(),
                "coord",
                "link",
                card_id,
                "test_profile_recipe",
                text,
                "--agent",
                actor,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        linked_count += linked.returncode == 0
        results.append(
            {
                "card": card_id,
                "state": "linked" if linked.returncode == 0 else "link-refused",
                "deselected": len(json.loads(text)["deselect"]),
            }
        )
    return results
