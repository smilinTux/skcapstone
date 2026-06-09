"""`skcapstone scheduler` — manage the unified job scheduler."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import click

from .. import AGENT_HOME
from ..scheduler_jobs import load_jobs, current_host_aliases, job_runs_here
from ..scheduler_runner import JobRunner
from ..scheduler_state import SchedulerState


def _home() -> Path:
    """Return the effective SKCAPSTONE_HOME path.

    Reads from the ``SKCAPSTONE_HOME`` environment variable when set,
    falling back to the package-level ``AGENT_HOME`` constant.

    Returns:
        Resolved :class:`~pathlib.Path` for the agent home directory.
    """
    return Path(os.environ.get("SKCAPSTONE_HOME", AGENT_HOME))


def _jobs_path() -> Path:
    """Return the path to the synced ``jobs.yaml`` registry.

    Returns:
        ``<SKCAPSTONE_HOME>/config/jobs.yaml`` as a :class:`~pathlib.Path`.
    """
    return _home() / "config" / "jobs.yaml"


def register_scheduler_commands(main: click.Group) -> None:
    """Register the ``scheduler`` command group onto *main*.

    Adds the following sub-commands:

    - ``scheduler list``   — list all configured jobs with run status.
    - ``scheduler status`` — show last-run state for this node.
    - ``scheduler run``    — execute a job immediately.
    - ``scheduler logs``   — tail the most recent log for a job.
    - ``scheduler enable`` — enable a job in ``jobs.yaml``.
    - ``scheduler disable`` — disable a job in ``jobs.yaml``.

    Args:
        main: The top-level :class:`click.Group` to attach commands to.
    """

    @main.group("scheduler")
    def scheduler() -> None:
        """Manage the unified job scheduler (skscheduler)."""

    @scheduler.command("list")
    def list_jobs() -> None:
        """List all configured jobs and where they run."""
        jobs = load_jobs(_jobs_path())
        if not jobs:
            click.echo("No jobs configured.")
            return
        here = current_host_aliases()
        for j in jobs:
            sched = j.schedule or (f"every {int(j.every_seconds)}s" if j.every_seconds else "-")
            mark = "x" if (j.enabled and job_runs_here(j, here)) else " "
            click.echo(f"[{mark}] {j.name:24s} {j.type:6s} {sched:18s} nodes={j.nodes}")

    @scheduler.command("status")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def status(as_json: bool) -> None:
        """Show last-run status for this node."""
        st = SchedulerState(root=_home(), hostname=socket.gethostname())
        data = st.all()
        if as_json:
            click.echo(json.dumps(data, indent=2))
            return
        if not data:
            click.echo("No run history on this node yet.")
            return
        for name, rec in data.items():
            click.echo(
                f"{name:24s} last={rec.get('last_run')} status={rec.get('last_status')} "
                f"runs={rec.get('run_count')} errors={rec.get('error_count')}"
            )

    @scheduler.command("run")
    @click.argument("job_name")
    def run_now(job_name: str) -> None:
        """Run a job now on this node (manual override; ignores schedule and affinity)."""
        jobs = {j.name: j for j in load_jobs(_jobs_path())}
        job = jobs.get(job_name)
        if not job:
            raise click.ClickException(f"Unknown job: {job_name}")
        runner = JobRunner(log_dir=_home() / "scheduler" / socket.gethostname() / "logs")
        result = runner.run(job)
        # Record state + fire the job's notify policy so a manual run is observable in
        # `scheduler status` and exercises the sk-alert hook (same as the scheduled path).
        from datetime import datetime, timezone
        from ..scheduled_tasks import TaskScheduler
        SchedulerState(_home(), socket.gethostname()).record_run(
            job.name, now=datetime.now(timezone.utc), ok=result.ok, error=result.error)
        TaskScheduler._maybe_notify(job, result, attempts=1)
        if result.output:
            click.echo(result.output.strip())
        if not result.ok:
            raise click.ClickException(f"Job failed: {result.error}")
        click.echo(f"OK {job_name} done")

    @scheduler.command("logs")
    @click.argument("job_name")
    @click.option("--tail", default=40, show_default=True, help="Number of lines to show.")
    def logs(job_name: str, tail: int) -> None:
        """Show the latest log for a job on this node."""
        log_dir = _home() / "scheduler" / socket.gethostname() / "logs"
        matches = sorted(log_dir.glob(f"{job_name}-*.log")) if log_dir.exists() else []
        if not matches:
            click.echo(f"No logs for '{job_name}'.")
            return
        lines = matches[-1].read_text(encoding="utf-8").splitlines()
        click.echo("\n".join(lines[-tail:]))

    @scheduler.command("enable")
    @click.argument("job_name")
    def enable(job_name: str) -> None:
        """Enable a job (sets enabled: true in jobs.yaml)."""
        _set_enabled(job_name, True)
        click.echo(f"enabled {job_name}")

    @scheduler.command("disable")
    @click.argument("job_name")
    def disable(job_name: str) -> None:
        """Disable a job (sets enabled: false in jobs.yaml)."""
        _set_enabled(job_name, False)
        click.echo(f"disabled {job_name}")


def _set_enabled(job_name: str, value: bool) -> None:
    """Set the ``enabled`` flag for *job_name* in ``jobs.yaml``.

    Reads the current ``jobs.yaml``, toggles the ``enabled`` field for the
    named job, and writes the file back atomically via
    :func:`yaml.safe_dump`.

    Args:
        job_name: The job key to update.
        value: ``True`` to enable the job; ``False`` to disable.

    Raises:
        click.ClickException: If *job_name* is not found in the config.
    """
    import yaml

    path = _jobs_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = data.get("jobs") or {}
    if job_name not in jobs:
        raise click.ClickException(f"Unknown job: {job_name}")
    jobs[job_name]["enabled"] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
