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
in a card link. Missing run artifacts are pending evidence, and zero-exit
checks do not justify a failing-test baseline even if later qualification
validation failed. Both are skipped without changing the recipe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from . import production_test_plan as plan
from .production_pytest_recipe import recipe_checks, validate_source

MAX_LINK = 262144
RECENT_SECONDS = 24 * 3600
FULL_SUITE = {"pytest_all": True}


class BaselinePendingError(ValueError):
    """No failed pytest evidence is available to curate on this host."""


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


def analyze_junit(raw: bytes, workspace: Path) -> tuple[dict[str, int], list[str], set[str]]:
    """Passing cases per test file, exact baseline node IDs, and untargetable files.

    A module-level skip or collection error has no node ID to deselect, so its
    whole file is left out of the targets instead.
    """
    if len(raw) > plan.MAX_OUTPUT or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise BaselineError("invalid JUnit size or entities")
    root = ElementTree.fromstring(raw)
    passing: dict[str, int] = {}
    baseline, untargetable = set(), set()
    for case in root.iter("testcase"):
        bad = any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
        classname, name = case.get("classname", ""), case.get("name", "")
        if not classname:
            module = (name or "").replace(".", "/") + ".py"
            if not bad or not (workspace / module).is_file():
                raise BaselineError("collection error or anonymous test case")
            untargetable.add(module)
            continue
        identity = node_id(classname, name, workspace)
        path = identity.split("::", 1)[0]
        if bad:
            baseline.add(identity)
        else:
            passing[path] = passing.get(path, 0) + 1
    return passing, sorted(baseline), untargetable


def compact_targets(
    passing: dict[str, int], untargetable: set[str], top: str = "tests"
) -> list[str]:
    """Directory targets where every test file below is clean, else clean files.

    A clean file has at least one passing case and no module-level problem.
    Directory targets also cover test files a candidate adds there.
    """
    files = set(passing) | untargetable
    clean = {path for path in passing if path not in untargetable}

    def under(directory: str) -> set[str]:
        return {path for path in files if path.startswith(directory + "/")}

    def compact(directory: str) -> list[str]:
        below = under(directory)
        if below and below <= clean:
            return [directory]
        prefix = len(directory) + 1
        direct = {path for path in below if "/" not in path[prefix:]}
        targets = sorted(direct & clean)
        subdirs = sorted(
            {directory + "/" + path[prefix:].split("/", 1)[0] for path in below - direct}
        )
        for subdir in subdirs:
            targets.extend(compact(subdir))
        return targets

    return compact(top)


def baseline_from_junit(raw: bytes, workspace: Path) -> list[str]:
    """Every failed, errored or skipped case as an exact node ID (no module skips)."""
    _passing, baseline, untargetable = analyze_junit(raw, workspace)
    if untargetable:
        raise BaselineError("collection error or anonymous test case")
    return baseline


