"""Worker-side persistence for exact child progress snapshots."""

from __future__ import annotations

import datetime
import os
import sys
import time
from typing import Callable

from .child_progress import (
    ChildProgressSnapshot,
    process_cgroup,
    process_start_ticks,
    snapshot_key,
    write_snapshot,
)
from .worker_watchdog import StartupObservation, classify_startup


def monitor_child_progress(
    args: object,
    child: object,
    stop: object,
    startup_observer: Callable[[object, int], StartupObservation],
) -> None:
    """Persist bounded observations for the exact live child generation."""
    directory = args.evidence_dir.parent / "worker-child-progress"
    path = directory / (snapshot_key(args.card, args.owner, args.claim_revision) + ".json")
    startup_complete_at = None
    provider_started_at = None
    first_output_at = None
    last_progress_at = None
    stdout_bytes = 0
    worker_pid = child.pid
    worker_start_ticks = None
    worker_cgroup = None
    while True:
        now = time.monotonic()
        observation = startup_observer(args, child.pid)
        state = classify_startup(observation)
        evidence = observation.executable_evidence or {}
        candidate_pid = evidence.get("pid", child.pid)
        try:
            candidate_pid = int(candidate_pid)
            candidate_ticks = process_start_ticks(candidate_pid)
            candidate_cgroup = process_cgroup(candidate_pid)
            worker_pid = candidate_pid
            worker_start_ticks = candidate_ticks
            worker_cgroup = candidate_cgroup
        except (OSError, TypeError, ValueError):
            pass
        if state == "startup-ready" and startup_complete_at is None:
            startup_complete_at = now
        if evidence and provider_started_at is None:
            provider_started_at = now
        try:
            current_bytes = args.stdout.stat().st_size
        except OSError:
            current_bytes = 0
        if current_bytes > stdout_bytes:
            if first_output_at is None:
                first_output_at = now
            last_progress_at = now
        stdout_bytes = max(stdout_bytes, current_bytes)
        if worker_start_ticks is not None and worker_cgroup is not None:
            snapshot = ChildProgressSnapshot(
                card_id=args.card,
                owner=args.owner,
                claim_revision=args.claim_revision,
                host=args.host,
                lane=args.lane,
                model_bucket=args.model,
                session_id=args.session,
                unit=args.unit,
                control_group=worker_cgroup,
                wrapper_pid=os.getpid(),
                child_pid=worker_pid,
                child_start_ticks=worker_start_ticks,
                started_at=args.started_monotonic,
                started_at_utc=datetime.datetime.fromtimestamp(
                    args.started_at, datetime.timezone.utc
                ).isoformat(),
                observed_at=now,
                startup_complete_at=startup_complete_at,
                provider_started_at=provider_started_at,
                first_output_at=first_output_at,
                last_progress_at=last_progress_at,
                stdout_bytes=stdout_bytes,
                child_alive=child.poll() is None,
                human_gate=args.human_gate,
                side_effects=args.side_effects,
                config=args.child_lease_config,
            )
            try:
                write_snapshot(path, snapshot)
            except (OSError, ValueError) as exc:
                sys.stderr.write(f"child progress snapshot write failed: {exc}\n")
        if stop.is_set() or child.poll() is not None:
            return
        stop.wait(args.observation_interval)
