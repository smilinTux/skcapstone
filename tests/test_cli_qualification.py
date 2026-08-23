"""CLI coverage for qualification trust-boundary commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.cli import main


def test_checkpoint_cli_create_verify_and_review(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("print('ok')\n", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint"
    runner = CliRunner()

    created = runner.invoke(
        main,
        [
            "qualify",
            "checkpoint-create",
            "--workspace",
            str(workspace),
            "--output",
            str(checkpoint),
            "--path",
            "app.py",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "inventory_sha256" in created.output

    verified = runner.invoke(
        main,
        [
            "qualify",
            "checkpoint-verify",
            "--workspace",
            str(workspace),
            "--checkpoint",
            str(checkpoint),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert '"sealed": true' in verified.output

    reviewed = runner.invoke(
        main,
        [
            "qualify",
            "checkpoint-review",
            "--workspace",
            str(workspace),
            "--checkpoint",
            str(checkpoint),
            "--reviewer",
            "independent-reviewer",
            "--disposition",
            "accept",
        ],
    )
    assert reviewed.exit_code == 0, reviewed.output
    assert "independent-reviewer/receipt.json" in reviewed.output


def test_checkpoint_cli_reports_drift_without_traceback(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "app.py"
    source.write_text("one\n", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint"
    runner = CliRunner()
    created = runner.invoke(
        main,
        [
            "qualify",
            "checkpoint-create",
            "--workspace",
            str(workspace),
            "--output",
            str(checkpoint),
            "--path",
            "app.py",
        ],
    )
    assert created.exit_code == 0
    source.write_text("two\n", encoding="utf-8")

    result = runner.invoke(
        main,
        [
            "qualify",
            "checkpoint-review",
            "--workspace",
            str(workspace),
            "--checkpoint",
            str(checkpoint),
            "--reviewer",
            "reviewer",
            "--disposition",
            "accept",
        ],
    )

    assert result.exit_code != 0
    assert "source drift" in result.output
    assert "Traceback" not in result.output


def test_vcs_policy_cli_rejects_duplicate_json_members(tmp_path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(
        '[{"name":"capauth","name":"other","canonical_url":'
        '"https://github.com/example/capauth.git","commit":"' + "1" * 40 + '","version":"0.3.1"}]',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "qualify",
            "audit-vcs",
            "--requirements",
            str(tmp_path / "missing.txt"),
            "--lock",
            str(tmp_path / "missing.lock"),
            "--policy",
            str(policy),
            "--output",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code != 0
    assert "policy is missing or invalid" in result.output
    assert "Traceback" not in result.output


def test_artifact_cli_prints_content_addressed_receipt(tmp_path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    report = source / "report.md"
    report.write_text("# Accepted\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    manifest = {
        "schema": "skcapstone-review-artifact/v1",
        "sealed": True,
        "disposition": "accepted",
        "producer": {"name": "reviewer", "version": "1"},
        "accepted_source_sha256": "a" * 64,
        "retention_policy": "review",
        "files": [{"path": "report.md", "sha256": digest, "kind": "report"}],
    }
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "qualify",
            "artifact-ingest",
            str(source),
            "--sink",
            str(tmp_path / "durable"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"acceptance_state": "accepted"' in result.output
    assert '"bundle_sha256"' in result.output


def test_qualification_cli_normalizes_malformed_and_non_utf8_inputs(tmp_path) -> None:
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "scan-manifest.json").write_text(
        json.dumps(
            {
                "schema": "skcapstone-review-artifact/v1",
                "sealed": True,
                "disposition": "accepted",
                "producer": {"name": "reviewer", "version": "1"},
                "accepted_source_sha256": 7,
                "retention_policy": "review",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    malformed = runner.invoke(
        main,
        ["qualify", "artifact-ingest", str(source), "--sink", str(tmp_path / "durable")],
    )
    assert malformed.exit_code != 0
    assert "accepted source digest is invalid" in malformed.output
    assert "Traceback" not in malformed.output

    policy = tmp_path / "policy.json"
    policy.write_bytes(b"\xff")
    invalid_text = runner.invoke(
        main,
        [
            "qualify",
            "audit-vcs",
            "--requirements",
            str(tmp_path / "requirements.txt"),
            "--lock",
            str(tmp_path / "uv.lock"),
            "--policy",
            str(policy),
            "--output",
            str(tmp_path / "audit"),
        ],
    )
    assert invalid_text.exit_code != 0
    assert "policy is missing or invalid" in invalid_text.output
    assert "Traceback" not in invalid_text.output

    policy.write_text(
        json.dumps(
            [
                {
                    "name": "capauth",
                    "canonical_url": 7,
                    "commit": "1" * 40,
                    "version": "0.3.1",
                }
            ]
        ),
        encoding="utf-8",
    )
    invalid_type = runner.invoke(
        main,
        [
            "qualify",
            "audit-vcs",
            "--requirements",
            str(tmp_path / "requirements.txt"),
            "--lock",
            str(tmp_path / "uv.lock"),
            "--policy",
            str(policy),
            "--output",
            str(tmp_path / "audit"),
        ],
    )
    assert invalid_type.exit_code != 0
    assert "VCS policy fields must be strings" in invalid_type.output
    assert "Traceback" not in invalid_type.output


def test_missing_pip_audit_is_normalized(tmp_path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "click==8.2.0 \\\n+    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "click"\nversion = "8.2.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text("[]\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "qualify",
            "audit-vcs",
            "--requirements",
            str(requirements),
            "--lock",
            str(lock),
            "--policy",
            str(policy),
            "--output",
            str(tmp_path / "audit"),
            "--pip-audit",
            "definitely-missing-pip-audit",
        ],
    )

    assert result.exit_code != 0
    assert "executable is unavailable" in result.output
    assert "Traceback" not in result.output