def baseline_recipe(deselect: list[str], workspace: Path, targets: list[str] | None = None) -> str:
    """The validated recipe text, or BaselineError."""
    targets = targets or ["tests"]
    if not deselect and targets == ["tests"]:
        raise BaselineError("failed run has no failing test cases")
    covered = [
        node
        for node in deselect
        if any(node.split("::", 1)[0] == t or node.startswith(t + "/") for t in targets)
    ]
    recipe = {
        "pytest": dict.fromkeys(targets, 1),
        "deselect": covered,
        "compile": [],
        "lint": [],
        "changelog": False,
        "file_cap": max(64, len(targets)),
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


def _failed_run(home: Path, job: dict) -> tuple[bytes, Path, dict]:
    """The failed run's raw report, sealed workspace and the recipe it ran."""
    run_plan, _path, plan_sha = plan.load_plan(
        home,
        job["binding"],
        require_current=False,
        allow_completed=True,
        allow_remote_host=True,
    )
    recipe = (run_plan.get("profile") or {}).get("recipe")
    if recipe is None:
        recipe = (job.get("initial_profile_value") or {}).get("recipe")
    if not isinstance(recipe, dict):
        raise BaselineError("failed run has no qualified recipe")
    directory = home / "fleet/test-runs" / plan_sha
    try:
        receipt = json.loads(plan.read_private(directory / "receipt.json"))
    except FileNotFoundError as exc:
        raise BaselinePendingError("awaiting failed-run artifacts on this host") from exc
    checks = receipt.get("checks") or []
    if checks and all(check.get("exit_code") == 0 for check in checks):
        raise BaselinePendingError("checks exited zero; no failing-test baseline")
    if "node_test" in recipe:
        from .baseline_node_recipe import failed_report

        workspace = Path(job["workspace"])
        try:
            return failed_report(directory, checks, recipe), workspace, recipe
        except FileNotFoundError as exc:
            raise BaselinePendingError("awaiting failed-run Node artifacts") from exc
    if [check.get("id") for check in checks] != ["pytest"] or checks[0].get("exit_code") != 1:
        raise BaselineError("pytest did not run to completion with test failures")
    workspace = Path(job["workspace"])
    if not workspace.is_dir() or workspace.is_symlink():
        raise BaselineError("sealed qualification workspace is unavailable")
    try:
        raw = plan.read_private(directory / "pytest.xml")
    except FileNotFoundError as exc:
        raise BaselinePendingError("awaiting failed-run JUnit on this host") from exc
    return raw, workspace, recipe


def _latest_link_writer(home: Path, card: str, key: str) -> str | None:
    """Writer of the newest link event for this card and key in the evidence overlay."""
    latest = ("", None)
    for path in sorted((home / "coordination/card_events").glob("*.jsonl")):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if card not in line or key not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                event.get("card_id") == card
                and event.get("action") == "link"
                and event.get("link_key") == key
                and str(event.get("ts", "")) >= latest[0]
            ):
                latest = (str(event.get("ts", "")), event.get("writer"))
    return latest[1]


def extended_recipe(current: dict, raw: bytes, workspace: Path) -> str:
    """A curated baseline recipe plus the tests that newly failed at the same base.

    A requalification runs at the card's base revision, so a test failing there
    is a pre-existing failure by definition. Host-dependent tests (staged
    rollout drift probes) pass while a host is in sync and fail during a
    rollout, so one baseline run cannot capture them (e6a9c210, 2026-10-11).
    """
    if "deselect" not in current or not isinstance(current.get("pytest"), dict):
        raise BaselineError("only a curated baseline recipe is extended")
    _passing, failing, untargetable = analyze_junit(raw, workspace)
    if untargetable:
        raise BaselineError("collection error or anonymous test case")
    merged = sorted(set(current["deselect"]) | set(failing))
    if merged == sorted(current["deselect"]):
        raise BaselineError("no new failing tests to add to the baseline")
    return baseline_recipe(merged, workspace, list(current["pytest"]))


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
        if card is None or card.archived:
            continue
        linked_recipe = (card.links or {}).get("test_profile_recipe")
        try:
            raw, workspace, ran = _failed_run(home, job)
            if linked_recipe:
                # Extend only a recipe this curator wrote, never an operator's.
                if (
                    _latest_link_writer(home, card_id, "test_profile_recipe") != actor
                    or json.loads(linked_recipe) != ran
                ):
                    continue
            if "node_test" in ran:
                from .baseline_node_recipe import baseline_recipe as node_baseline

                text = node_baseline(raw, workspace, ran)
            elif linked_recipe:
                text = extended_recipe(ran, raw, workspace)
            else:
                if ran != FULL_SUITE:
                    raise BaselineError("not a full-suite qualification")
                passing, baseline, untargetable = analyze_junit(raw, workspace)
                targets = compact_targets(passing, untargetable)
                if not targets:
                    raise BaselineError("no test file passes at the base")
                text = baseline_recipe(baseline, workspace, targets)
        except BaselinePendingError as exc:
            results.append({"card": card_id, "state": "skipped", "reason": str(exc)})
            continue
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
                "-",
                "--value-from-stdin",
                "--agent",
                actor,
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=60,
        )
        linked_count += linked.returncode == 0
        curated = json.loads(text)
        results.append(
            {
                "card": card_id,
                "state": "linked" if linked.returncode == 0 else "link-refused",
                "deselected": (
                    len(ran["node_test"]) - len(curated["node_test"])
                    if "node_test" in ran
                    else len(curated["deselect"])
                ),
                "targets": len(curated.get("node_test", curated.get("pytest", {}))),
            }
        )
    return results
