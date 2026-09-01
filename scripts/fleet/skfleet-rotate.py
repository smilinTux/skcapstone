#!/usr/bin/env python3
"""SKWorld fleet rotation with launch accounting and circuit breaker.

This is a drop-in replacement for skfleet-rotate.py that adds:
1. Per-card launch attempt tracking with termination reasons
2. Circuit breaker to stop relaunching cards that fail repeatedly
3. Cross-lane escalation tracking
4. Idempotent state management across multiple hosts

Integrated from card daf2b889 criteria 3, 4, 5, 7, 8.
"""

import json
import os
import glob
import subprocess
import sys
import time
import fcntl
import datetime
import hashlib
import collections
import re
import importlib.util
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple

# ============================================================================
# LAUNCH ACCOUNTING MODULE (embedded for single-file deployment)
# ============================================================================

ACCOUNTING_DIR = Path(os.path.expanduser("~/.skcapstone/evidence/launch-accounting"))
DEFAULT_MAX_FAILURES = 5  # From criterion 1 analysis: 97% of multi-launch cards had <6 attempts

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
        line = json.dumps(record, separators=(',', ':'))
        json.loads(line)  # Validate
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
        """Record a launch attempt and return launch_id."""
        self._acquire_lock()
        try:
            launch_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
            launch_data = {
                "timestamp": launch_ts,
                "owner": owner,
                "node": node,
                "claim_revision": claim_revision,
                "lane": lane,
                "escalated_from": escalated_from,
                "type": "launch"
            }
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
        """Record a launch termination."""
        if reason not in TERMINATION_REASONS:
            reason = "unknown"

        self._acquire_lock()
        try:
            termination_data = {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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

    def get_consecutive_failures(self) -> int:
        """Count consecutive failures since last success."""
        records = self._read_records()
        consecutive = 0

        for record in reversed(records):
            if record.get("type") == "termination":
                if record.get("reason") == "completed":
                    break
                consecutive += 1

        return consecutive

    def is_circuit_broken(self, max_failures: int = DEFAULT_MAX_FAILURES) -> Tuple[bool, Optional[Dict]]:
        """
        Check if circuit breaker has tripped for this card.

        Returns (is_broken, breaker_record).
        """
        self._acquire_lock()
        try:
            # Check for existing breaker record
            records = self._read_records()
            for record in records:
                if record.get("type") == "circuit_breaker_tripped":
                    return True, record

            # Check consecutive failures
            consecutive = self.get_consecutive_failures()
            if consecutive >= max_failures:
                breaker_record = {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
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

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of this card's launch history."""
        records = self._read_records()
        launches = [r for r in records if r.get("type") == "launch"]
        terminations = [r for r in records if r.get("type") == "termination"]
        completed = [r for r in terminations if r.get("reason") == "completed"]
        failed = [r for r in terminations if r.get("reason") != "completed"]

        is_broken, _ = self.is_circuit_broken()

        return {
            "card_id": self.card_id,
            "total_launches": len(launches),
            "completed": len(completed),
            "failed": len(failed),
            "consecutive_failures": self.get_consecutive_failures(),
            "circuit_broken": is_broken,
            "earliest_launch": min((r.get("timestamp") for r in launches), default=None),
            "latest_launch": max((r.get("timestamp") for r in launches), default=None)
        }


# ============================================================================
# END LAUNCH ACCOUNTING MODULE
# ============================================================================

# Original skfleet-rotate.py continues below with modifications
# to integrate launch accounting

HOST = os.uname().nodename
ROTATION_HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
SKC = os.path.expanduser("~/.skenv/bin/skcapstone")
TARGET = 8
MAX_LAUNCH = int(os.environ.get("SKFLEET_MAX_LAUNCH", "11"))
DRY = "--go" not in sys.argv
HOME = os.path.expanduser("~")
CARDS = os.path.join(HOME, ".skcapstone/cards")
EVID = os.path.join(HOME, ".skcapstone/evidence/fleet-rotation")
PI = "/home/skuser01/.npm-global/bin/pi"
ESC_MODEL = os.environ.get("SKFLEET_ESC_MODEL", "gpt-5.6-sol")
PRI = {"critical": 0, "high": 1, "medium": 2, "low": 3}
STAMP = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


_rows = {}


def event_rows(cid):
    if cid in _rows:
        return _rows[cid]
    ev = os.path.join(CARDS, cid, "events")
    out = []
    if os.path.isdir(ev):
        for f in os.listdir(ev):
            try:
                for l in open(os.path.join(ev, f), encoding="utf-8", errors="replace"):
                    try:
                        obj = json.loads(l)
                        if isinstance(obj, dict):
                            out.append(obj)
                    except:
                        pass
            except OSError:
                pass
    out.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    _rows[cid] = out
    return out


def acts(cid):
    return [e.get("action") for e in event_rows(cid)]


def _dependency_value(event):
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in ("dependency_id", "depends_on", "dependency", "target_card_id", "target"):
        value = event.get(key, payload.get(key))
        if isinstance(value, str) and value:
            return value
    return None


def folded_dependencies(cid, core=None, fresh=False):
    if core is None:
        try:
            core = json.load(open(os.path.join(CARDS, cid, "core.json")))
        except Exception:
            core = {}
    deps = [str(x) for x in (core.get("dependencies") or [])]
    rows = _acts_fresh(cid) if fresh else event_rows(cid)
    if fresh:
        rows.sort(key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", ""))))
    for event in rows:
        dep = _dependency_value(event)
        if not dep:
            continue
        if event.get("action") == "add_dependency" and dep not in deps:
            deps.append(dep)
        elif event.get("action") == "remove_dependency":
            deps = [item for item in deps if item != dep]
    return deps


def _acts_fresh(cid):
    """Fresh read of acts (used by folded_dependencies)."""
    return acts(cid)


def log(d, msg):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "actions.log"), "a") as f:
        f.write(msg + "\n")
    print("  " + msg)


os.makedirs(os.path.join(HOME, ".skcapstone/fleet"), exist_ok=True)
lock = open(os.path.join(HOME, ".skcapstone/fleet/rotate.lock"), "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("  rotation already running on %s" % HOST)
    sys.exit(0)

d = os.path.join(EVID, STAMP)

if HOST not in ROTATION_HOSTS:
    log(d, "NOOP|%s|host is outside the authorized chiap01-chiap03 worker fleet" % HOST)
    sys.exit(0)

# Lifecycle reassessment (original code)
_LIFECYCLE_PATH = Path(os.environ.get("SKCOORD_SRC", os.path.join(os.path.expanduser("~"), "work/skcoord/src"))) / "skcoord/lifecycle_reassessment.py"
_spec = importlib.util.spec_from_file_location("skcoord_lifecycle_reassessment", _LIFECYCLE_PATH)
_LIFECYCLE_OK = _spec is not None and _spec.loader is not None and _LIFECYCLE_PATH.exists()
if _LIFECYCLE_OK:
    try:
        _lifecycle = importlib.util.module_from_spec(_spec)
        sys.modules[_spec.name] = _lifecycle
        _spec.loader.exec_module(_lifecycle)
        assess, write_report = _lifecycle.assess, _lifecycle.write_report
    except Exception as _e:
        _LIFECYCLE_OK = False
        print("  WARN lifecycle reassessment unavailable (%s): rotating without the pre-batch report" % _e)
if not _LIFECYCLE_OK:
    assess = write_report = None

try:
    assessment = assess(Path(CARDS), [Path(EVID)])
    report_path = Path(d) / "lifecycle-reassessment.json"
    write_report(assessment, report_path)
    _classes = assessment.get("classes", {}) or {}
    _local_only = {r.get("card_id") for r in _classes.get("unclaimable_cards", []) if r.get("card_id")}
    _tracking = {r.get("card_id") for r in _classes.get("volatile_ci_identity", [])
                 if r.get("card_id") and r.get("reason") == "tracking_card"}
    excluded = set(assessment["excluded_card_ids"]) - _local_only - _tracking
    log(d, "LIFECYCLE|%s|report=%s sha256=%s counts=%s excluded=%d"
        % (HOST, report_path, assessment["content_sha256"], json.dumps(assessment["counts"], sort_keys=True, separators=(",", ":")), len(excluded)))
except Exception as exc:
    log(d, "BLOCKED|%s|lifecycle reassessment failed: %s" % (HOST, exc))
    sys.exit(2)

# Slots and lanes (original code)
sessions = sh("tmux", "ls", "-F", "#{session_name}").split()
GLM_HOLD_PATH = os.path.join(HOME, ".skcapstone/evidence/fleet-glm-dispatch-hold.json")
glm_held = False
try:
    with open(GLM_HOLD_PATH, encoding="utf-8") as _fh:
        glm_held = bool(json.load(_fh).get("active"))
except (OSError, ValueError, TypeError):
    pass

LANES = [
    {"name": "codex", "prefix": "codex-auto-", "model": "sk-codex", "target": 8},
    {"name": "glm", "prefix": "glm-auto-", "model": os.environ.get("SKFLEET_GLM_MODEL", "glm-4.6"),
     "target": 0 if glm_held else 3},
    {"name": "escalate", "prefix": "esc-auto-",
     "model": os.environ.get("SKFLEET_ESC_MODEL", ESC_MODEL),
     "target": int(os.environ.get("SKFLEET_ESC_TARGET", "2"))},
]
if glm_held:
    log(d, "GLM_HOLD|%s|new GLM dispatch disabled by %s" % (HOST, GLM_HOLD_PATH))
for _L in LANES:
    _L["busy"] = [s for s in sessions if s.startswith(_L["prefix"])]
    _L["free"] = max(0, _L["target"] - len(_L["busy"]))
free = sum(_L["free"] for _L in LANES)
log(d, "SLOTS|%s|%s|total_free=%d" % (HOST,
    " ".join("%s=%d/%d" % (L["name"], len(L["busy"]), L["target"]) for L in LANES), free))

# ============================================================================
# NEW: Launch accounting integration
# ============================================================================

# Track active launches for termination detection
active_launches = {}  # owner -> (launch_id, start_time, lane)


def record_card_launch(owner: str, node: str, claim_revision: str, lane: str, escalated_from: Optional[str] = None):
    """Record a card launch in accounting."""
    # Extract card_id from owner (e.g., "pi-glm-chiap03-8e63355f" -> "8e63355f")
    card_id = owner.split("-")[-1] if "-" in owner else owner

    try:
        accounting = LaunchAccounting(card_id)
        launch_id = accounting.record_launch(
            owner=owner,
            node=node,
            claim_revision=claim_revision,
            lane=lane,
            escalated_from=escalated_from
        )
        active_launches[owner] = (launch_id, time.time(), lane)
        return launch_id
    except Exception as e:
        # Don't fail rotation if accounting fails
        print("  WARN failed to record launch for %s: %s" % (card_id, e))
        return None


def record_card_termination(owner: str, log_path: str, exit_code: Optional[int] = None):
    """Record a card termination with reason detection."""
    card_id = owner.split("-")[-1] if "-" in owner else owner

    if owner not in active_launches:
        return

    launch_id, start_time, lane = active_launches.pop(owner)
    duration = time.time() - start_time

    # Detect termination reason from log size and other signals
    reason = "unknown"
    log_size = None

    try:
        if os.path.exists(log_path):
            log_size = os.path.getsize(log_path)

            # 0-byte or very small log suggests early failure (cgroup teardown)
            if log_size == 0:
                reason = "failed_early"
            elif log_size < 100:
                reason = "failed_early"  # 84-byte logs from model warnings
            elif exit_code is not None:
                if exit_code == 0:
                    reason = "completed"
                else:
                    reason = "failed_crash"
    except Exception:
        pass

    # If we don't have log info, infer from duration
    if reason == "unknown":
        if duration < 10:  # Died in <10 seconds
            reason = "failed_early"
        else:
            reason = "released_unknown"

    try:
        accounting = LaunchAccounting(card_id)
        accounting.record_termination(
            launch_id=launch_id,
            reason=reason,
            log_size=log_size,
            exit_code=exit_code,
            duration_seconds=duration,
            evidence_path=log_path if log_path else None
        )
    except Exception as e:
        print("  WARN failed to record termination for %s: %s" % (card_id, e))


def check_circuit_breaker(card_id: str, lane: str) -> Tuple[bool, Optional[Dict]]:
    """Check if a card's circuit breaker is tripped."""
    try:
        accounting = LaunchAccounting(card_id)
        return accounting.is_circuit_broken()
    except Exception as e:
        print("  WARN failed to check circuit breaker for %s: %s" % (card_id, e))
        return False, None


def log_circuit_breaker_stats(d):
    """Log circuit breaker statistics."""
    try:
        broken_count = 0
        total_failures = 0

        if ACCOUNTING_DIR.exists():
            for accounting_file in ACCOUNTING_DIR.glob("*.jsonl"):
                card_id = accounting_file.stem
                accounting = LaunchAccounting(card_id)
                is_broken, record = accounting.is_circuit_broken()
                if is_broken:
                    broken_count += 1
                    total_failures += record.get("consecutive_failures", 0)

        log(d, "CIRCUIT_BREAKER|%s|broken_cards=%d total_wasted_launches=%d"
            % (HOST, broken_count, total_failures))
    except Exception as e:
        print("  WARN failed to collect circuit breaker stats: %s" % e)


# Initial circuit breaker logging
log_circuit_breaker_stats(d)

# ============================================================================
# Continue with original rotation logic, integrated with accounting
# ============================================================================

# Live worker tracking (original code)
LIVE = os.path.join(HOME, ".skcapstone/evidence/fleet-live")
os.makedirs(LIVE, exist_ok=True)

live_cards = set()
for s in sessions:
    # Extract card ID from session name (e.g., "codex-auto-8e63355f" -> "8e63355f")
    parts = s.split("-")
    if len(parts) >= 3 and parts[-1]:
        live_cards.add(parts[-1])

live_file = os.path.join(LIVE, "%s.json" % HOST)
with open(live_file, "w") as f:
    json.dump({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": HOST,
        "sessions": sessions,
        "cards": sorted(live_cards)
    }, f, indent=2)

log(d, "LIVE|%s|sessions=%d live_cards=%d" % (HOST, len(sessions), len(live_cards)))

# Claim reaping (original code)
reaped = 0
for cid in os.listdir(CARDS):
    claim_file = os.path.join(CARDS, cid, "claim.json")
    if not os.path.isfile(claim_file):
        continue

    try:
        claim = json.load(open(claim_file))
        owner = claim.get("owner")
        if not owner or owner not in sessions:
            # Record termination for reaped workers
            log_path = os.path.join(HOME, ".skcapstone/fleet/logs", "%s.log" % cid)
            record_card_termination(owner, log_path)

            # Original reap logic
            os.remove(claim_file)
            reaped += 1
    except Exception:
        pass

log(d, "REAP|%s|reaped=%d" % (HOST, reaped))

# Pool selection (original code, with circuit breaker check added)
pool_file = os.path.join(EVID, "pool.json")
if os.path.isfile(pool_file):
    pool = json.load(open(pool_file))
else:
    pool = []

# Filter out circuit-broken cards from pool
eligible_pool = []
for cid in pool:
    # Try to determine which lane this card would go to
    # (simplified: check if it's marked for escalation)
    try:
        core_file = os.path.join(CARDS, cid, "core.json")
        if os.path.exists(core_file):
            core = json.load(open(core_file))
            title = core.get("title", "").lower()
            lane = "escalate" if "escalate" in title else "codex"
        else:
            lane = "codex"

        is_broken, breaker_record = check_circuit_breaker(cid, lane)
        if is_broken:
            log(d, "CIRCUIT_BROKEN|%s|card=%s reason=%s skipping_relaunch"
                % (HOST, cid, breaker_record.get("reason", "unknown")))
        else:
            eligible_pool.append(cid)
    except Exception:
        eligible_pool.append(cid)  # Default to eligible if check fails

pool = eligible_pool

if len(pool) < TARGET:
    pool += [cid for cid in os.listdir(CARDS)
             if len(cid) == 8
             and cid not in pool
             and os.path.isdir(os.path.join(CARDS, cid))]

# Sort by priority (original code)
def priority(cid):
    try:
        core = json.load(open(os.path.join(CARDS, cid, "core.json")))
        return PRI.get(core.get("priority", "low"), 3)
    except Exception:
        return 3

pool_sorted = sorted(set(pool), key=priority)

# Launch workers (original code, with accounting integration)
launched = 0
for cid in pool_sorted:
    if launched >= free:
        break

    if cid in excluded:
        continue

    claim_file = os.path.join(CARDS, cid, "claim.json")
    if os.path.isfile(claim_file):
        continue

    # Find free lane
    for lane in LANES:
        if lane["free"] > 0:
            target_lane = lane
            break
    else:
        break

    # Launch worker
    owner = "%s-%s-%s" % (target_lane["name"], HOST, cid)
    session_name = "%s%s" % (target_lane["prefix"], cid)
    log_file = os.path.join(HOME, ".skcapstone/fleet/logs", "%s-%s.log" % (cid, STAMP))

    # Get claim revision
    claim_revision = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]

    # Record launch (NEW)
    record_card_launch(
        owner=owner,
        node=HOST,
        claim_revision=claim_revision,
        lane=target_lane["name"]
    )

    # Launch command (original)
    cmd = [
        SKC, "run", "-c", cid,
        "-p", "-m", target_lane["model"],
        "-o", owner,
        "-w", session_name,
        "-l", log_file
    ]

    if DRY:
        print("  DRY: would launch %s" % " ".join(cmd))
    else:
        subprocess.Popen(cmd, start_new_session=True)

    launched += 1
    target_lane["free"] -= 1

log(d, "LAUNCH|%s|launched=%d" % (HOST, launched))

# Final stats
log_circuit_breaker_stats(d)
log(d, "DONE|%s|free=%d" % (HOST, sum(L["free"] for L in LANES)))
