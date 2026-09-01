#!/usr/bin/env python3
"""Launch accounting and circuit breaker for SKFleet rotation.

This module provides:
1. Per-card launch attempt tracking with termination reasons
2. Circuit breaker to stop relaunching cards that fail repeatedly
3. Cross-lane escalation tracking
4. Idempotent state management across multiple hosts

Meets criteria 3, 4, 5, 7 of card daf2b889.
"""

import json
import os
import fcntl
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

HOME = Path(os.path.expanduser("~"))
ACCOUNTING_DIR = HOME / ".skcapstone" / "evidence" / "launch-accounting"
DEFAULT_MAX_FAILURES = 5  # From criterion 1: 97% of multi-launch cards had <6 attempts

# Termination reasons
TERMINATION_REASONS = {
    "completed": "Worker completed successfully",
    "failed_early": "Worker died immediately (0-byte or <100-byte log)",
    "failed_timeout": "Worker timed out",
    "failed_crash": "Worker crashed or was killed",
    "failed_dependency": "Worker blocked on dependency",
    "failed_capability": "Worker exceeded capability",
    "released_unknown": "Claim released with no completion event",
    "killed_cgroup": "Worker killed by cgroup teardown (KillMode=process)",
    "unknown": "Unknown termination reason"
}


class LaunchAccounting:
    """Thread-safe launch accounting with file locking for multi-host safety."""

    def __init__(self, card_id: str):
        self.card_id = card_id
        self.accounting_file = ACCOUNTING_DIR / f"{card_id}.jsonl"
        self.lock_file = ACCOUNTING_DIR / f"{card_id}.lock"
        ACCOUNTING_DIR.mkdir(parents=True, exist_ok=True)

    def _acquire_lock(self):
        """Acquire exclusive lock for this card's accounting file."""
        if not self.lock_file.exists():
            self.lock_file.touch()
        self.lock_fd = open(self.lock_file, 'w')
        fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX)

    def _release_lock(self):
        """Release lock."""
        if hasattr(self, 'lock_fd'):
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            self.lock_fd.close()

    def _read_records(self) -> List[Dict[str, Any]]:
        """Read all accounting records for this card."""
        records = []
        if self.accounting_file.exists():
            for line in self.accounting_file.read_text().strip().split('\n'):
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    def _append_record(self, record: Dict[str, Any]):
        """Atomically append a record (CardStore append-only pattern)."""
        # Serialize first, validate it's valid JSON
        line = json.dumps(record, separators=(',', ':'))
        # Validate by parsing
        json.loads(line)  # Will raise if invalid
        # Now append
        with open(self.accounting_file, 'a') as f:
            f.write(line + '\n')

    def record_launch(
        self,
        owner: str,
        node: str,
        claim_revision: str,
        lane: str,
        escalated_from: Optional[str] = None
    ) -> str:
        """
        Record a launch attempt.

        Returns the launch_id (hash of the attempt).
        """
        self._acquire_lock()
        try:
            launch_ts = datetime.now(timezone.utc).isoformat()
            launch_data = {
                "timestamp": launch_ts,
                "owner": owner,
                "node": node,
                "claim_revision": claim_revision,
                "lane": lane,
                "escalated_from": escalated_from,
                "type": "launch"
            }
            # Hash for reference
            launch_id = hashlib.sha256(
                json.dumps(launch_data, sort_keys=True).encode()
            ).hexdigest()[:16]
            launch_data["launch_id"] = launch_id
            self._append_record(launch_data)
            return launch_id
        finally:
            self._release_lock()

    def record_termination(
        self,
        launch_id: str,
        reason: str,
        log_size: Optional[int] = None,
        exit_code: Optional[int] = None,
        duration_seconds: Optional[float] = None,
        evidence_path: Optional[str] = None
    ):
        """
        Record a launch termination.

        Args:
            launch_id: The launch_id from record_launch()
            reason: One of TERMINATION_REASONS keys
            log_size: Size of worker log in bytes (for detecting early failures)
            exit_code: Worker exit code if available
            duration_seconds: How long the worker ran
            evidence_path: Path to worker log or other evidence
        """
        if reason not in TERMINATION_REASONS:
            reason = "unknown"

        self._acquire_lock()
        try:
            termination_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "launch_id": launch_id,
                "type": "termination",
                "reason": reason,
                "reason_description": TERMINATION_REASONS[reason],
                "log_size": log_size,
                "exit_code": exit_code,
                "duration_seconds": duration_seconds,
                "evidence_path": evidence_path
            }
            self._append_record(termination_data)
        finally:
            self._release_lock()

    def get_launch_history(self) -> List[Dict[str, Any]]:
        """Get complete launch history for this card."""
        self._acquire_lock()
        try:
            return self._read_records()
        finally:
            self._release_lock()

    def get_failure_count(self, since: Optional[str] = None) -> int:
        """
        Count failed launches.

        Args:
            since: ISO timestamp to count failures since (None = all time)
        """
        records = self.get_launch_history()
        failures = 0

        for record in records:
            if record.get("type") == "termination":
                reason = record.get("reason", "")
                # All non-completion reasons are failures
                if reason != "completed":
                    if since is None or record.get("timestamp", "") >= since:
                        failures += 1

        return failures

    def get_consecutive_failures(self) -> int:
        """Count consecutive failures since last success."""
        records = self.get_launch_history()
        consecutive = 0

        for record in reversed(records):
            if record.get("type") == "termination":
                if record.get("reason") == "completed":
                    break
                consecutive += 1

        return consecutive

    def is_circuit_broken(self, max_failures: int = DEFAULT_MAX_FAILURES) -> tuple[bool, Optional[Dict]]:
        """
        Check if circuit breaker has tripped for this card.

        Returns:
            (is_broken, breaker_record) where breaker_record is the record
            that tripped the breaker, or None if not broken.
        """
        self._acquire_lock()
        try:
            # Check if we already have a breaker record
            records = self._read_records()
            for record in records:
                if record.get("type") == "circuit_breaker_tripped":
                    return True, record

            # Check consecutive failures
            consecutive = self.get_consecutive_failures()
            if consecutive >= max_failures:
                breaker_record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "circuit_breaker_tripped",
                    "consecutive_failures": consecutive,
                    "max_failures": max_failures,
                    "action": "stop_relaunch",
                    "reason": f"Card failed {consecutive} times consecutively"
                }
                self._append_record(breaker_record)
                return True, breaker_record

            return False, None
        finally:
            self._release_lock()

    def get_cross_lane_count(self) -> int:
        """Count how many different lanes this card has been launched in."""
        records = self.get_launch_history()
        lanes = set()

        for record in records:
            if record.get("type") == "launch":
                lane = record.get("lane")
                if lane:
                    lanes.add(lane)

        return len(lanes)

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of this card's launch history."""
        records = self.get_launch_history()
        launches = [r for r in records if r.get("type") == "launch"]
        terminations = [r for r in records if r.get("type") == "termination"]
        completed = [r for r in terminations if r.get("reason") == "completed"]
        failed = [r for r in terminations if r.get("reason") != "completed"]

        return {
            "card_id": self.card_id,
            "total_launches": len(launches),
            "completed": len(completed),
            "failed": len(failed),
            "consecutive_failures": self.get_consecutive_failures(),
            "cross_lane_count": self.get_cross_lane_count(),
            "circuit_broken": self.is_circuit_broken()[0],
            "earliest_launch": min((r.get("timestamp") for r in launches), default=None),
            "latest_launch": max((r.get("timestamp") for r in launches), default=None)
        }


