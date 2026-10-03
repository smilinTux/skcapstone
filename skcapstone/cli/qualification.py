"""Qualification, immutable checkpoint, and durable evidence commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..qualification.artifacts import ArtifactError, ingest_artifact_bundle
from ..qualification.checkpoint import (
    CheckpointError,
    create_checkpoint,
    record_review,
    seal_completion,
    verify_checkpoint,
)
from ..qualification.jsonutil import StrictJsonError, strict_json_loads
from ..qualification.vcs_audit import (
    VcsAuditError,
    VcsDependencyPolicy,
    prepare_vcs_audit,
    run_vcs_audit,
)
from ._common import AGENT_HOME, console


def _fail_closed(function, *args, **kwargs):
    """Normalize qualification boundary failures to concise CLI errors."""
    try:
        return function(*args, **kwargs)
    except (
        ArtifactError,
        CheckpointError,
        OSError,
        StrictJsonError,
        UnicodeDecodeError,
        VcsAuditError,
    ) as exc:
        raise click.ClickException(str(exc)) from None


def register_qualification_commands(main: click.Group) -> None:
    """Register qualification and evidence commands."""

    @main.group("qualify")
    def qualify() -> None:
        """Create review checkpoints and retain qualification evidence."""

    @qualify.command("checkpoint-create")
    @click.option("--workspace", required=True, type=click.Path(path_type=Path))
    @click.option("--output", required=True, type=click.Path(path_type=Path))
    @click.option("--path", "paths", multiple=True, required=True)
    def checkpoint_create(workspace: Path, output: Path, paths: tuple[str, ...]) -> None:
        """Snapshot exact files behind a paired SHA256 inventory."""
        result = _fail_closed(create_checkpoint, workspace, paths, output)
        console.print(
            json.dumps(
                {
                    "directory": str(result.directory),
                    "inventory": str(result.inventory_path),
                    "inventory_sha256": result.inventory_sha256,
                },
                indent=2,
            )
        )

    @qualify.command("checkpoint-verify")
    @click.option("--workspace", required=True, type=click.Path(path_type=Path))
    @click.option("--checkpoint", required=True, type=click.Path(path_type=Path))
    def checkpoint_verify(workspace: Path, checkpoint: Path) -> None:
        """Verify checkpoint snapshot and current workspace bytes."""
        receipt = _fail_closed(verify_checkpoint, checkpoint, workspace)
        console.print(
            json.dumps(
                {
                    "sealed": receipt.sealed,
                    "inventory_sha256": receipt.inventory_sha256,
                    "changed_paths": receipt.changed_paths,
                    "missing_paths": receipt.missing_paths,
                    "snapshot_errors": receipt.snapshot_errors,
                },
                indent=2,
            )
        )
        if not receipt.sealed:
            raise click.ClickException("checkpoint verification failed")

    @qualify.command("checkpoint-review")
    @click.option("--workspace", required=True, type=click.Path(path_type=Path))
    @click.option("--checkpoint", required=True, type=click.Path(path_type=Path))
    @click.option("--reviewer", required=True)
    @click.option("--disposition", required=True, type=click.Choice(["accept", "reject"]))
    @click.option("--notes", default="")
    def checkpoint_review(
        workspace: Path,
        checkpoint: Path,
        reviewer: str,
        disposition: str,
        notes: str,
    ) -> None:
        """Record an independent review bound to exact source bytes."""
        receipt = _fail_closed(
            record_review,
            checkpoint,
            workspace,
            reviewer=reviewer,
            disposition=disposition,
            notes=notes,
        )
        console.print(str(receipt.receipt_path))

    @qualify.command("checkpoint-complete")
    @click.option("--workspace", required=True, type=click.Path(path_type=Path))
    @click.option("--checkpoint", required=True, type=click.Path(path_type=Path))
    @click.option("--evidence", multiple=True)
    def checkpoint_complete(workspace: Path, checkpoint: Path, evidence: tuple[str, ...]) -> None:
        """Seal a completion inventory with only declared evidence changes."""
        receipt = _fail_closed(
            seal_completion,
            checkpoint,
            workspace,
            evidence_allowlist=set(evidence),
        )
        console.print(
            json.dumps(
                {
                    "accepted_inventory_sha256": receipt.accepted_inventory_sha256,
                    "completion_inventory_sha256": receipt.completion_inventory_sha256,
                    "changed_paths": receipt.changed_paths,
                    "receipt": str(receipt.receipt_path),
                },
                indent=2,
            )
        )

    @qualify.command("artifact-ingest")
    @click.argument("source", type=click.Path(path_type=Path))
    @click.option(
        "--sink",
        default=lambda: str(Path(AGENT_HOME).expanduser() / "evidence" / "artifacts"),
        type=click.Path(path_type=Path),
    )
    def artifact_ingest(source: Path, sink: Path) -> None:
        """Copy a validated review bundle into durable content-addressed storage."""
        receipt = _fail_closed(ingest_artifact_bundle, source, sink)
        console.print(
            json.dumps(
                {
                    "bundle_sha256": receipt.bundle_sha256,
                    "acceptance_state": receipt.acceptance_state,
                    "directory": str(receipt.directory),
                    "receipt": str(receipt.receipt_path),
                },
                indent=2,
            )
        )

    @qualify.command("audit-vcs")
    @click.option("--requirements", required=True, type=click.Path(path_type=Path))
    @click.option("--lock", "lock_path", required=True, type=click.Path(path_type=Path))
    @click.option("--policy", "policy_path", required=True, type=click.Path(path_type=Path))
    @click.option("--output", required=True, type=click.Path(path_type=Path))
    @click.option("--pip-audit", "pip_audit", default="pip-audit")
    def audit_vcs(
        requirements: Path,
        lock_path: Path,
        policy_path: Path,
        output: Path,
        pip_audit: str,
    ) -> None:
        """Audit registry packages with hashes and exact VCS releases separately."""
        try:
            raw = strict_json_loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, StrictJsonError):
            raise click.ClickException("VCS policy is missing or invalid") from None
        if not isinstance(raw, list):
            raise click.ClickException("VCS policy must be a JSON array")
        if not all(isinstance(item, dict) for item in raw):
            raise click.ClickException("each VCS policy must be an object")
        try:
            policies = [VcsDependencyPolicy(**item) for item in raw]
        except (TypeError, VcsAuditError) as exc:
            raise click.ClickException(str(exc)) from None
        plan = _fail_closed(prepare_vcs_audit, requirements, lock_path, policies, output)
        receipt = _fail_closed(run_vcs_audit, plan, executable=pip_audit)
        console.print(
            f"registry hash audit plus {len(policies)} VCS release audit(s): "
            f"{'PASS' if receipt.passed else 'FAIL'}"
        )
        console.print(str(receipt.receipt_path))
        if not receipt.passed:
            raise click.ClickException("reconciled vulnerability audit failed")
