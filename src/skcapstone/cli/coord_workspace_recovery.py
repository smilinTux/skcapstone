"""Governed CLI for preserving and reissuing an inactive claimed workspace."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import click

from ..card_store import CardStore
from ..fleet.workspace_lifecycle import WorkspaceProof, quarantine_and_reissue
from ._common import AGENT_HOME
from ._validators import validate_agent_name, validate_task_id


def register_coord_workspace_recovery(coord: click.Group) -> None:
    """Register the explicit, claim-fenced workspace recovery command."""

    @coord.command("workspace-quarantine-reissue")
    @click.argument("task_id")
    @click.option("--agent", required=True, help="Explicit operator identity.")
    @click.option("--claim-revision", required=True, help="Exact current claim generation.")
    @click.option("--workspace", required=True, type=click.Path(path_type=Path))
    @click.option("--repository", required=True, type=click.Path(path_type=Path))
    @click.option("--branch", required=True)
    @click.option("--head", required=True)
    @click.option("--base-revision", required=True)
    @click.option("--quarantine-root", required=True, type=click.Path(path_type=Path))
    @click.option("--reissue-path", required=True, type=click.Path(path_type=Path))
    @click.option("--home", default=AGENT_HOME, type=click.Path(path_type=Path))
    @click.option(
        "--expected-card-sha256",
        help="Required for an unowned backlog/ready card; hash of its canonical folded snapshot.",
    )
    @click.option(
        "--execute", is_flag=True, help="Write quarantine and create the clean worktree."
    )
    def workspace_quarantine_reissue(
        task_id,
        agent,
        claim_revision,
        workspace,
        repository,
        branch,
        head,
        base_revision,
        quarantine_root,
        reissue_path,
        home,
        expected_card_sha256,
        execute,
    ):
        """Preserve one inactive worktree and create a clean pinned reissue.

        This command never changes the card or claim. It accepts the exact
        active claim, or an unowned backlog/ready card fenced by its full
        canonical card snapshot hash.
        """
        validate_task_id(task_id)
        validate_agent_name(agent)
        card = CardStore(Path(home).expanduser()).fold(task_id)
        if card is None:
            raise click.ClickException(f"No card {task_id}.")
        status = str(card.status.value)
        active_claim = (
            card.owner == agent
            and str(card.meta.get("_claim_revision") or "") == claim_revision
            and status == "doing"
        )
        snapshot = json.dumps(card.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        snapshot_sha256 = hashlib.sha256(snapshot.encode()).hexdigest()
        unowned_recovery = (
            card.owner is None
            and status in {"backlog", "ready"}
            and expected_card_sha256 == snapshot_sha256
        )
        if not (active_claim or unowned_recovery):
            raise click.ClickException(
                "card claim changed; unowned recovery requires the exact "
                "backlog/ready card snapshot hash"
            )
        try:
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            current = subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise click.ClickException(f"cannot inspect exact worktree: {exc}") from exc
        status_records = [line for line in status.split("\0") if line]
        dirty_paths = len(status_records)
        untracked_paths = sum(record.startswith("??") for record in status_records)
        proof = WorkspaceProof(
            card_id=task_id,
            claim_revision=claim_revision,
            workspace=str(workspace),
            branch=branch,
            head=current,
            porcelain_sha256=hashlib.sha256(status.encode()).hexdigest(),
            dirty_paths=dirty_paths,
            untracked_paths=untracked_paths,
            active_processes=0,
            recovery_instructions=("verify quarantine manifest", "verify clean reissue"),
            outcome="quarantine-and-reissue",
        )
        if current != head:
            raise click.ClickException("workspace HEAD changed")
        try:
            result = quarantine_and_reissue(
                proof,
                repository=repository,
                quarantine_root=quarantine_root,
                reissue_path=reissue_path,
                base_revision=base_revision,
                execute=execute,
            )
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))