def check_all_circuit_breakers() -> List[Dict[str, Any]]:
    """
    Check all cards for tripped circuit breakers.

    Returns list of cards that are circuit-broken with their summaries.
    """
    broken_cards = []

    if not ACCOUNTING_DIR.exists():
        return broken_cards

    for accounting_file in ACCOUNTING_DIR.glob("*.jsonl"):
        card_id = accounting_file.stem
        accounting = LaunchAccounting(card_id)
        is_broken, breaker_record = accounting.is_circuit_broken()

        if is_broken:
            summary = accounting.get_summary()
            summary["breaker_record"] = breaker_record
            broken_cards.append(summary)

    return sorted(broken_cards, key=lambda x: x["consecutive_failures"], reverse=True)


def get_fleet_summary() -> Dict[str, Any]:
    """Get fleet-wide launch accounting summary."""
    if not ACCOUNTING_DIR.exists():
        return {"total_cards": 0, "cards_with_history": 0, "circuit_broken": 0}

    cards_with_history = 0
    total_launches = 0
    total_completed = 0
    total_failed = 0
    circuit_broken = 0

    for accounting_file in ACCOUNTING_DIR.glob("*.jsonl"):
        cards_with_history += 1
        accounting = LaunchAccounting(accounting_file.stem)
        summary = accounting.get_summary()

        total_launches += summary["total_launches"]
        total_completed += summary["completed"]
        total_failed += summary["failed"]

        if summary["circuit_broken"]:
            circuit_broken += 1

    return {
        "total_cards": len(list(ACCOUNTING_DIR.glob("*.jsonl"))),
        "cards_with_history": cards_with_history,
        "total_launches": total_launches,
        "total_completed": total_completed,
        "total_failed": total_failed,
        "circuit_broken": circuit_broken,
        "success_rate": total_completed / total_launches if total_launches > 0 else 0.0
    }


if __name__ == "__main__":
    # Simple CLI for testing
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <card_id> [command]")
        print("Commands:")
        print("  summary          - Show card summary")
        print("  history          - Show full history")
        print("  fleet-summary    - Show fleet-wide summary")
        print("  circuit-broken   - List all circuit-broken cards")
        sys.exit(1)

    card_id = sys.argv[1]
    command = sys.argv[2] if len(sys.argv) > 2 else "summary"

    if command == "summary":
        accounting = LaunchAccounting(card_id)
        print(json.dumps(accounting.get_summary(), indent=2))

    elif command == "history":
        accounting = LaunchAccounting(card_id)
        print(json.dumps(accounting.get_launch_history(), indent=2))

    elif command == "fleet-summary":
        print(json.dumps(get_fleet_summary(), indent=2))

    elif command == "circuit-broken":
        broken = check_all_circuit_breakers()
        print(json.dumps(broken, indent=2))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
