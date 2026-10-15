"""runtime_fingerprint pins harness execution semantics, not harness bytes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fleet import harness_attest as attest
from skcapstone.fleet import production_test_plan as plan

FLEET = Path(plan.__file__).resolve().parent


def _touch_harness(suffix: str) -> None:
    for name in plan.HARNESS_MODULES:
        with (plan.HARNESS_ROOT / name).open("a") as handle:
            handle.write(suffix)


def test_comment_and_formatting_edits_do_not_restale(qualified_runtime):
    before = plan.runtime_fingerprint()
    _touch_harness("# a comment\n\n\n")
    plan._RUNTIME_CACHE.clear()
    assert plan.runtime_fingerprint() == before


def test_unrelated_code_edit_at_same_version_does_not_restale(qualified_runtime):
    before = plan.runtime_fingerprint()
    _touch_harness("def plan_naming_helper():\n    return 'renamed'\n")
    plan._RUNTIME_CACHE.clear()
    assert plan.runtime_fingerprint() == before


def test_python_execution_version_bump_restales_python(qualified_runtime, monkeypatch):
    v1 = plan.runtime_fingerprint("python")
    monkeypatch.setattr(plan, "PYTHON_HARNESS_EXECUTION_VERSION", 2)
    assert plan.runtime_fingerprint("python") != v1


def test_node_execution_version_only_restales_node_profiles(qualified_runtime, monkeypatch):
    python_before = plan.runtime_fingerprint("python")
    node_before = plan.runtime_fingerprint("node")
    monkeypatch.setattr(plan, "NODE_HARNESS_EXECUTION_VERSION", 3)
    assert plan.runtime_fingerprint("python") == python_before
    assert plan.runtime_fingerprint("node") != node_before


def test_python_family_matches_deployed_legacy_fingerprint(qualified_runtime):
    """The family split preserves the exact deployed Python fingerprint string."""
    assert plan.runtime_fingerprint("python") == plan.runtime_fingerprint()


@pytest.mark.parametrize("bad", [0, -1, True, "1", None])
@pytest.mark.parametrize("family", ["python", "node"])
def test_invalid_version_fails_closed(qualified_runtime, monkeypatch, bad, family):
    key = (
        "PYTHON_HARNESS_EXECUTION_VERSION"
        if family == "python"
        else "NODE_HARNESS_EXECUTION_VERSION"
    )
    monkeypatch.setattr(plan, key, bad)
    with pytest.raises(plan.TestEvidenceError):
        plan.runtime_fingerprint(family)


def test_pin_table_must_match_harness_modules(qualified_runtime, monkeypatch):
    monkeypatch.setattr(plan, "_LEGACY_HARNESS_SHA256", {"qualified_runtime.py": "0" * 64})
    with pytest.raises(plan.TestEvidenceError, match="legacy pins"):
        plan.runtime_fingerprint()


def test_missing_or_redirected_harness_module_fails_closed(qualified_runtime, tmp_path):
    victim = plan.HARNESS_ROOT / plan.HARNESS_MODULES[1]
    original = victim.read_bytes()
    victim.unlink()
    with pytest.raises(FileNotFoundError):
        plan.runtime_fingerprint()
    outside = tmp_path / "elsewhere.py"
    outside.write_bytes(original)
    victim.symlink_to(outside)
    with pytest.raises(plan.TestEvidenceError, match="redirected"):
        plan.runtime_fingerprint()


def test_version_one_equals_the_legacy_byte_fingerprint(qualified_runtime, monkeypatch):
    """Migration: a profile sealed by the byte-hashing code stays valid at v1.

    The old code appended ("harness/<name>", sha256(bytes)) rows after everything
    else. Rebuild that value independently from the same rows and compare strings,
    which is all the profile comparison sites do.
    """
    blobs = []
    real_sha = plan.sha
    monkeypatch.setattr(plan, "sha", lambda data: blobs.append(data) or real_sha(data))
    legacy_pins = {
        name: real_sha((plan.HARNESS_ROOT / name).read_bytes()) for name in plan.HARNESS_MODULES
    }
    monkeypatch.setattr(plan, "_LEGACY_HARNESS_SHA256", legacy_pins)
    new_value = plan.runtime_fingerprint()
    rows = json.loads(blobs[-1])
    harness_rows = [row for row in rows if row[0].startswith("harness/")]
    assert harness_rows == [[f"harness/{n}", legacy_pins[n]] for n in plan.HARNESS_MODULES]
    assert rows[-len(harness_rows) :] == harness_rows  # legacy ordering: harness last
    assert new_value == real_sha(json.dumps(rows, separators=(",", ":")).encode())
    # And the shipped pins describe exactly the deployed harness modules.
    assert set(plan._LEGACY_HARNESS_SHA256) == set(plan.HARNESS_MODULES)
    assert all(len(v) == 64 for v in plan._LEGACY_HARNESS_SHA256.values())


def test_pre_deploy_profile_survives_post_deploy_byte_edits(qualified_runtime, monkeypatch):
    """Sealed with the old formula, then the harness bytes change: still equal."""
    legacy_pins = {
        name: plan.sha((plan.HARNESS_ROOT / name).read_bytes()) for name in plan.HARNESS_MODULES
    }
    monkeypatch.setattr(plan, "_LEGACY_HARNESS_SHA256", legacy_pins)
    sealed = plan.runtime_fingerprint()
    _touch_harness("# post-deploy refactor\nVALUE = 1\n")
    plan._RUNTIME_CACHE.clear()
    assert plan.runtime_fingerprint() == sealed


# ---- the CI guard -------------------------------------------------------------


def test_ledger_matches_the_harness_tree():
    version = plan.HARNESS_LEDGER_VERSION
    ledger = attest.HARNESS_EXECUTION_LEDGER
    assert sorted(ledger) == list(range(1, version + 1)), "ledger needs a row per version"
    assert set(ledger[version]) == set(plan.HARNESS_MODULES)
    drift = {
        name: digest
        for name, digest in attest.module_digests(FLEET, plan.HARNESS_MODULES).items()
        if ledger[version][name] != digest
    }
    assert not drift, (
        f"harness execution tree changed for {sorted(drift)} without a ledger update. "
        "If harness code changed, bump HARNESS_LEDGER_VERSION in "
        "production_test_plan.py and add a harness_attest.HARNESS_EXECUTION_LEDGER row. "
        "For execution semantics, also bump only the affected family version. "
        "If the change is behaviour-neutral, refresh this "
        "version's row (current digests: "
        f"{json.dumps(drift, indent=1)})."
    )


BASE = '''
"""Module doc."""
import os


def run(cmd, env):
    """Run it."""
    # inline comment
    return [os.fspath(cmd), *sorted(env)]
'''


def test_digest_ignores_comments_docstrings_and_formatting():
    reformatted = """
