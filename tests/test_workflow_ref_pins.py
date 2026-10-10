"""Every workflow ref resolves, or this suite goes red instead of absent.

This test is deliberately placed in the pytest suite rather than only in
docs-check. On 2026-09-19 a broken `uses:` ref in `.github/workflows/docs-check.yml`
made that workflow fail BEFORE creating a job, which publishes no check run at
all: the required gate went ABSENT, not red, and every consumer read the PR as
healthy. A guard that lives inside docs-check cannot observe a broken
docs-check. `pytest.yml` has no cross-repo `uses:` of its own, so it still runs
when a cross-repo ref is broken, which makes it a valid vantage point.

The network half (does the pinned sha still resolve upstream) runs in the
`shim-imports` job, not here: a unit suite that reaches the GitHub API would be
flaky and would fail closed for the wrong reason. Here we assert only what is
decidable offline, which is exactly the abbreviated-sha and unpinned-ref class.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GUARD = PROJECT_ROOT / "scripts" / "ci" / "workflow_refs.py"


def _load():
    spec = importlib.util.spec_from_file_location("workflow_refs", GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    assert GUARD.is_file(), f"{GUARD} is missing; the ref guard has been removed"
    return _load()


def test_repo_workflow_refs_are_all_well_formed(guard):
    """The live repo: no abbreviated shas, every reusable workflow sha-pinned."""
    pins, findings = guard.collect(PROJECT_ROOT)
    failures = [f for f in findings if f.level == "fail"]
    assert not failures, "\n".join(f.render() for f in failures)


def test_there_is_something_to_check(guard):
    """Zero workflows parsed would make the assertion above vacuously true.

    This is the same defect the guard exists to prevent, one level up: a test
    that passes because it observed nothing is not a passing test.
    """
    assert guard.workflow_files(PROJECT_ROOT), "no workflow files found -- nothing was checked"


def test_guard_catches_an_abbreviated_reusable_workflow_sha(guard, tmp_path):
    """The 2026-09-19 09:29Z break, reproduced."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9f\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "ABBREVIATED" in f.msg for f in findings)


# The exact reviewed dependency graph of the pytest workflow, card 2bd7d1c8.
# Every sibling install in .github/workflows/pytest.yml must resolve to
# EXACTLY these commits -- not merely to "some full-length sha". Review
# 3f9d3f44 mutation-tested the weaker shape-only assertion (swapping in
# forty a's kept the suite green), so an unreviewed pin swap must fail here.
# Bump a pin only through an SKCapstone review that reruns the suite against
# the new revision, and update this mapping in the same change.
REVIEWED_SIBLING_PINS = {
    "https://github.com/smilinTux/skcoord": "47cf3b1d6cbfbda5a07f32ae3a9ed9ad59de6217",
    "https://github.com/smilinTux/skdashboard": "d4c132284cda9186b5f9069803573c561f2f83b8",
    "https://github.com/smilinTux/skharness": "7409a1ab28f9c8fed87cd40226f4c031ca9e3f6e",
}

_VCS_REQUIREMENT = re.compile(
    r'"(?P<pkg>[A-Za-z0-9_.-]+) @ git\+(?P<url>https://github\.com/[^@"]+)@(?P<rev>[^"]+)"'
)


def _sibling_requirements(workflow_text: str) -> dict[str, list[tuple[str, str]]]:
    """Map package name -> [(repo url, revision)] for every VCS install."""
    out: dict[str, list[tuple[str, str]]] = {}
    for match in _VCS_REQUIREMENT.finditer(workflow_text):
        out.setdefault(match.group("pkg"), []).append((match.group("url"), match.group("rev")))
    return out


