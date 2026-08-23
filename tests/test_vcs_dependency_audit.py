"""Exact VCS dependency vulnerability-audit split tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from skcapstone.qualification.vcs_audit import (
    VcsAuditError,
    VcsDependencyPolicy,
    prepare_vcs_audit,
    run_vcs_audit,
)

COMMIT = "1" * 40
URL = "https://github.com/example/capauth.git"


def _policy(**changes):
    base = VcsDependencyPolicy(name="capauth", canonical_url=URL, commit=COMMIT, version="0.3.1")
    return replace(base, **changes)


def _write_inputs(tmp_path, *, requirement=None, source=None, dependencies=None):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        (requirement or f"capauth @ git+{URL}@{COMMIT}")
        + "\n"
        + "click==8.2.0 \\\n+    --hash=sha256:"
        + "a" * 64
        + "\n",
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\nrevision = 3\n\n"
        "[[package]]\n"
        'name = "capauth"\n'
        'version = "0.3.1"\n'
        f'source = {{ git = "{source or f"{URL}?rev={COMMIT}#{COMMIT}"}" }}\n'
        "dependencies = [\n"
        + "".join(f'    {{ name = "{name}" }},\n' for name in (dependencies or ["click"]))
        + "]\n\n"
        "[[package]]\n"
        'name = "click"\n'
        'version = "8.2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    return requirements, lock


def test_exact_vcs_line_is_removed_and_registry_hashes_remain(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)

    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )

    filtered = plan.registry_requirements.read_text(encoding="utf-8")
    assert "git+" not in filtered
    assert "click==8.2.0" in filtered
    assert "--hash=sha256:" in filtered
    assert plan.vcs_release_requirements[0].read_text() == "capauth==0.3.1\n"


@pytest.mark.parametrize(
    "requirement",
    [
        f"capauth @ git+{URL}@main",
        f"capauth @ git+https://github.com/other/capauth.git@{COMMIT}",
        f"capauth @ git+{URL}@{'2' * 40}",
        f"capauth @ git+{URL}@{COMMIT}\nother @ git+{URL}@{'2' * 40}",
    ],
)
def test_branch_tag_url_commit_and_extra_vcs_lines_fail(tmp_path, requirement) -> None:
    requirements, lock = _write_inputs(tmp_path, requirement=requirement)

    with pytest.raises(VcsAuditError, match="VCS requirement"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "audit",
            installed_versions={"capauth": "0.3.1"},
        )


@pytest.mark.parametrize(
    ("source", "policy", "message"),
    [
        (
            f"https://github.com/other/capauth.git?rev={COMMIT}#{COMMIT}",
            _policy(),
            "lock source",
        ),
        (f"{URL}?rev={'2' * 40}#{'2' * 40}", _policy(), "lock source"),
        (f"{URL}?rev={COMMIT}#{COMMIT}", _policy(version="9.9.9"), "lock version"),
    ],
)
def test_lock_url_commit_and_version_drift_fail(tmp_path, source, policy, message) -> None:
    requirements, lock = _write_inputs(tmp_path, source=source)

    with pytest.raises(VcsAuditError, match=message):
        prepare_vcs_audit(
            requirements,
            lock,
            [policy],
            tmp_path / "audit",
            installed_versions={"capauth": policy.version},
        )


def test_installed_version_and_dependency_omission_fail(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    with pytest.raises(VcsAuditError, match="installed version"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "version",
            installed_versions={"capauth": "0.3.0"},
        )

    requirements.write_text(f"capauth @ git+{URL}@{COMMIT}\n", encoding="utf-8")
    with pytest.raises(VcsAuditError, match="dependency omission"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "missing",
            installed_versions={"capauth": "0.3.1"},
        )


def test_unhashed_registry_requirement_fails(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    requirements.write_text(f"capauth @ git+{URL}@{COMMIT}\nclick==8.2.0\n", encoding="utf-8")

    with pytest.raises(VcsAuditError, match="hash-required"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "audit",
            installed_versions={"capauth": "0.3.1"},
        )


def test_zero_and_multiple_vcs_dependencies_are_supported(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    requirements.write_text(
        "click==8.2.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    lock.write_text(
        'version = 1\n[[package]]\nname = "click"\nversion = "8.2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    zero = prepare_vcs_audit(requirements, lock, [], tmp_path / "zero")
    assert zero.vcs_release_requirements == ()

    second_commit = "2" * 40
    requirements.write_text(
        f"capauth @ git+{URL}@{COMMIT}\n"
        f"helper @ git+https://github.com/example/helper.git@{second_commit}\n"
        "click==8.2.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    lock.write_text(
        "version = 1\n"
        '[[package]]\nname = "capauth"\nversion = "0.3.1"\n'
        f'source = {{ git = "{URL}?rev={COMMIT}#{COMMIT}" }}\n'
        '[[package]]\nname = "helper"\nversion = "1.0.0"\n'
        'source = { git = "https://github.com/example/helper.git'
        f'?rev={second_commit}#{second_commit}" }}\n'
        '[[package]]\nname = "click"\nversion = "8.2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    multiple = prepare_vcs_audit(
        requirements,
        lock,
        [
            _policy(),
            VcsDependencyPolicy(
                name="helper",
                canonical_url="https://github.com/example/helper.git",
                commit=second_commit,
                version="1.0.0",
            ),
        ],
        tmp_path / "multiple",
        installed_versions={"capauth": "0.3.1", "helper": "1.0.0"},
    )
    assert len(multiple.vcs_release_requirements) == 2


def test_runner_reconciles_hashed_registry_and_release_audits(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )
    calls = []

    def runner(command):
        calls.append(command)
        return {"returncode": 0, "stdout": "No known vulnerabilities", "stderr": ""}

    receipt = run_vcs_audit(plan, runner=runner, executable="pip-audit")

    assert len(calls) == 2
    assert "--require-hashes" in calls[0]
    assert "--no-deps" in calls[1]
    assert receipt.passed is True
    payload = json.loads(receipt.receipt_path.read_text())
    assert payload["limitations"]
    assert payload["registry_sha256"] == plan.registry_sha256
    assert payload["release_sha256"] == list(plan.release_sha256)
    assert payload["plan_sha256"] == plan.plan_sha256


def test_any_audit_failure_fails_the_reconciled_result(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )
    results = iter([0, 1])

    def runner(_command):
        return {"returncode": next(results), "stdout": "", "stderr": "failure"}

    receipt = run_vcs_audit(plan, runner=runner)
    assert receipt.passed is False


def test_vcs_policy_rejects_credentials_and_url_modifiers() -> None:
    with pytest.raises(VcsAuditError, match="credentials or modifiers"):
        _policy(canonical_url="https://user:secret@example.com/capauth.git")
    with pytest.raises(VcsAuditError, match="HTTPS .git URL|credentials or modifiers"):
        _policy(canonical_url=f"{URL}?ref=main")


def test_vcs_dependency_cannot_also_appear_as_registry_requirement(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    requirements.write_text(
        f"capauth @ git+{URL}@{COMMIT}\n"
        "capauth==0.3.1 \\\n+    --hash=sha256:" + "b" * 64 + "\n"
        "click==8.2.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )

    with pytest.raises(VcsAuditError, match="duplicated as a registry"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "audit",
            installed_versions={"capauth": "0.3.1"},
        )


@pytest.mark.parametrize("target", ["registry", "release", "plan"])
def test_execution_rejects_mutated_prepared_plan_files(tmp_path, target) -> None:
    requirements, lock = _write_inputs(tmp_path)
    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )
    paths = {
        "registry": plan.registry_requirements,
        "release": plan.vcs_release_requirements[0],
        "plan": plan.output_dir / "plan.json",
    }
    paths[target].chmod(0o644)
    paths[target].write_text("", encoding="utf-8")
    calls = []

    with pytest.raises(VcsAuditError, match="changed after validation"):
        run_vcs_audit(plan, runner=lambda command: calls.append(command))
    assert calls == []


def test_receipt_redacts_subprocess_credentials(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )

    receipt = run_vcs_audit(
        plan,
        runner=lambda _command: {
            "returncode": 0,
            "stdout": 'prefix={"api_key":"synthetic-secret-value"}',
            "stderr": (
                "Authorization: Bearer synthetic-bearer\n"
                '{"authorization":"Bearer synthetic-json-bearer",'
                '"client_secret":"synthetic-client-secret",'
                '"refresh_token":"synthetic-refresh-token"}'
            ),
        },
    )

    rendered = receipt.receipt_path.read_text(encoding="utf-8")
    assert "synthetic-secret-value" not in rendered
    assert "synthetic-bearer" not in rendered
    assert "synthetic-json-bearer" not in rendered
    assert "synthetic-client-secret" not in rendered
    assert "synthetic-refresh-token" not in rendered
    assert "[REDACTED]" in rendered


def test_malformed_lock_record_types_fail_closed(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    lock.write_text(
        'version = 1\n[[package]]\nname = "capauth"\nversion = "0.3.1"\nsource = 7\n',
        encoding="utf-8",
    )

    with pytest.raises(VcsAuditError, match="lock source is invalid"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "audit",
            installed_versions={"capauth": "0.3.1"},
        )


def test_malformed_lock_dependency_record_fails_closed(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    rendered = lock.read_text(encoding="utf-8")
    lock.write_text(
        rendered.replace('dependencies = [\n    { name = "click" },\n]', "dependencies = [7]"),
        encoding="utf-8",
    )

    with pytest.raises(VcsAuditError, match="lock dependency record is invalid"):
        prepare_vcs_audit(
            requirements,
            lock,
            [_policy()],
            tmp_path / "audit",
            installed_versions={"capauth": "0.3.1"},
        )


def test_missing_audit_executable_and_receipt_symlink_fail_closed(tmp_path) -> None:
    requirements, lock = _write_inputs(tmp_path)
    plan = prepare_vcs_audit(
        requirements,
        lock,
        [_policy()],
        tmp_path / "audit",
        installed_versions={"capauth": "0.3.1"},
    )

    def missing(_command):
        raise FileNotFoundError("synthetic missing executable")

    with pytest.raises(VcsAuditError, match="executable is unavailable"):
        run_vcs_audit(plan, runner=missing)

    external = tmp_path / "external.json"
    external.write_text("unchanged\n", encoding="utf-8")
    (plan.output_dir / "audit-receipt.json").symlink_to(external)
    with pytest.raises(VcsAuditError, match="already exists or is unsafe"):
        run_vcs_audit(
            plan,
            runner=lambda _command: {"returncode": 0, "stdout": "", "stderr": ""},
        )
    assert external.read_text(encoding="utf-8") == "unchanged\n"