import os
def run(cmd,
        env):
    return [os.fspath(cmd),
            *sorted(env)]  # trailing
"""
    assert attest.ast_digest(BASE) == attest.ast_digest(reformatted)


@pytest.mark.parametrize(
    "mutation",
    [
        ("sorted(env)", "list(env)"),
        ("os.fspath(cmd)", "str(cmd)"),
        ("import os", "import os, sys"),
        ("def run(cmd, env)", "def run(cmd, env=None)"),
    ],
)
def test_digest_catches_execution_changes(mutation):
    old, new = mutation
    assert attest.ast_digest(BASE) != attest.ast_digest(BASE.replace(old, new))


def test_digest_excludes_only_the_bookkeeping_constants():
    a = (
        BASE
        + "HARNESS_LEDGER_VERSION = 1\n"
        + "PYTHON_HARNESS_EXECUTION_VERSION = 1\n"
        + "_LEGACY_HARNESS_SHA256 = {'a': 'b'}\n"
    )
    b = (
        BASE
        + "HARNESS_LEDGER_VERSION = 2\n"
        + "PYTHON_HARNESS_EXECUTION_VERSION = 2\n"
        + "_LEGACY_HARNESS_SHA256 = {}\n"
    )
    assert attest.ast_digest(a) == attest.ast_digest(b) == attest.ast_digest(BASE)
    assert attest.ast_digest(BASE + "OTHER = 1\n") != attest.ast_digest(BASE)


def test_guard_catches_an_unbumped_execution_change(tmp_path):
    """Edit a copy of the real recipe module so it does something different."""
    source = (FLEET / "production_pytest_recipe.py").read_text()
    (tmp_path / "production_pytest_recipe.py").write_text(source + "\n# only a comment\n")
    name = "production_pytest_recipe.py"
    committed = attest.HARNESS_EXECUTION_LEDGER[plan.HARNESS_LEDGER_VERSION][name]
    assert attest.module_digests(tmp_path, (name,))[name] == committed
    (tmp_path / name).write_text(source + "\nEXTRA_PYTEST_FLAG = '-p no:cacheprovider'\n")
    assert attest.module_digests(tmp_path, (name,))[name] != committed