def test_pytest_sibling_installs_are_immutable_reviewed_pins():
    """Card 2bd7d1c8 regression: the pytest workflow must not resolve a
    moving branch for its sibling installs, and must install exactly the
    reviewed revisions.

    Before the fix the workflow ran ``git ls-remote ... HEAD`` for skdashboard
    and skharness and installed whatever that resolved to, so a foreign
    repository change could break SKCapstone trunk with no SKCapstone commit
    (observed 2026-09-20: a pinned-candidate run failed collection because
    main's tests imported ``skdashboard.assistant_client``). Review 3f9d3f44
    additionally proved that a shape-only 40-hex assertion accepts ANY commit
    -- its reviewer swapped the approved skcoord sha for forty a's and the
    focused suite stayed green -- so this test pins the exact reviewed
    mapping and requires exactly one VCS requirement per sibling.
    """
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8")
    assert "git ls-remote" not in workflow, "sibling installs must not resolve a moving ref"
    found = _sibling_requirements(workflow)
    approved_urls = set(REVIEWED_SIBLING_PINS)
    assert set(found) == {"skcoord", "skdashboard", "skharness"}, (
        f"sibling install set changed: found {sorted(found)}; the pytest "
        "workflow must install exactly skcoord, skdashboard and skharness"
    )
    for package, requirements in found.items():
        assert len(requirements) == 1, (
            f"{package} must have exactly one VCS requirement in pytest.yml, "
            f"found {len(requirements)}: {requirements}"
        )
        url, revision = requirements[0]
        assert url in approved_urls, f"{package} installed from unapproved repo {url!r}"
        approved = REVIEWED_SIBLING_PINS[url]
        assert revision == approved, (
            f"{package} is pinned to {revision!r} but the reviewed pin for "
            f"{url} is {approved!r}; an unreviewed dependency change is "
            "exactly what card 2bd7d1c8 exists to prevent"
        )


def test_pytest_workflow_has_no_unpinned_git_urls():
    """No sibling may appear as a bare ``git+URL`` with no ``@rev``.

    A requirement like ``git+https://.../skdashboard`` (no fragment) resolves
    to the moving default branch; the regex in the mapping test skips it, so
    this companion catches the unpinned form directly.
    """
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8")
    for line in workflow.splitlines():
        stripped = line.strip()
        if " @ git+" in stripped and stripped.endswith('"'):
            assert (
                "@" in stripped.rsplit("git+", 1)[1]
            ), f"VCS requirement without an immutable revision: {stripped!r}"


def test_reviewed_sibling_pins_are_full_40_hex_commits():
    """The approved mapping itself is well formed: full 40-hex lowercase."""
    for url, revision in REVIEWED_SIBLING_PINS.items():
        assert len(revision) == 40 and all(
            char in "0123456789abcdef" for char in revision
        ), f"approved pin for {url} is not a full 40-hex commit: {revision!r}"


def test_skharness_pin_matches_pyproject_reviewed_pin():
    """The workflow's skharness install must equal pyproject.toml's pin.

    pyproject.toml already carried the reviewed skharness commit; divergence
    between the two would mean CI tests a different graph than users install.
    """
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8")
    harness_pin = REVIEWED_SIBLING_PINS["https://github.com/smilinTux/skharness"]
    assert (
        harness_pin in pyproject
    ), "skharness workflow pin diverged from the reviewed pyproject.toml pin"
    assert harness_pin in workflow


def test_guard_catches_a_mutable_branch_pin(guard, tmp_path):
    """A branch pin is failure mode 1 waiting to happen: delete it and the gate
    goes absent rather than red."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/docs-check.yml@main\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "full 40-hex sha" in f.msg for f in findings)


def test_guard_catches_an_abbreviated_ref_shaped_input(guard, tmp_path):
    """`standards-ref` selects which validator actually runs and drifts
    independently of the `uses:` pin, so it needs the same rule."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n"
        "    with:\n      standards-ref: 8a799322af9f\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "standards-ref" in f.msg for f in findings)


def test_guard_does_not_flag_a_correct_full_sha_pin(guard, tmp_path):
    """Positive control. A guard that flags everything is as useless as one that
    flags nothing."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n",
        encoding="utf-8",
    )
    pins, findings = guard.collect(tmp_path)
    assert not findings
    assert len(pins) == 1


def test_unparseable_workflow_is_a_failure_not_a_skip(guard, tmp_path):
    """A workflow we could not read is a workflow we did not check. Reporting
    that as clean is the CardStore.fold defect in miniature."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "broken.yml").write_text("jobs:\n  docs:\n   uses: [\n", encoding="utf-8")
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "could not parse" in f.msg for f in findings)


def test_guard_self_test_passes():
    """Run the script's own negative control the way CI would."""
    r = subprocess.run(
        [sys.executable, str(GUARD), "--self-test"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
