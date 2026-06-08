# skscheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn skcapstone's interval-only `TaskScheduler` into a unified, config-driven fleet job scheduler ("skscheduler") with cron schedules, python/shell/agent job types, per-node affinity, node-local state, a CLI, and a daily GTD-triage agent job on .41.

**Architecture:** Extend the existing `src/skcapstone/scheduled_tasks.py`. A synced `~/.skcapstone/config/jobs.yaml` is the single registry; new focused modules handle the job spec/config (`scheduler_jobs.py`), node-local state + locks (`scheduler_state.py`), and execution (`scheduler_runner.py`). The `TaskScheduler` tick loop gains a second pass that fires due config jobs whose node-affinity includes this host. State/logs live under `~/.skcapstone/scheduler/<hostname>/` (never synced).

**Tech Stack:** Python 3.11+, `croniter` (new dep), `pyyaml` (already used), `click` (CLI), `pytest`. Spec: `docs/superpowers/specs/2026-06-08-skscheduler-design.md`.

**Branch:** `feat/skscheduler`. Run tests with `~/.skenv/bin/python -m pytest`.

---

## Quick wins first (independent of the scheduler)

### Task 1: Fix ITIL problem-close → GTD project completion leak

A GTD project is created per problem (`itil.py:510`) but its id is never stored on the problem, and closing/resolving a problem never completes it → stale projects (e.g. `prb-1c7ae152` at 82 days). Mirror the incident-side behavior.

**Files:**
- Modify: `src/skcapstone/itil.py` (`create_problem` ~510, `update_problem` ~536)
- Test: `tests/test_itil_gtd_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_itil_gtd_lifecycle.py
from pathlib import Path
from skcapstone.itil import ITILManager
from skcapstone.mcp_tools.gtd_tools import _load_list, _load_archive


def test_resolving_problem_completes_its_gtd_project(tmp_path: Path, monkeypatch):
    # gtd_tools resolves paths from SHARED_ROOT/coordination/gtd — point it at tmp
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    mgr = ITILManager(str(tmp_path))

    prb = mgr.create_problem(title="Flaky widget", managed_by="opus")
    # The created problem must track its auto-created GTD project id
    assert prb.gtd_item_ids, "problem should store its GTD project id"
    assert any(p["id"] in prb.gtd_item_ids for p in _load_list("projects"))

    mgr.update_problem(prb.id, agent="opus", new_status="analyzing")
    mgr.update_problem(prb.id, agent="opus", new_status="resolved")

    # Project should be gone from active projects and present in archive as done
    assert not any(p["id"] in prb.gtd_item_ids for p in _load_list("projects"))
    archived = _load_archive()
    assert any(a["id"] in prb.gtd_item_ids and a["status"] == "done" for a in archived)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_itil_gtd_lifecycle.py -v`
Expected: FAIL — `prb.gtd_item_ids` is empty (project id not stored).

- [ ] **Step 3: Store the project id in `create_problem`**

In `src/skcapstone/itil.py`, change the auto-create call inside `create_problem` (currently line ~510):

```python
        # Auto-create GTD project (and track its id so we can close it later)
        project_id = self._create_gtd_project_for_problem(problem)
        if project_id:
            problem.gtd_item_ids.append(project_id)
            self._update_record(
                self.problems_dir, problem.id, problem.title, problem.model_dump()
            )

        return problem
```

- [ ] **Step 4: Complete the project on resolve in `update_problem`**

In `update_problem`, inside the `if new_status:` block (after `prb.timeline.append(...)` at ~539), add:

```python
            if new_status == ProblemStatus.RESOLVED.value:
                self._complete_gtd_items(prb.gtd_item_ids)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_itil_gtd_lifecycle.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/skcapstone/itil.py tests/test_itil_gtd_lifecycle.py
git commit -m "fix(itil): complete GTD project when problem resolves (lifecycle leak)"
```

---

## Phase 1 — Job spec, config loader, node affinity, due-check

### Task 2: Add `croniter` dependency

**Files:**
- Modify: `pyproject.toml` (`dependencies` array)

- [ ] **Step 1: Add the dependency**

Add `"croniter>=2.0"` to the `[project].dependencies` list in `pyproject.toml`.

- [ ] **Step 2: Install into the venv**

Run: `~/.skenv/bin/pip install 'croniter>=2.0' -q && ~/.skenv/bin/python -c "import croniter; print(croniter.__name__)"`
Expected: prints `croniter`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: add croniter dependency for skscheduler cron schedules"
```

### Task 3: `JobSpec` + config loader

**Files:**
- Create: `src/skcapstone/scheduler_jobs.py`
- Test: `tests/test_scheduler_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_jobs.py
from pathlib import Path
from skcapstone.scheduler_jobs import JobSpec, load_jobs


def test_load_jobs_parses_yaml(tmp_path: Path):
    cfg = tmp_path / "jobs.yaml"
    cfg.write_text(
        "jobs:\n"
        "  gtd-triage:\n"
        "    schedule: '0 6 * * *'\n"
        "    type: agent\n"
        "    nodes: ['.41']\n"
        "    agent: lumina\n"
        "    prompt: 'triage inbox'\n"
        "    timeout: 900\n"
        "  health:\n"
        "    every: 300s\n"
        "    type: python\n"
        "    nodes: all\n"
        "    callback: skcapstone.service_health:run_once\n",
        encoding="utf-8",
    )
    jobs = load_jobs(cfg)
    by_name = {j.name: j for j in jobs}
    assert by_name["gtd-triage"].schedule == "0 6 * * *"
    assert by_name["gtd-triage"].every_seconds is None
    assert by_name["gtd-triage"].type == "agent"
    assert by_name["gtd-triage"].nodes == [".41"]
    assert by_name["health"].every_seconds == 300.0
    assert by_name["health"].nodes == "all"
    assert by_name["health"].enabled is True  # default


def test_load_jobs_missing_file_returns_empty(tmp_path: Path):
    assert load_jobs(tmp_path / "nope.yaml") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -v`
Expected: FAIL — `No module named 'skcapstone.scheduler_jobs'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/scheduler_jobs.py
"""Declarative job specs for the skscheduler, loaded from jobs.yaml."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger("skcapstone.scheduler_jobs")

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: Union[str, int, float]) -> float:
    """Parse '300s' / '5m' / '1h' / 90 into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid duration: {value!r}")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]


@dataclass
class JobSpec:
    name: str
    type: str = "python"                     # python | shell | agent
    schedule: Optional[str] = None           # cron expression
    every_seconds: Optional[float] = None    # interval form
    nodes: Union[str, list[str]] = "all"     # "all" or list of host aliases
    agent: Optional[str] = None
    prompt: Optional[str] = None
    command: Optional[str] = None
    callback: Optional[str] = None           # dotted "module:fn" for python jobs
    timeout: float = 900.0
    enabled: bool = True


def load_jobs(config_path: Path) -> list[JobSpec]:
    """Load JobSpecs from jobs.yaml. Missing file -> []."""
    if not config_path.exists():
        return []
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    out: list[JobSpec] = []
    for name, raw in (data.get("jobs") or {}).items():
        raw = dict(raw or {})
        every = raw.pop("every", None)
        out.append(
            JobSpec(
                name=name,
                type=raw.get("type", "python"),
                schedule=raw.get("schedule"),
                every_seconds=_parse_duration(every) if every is not None else None,
                nodes=raw.get("nodes", "all"),
                agent=raw.get("agent"),
                prompt=raw.get("prompt"),
                command=raw.get("command"),
                callback=raw.get("callback"),
                timeout=float(raw.get("timeout", 900.0)),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduler_jobs.py tests/test_scheduler_jobs.py
git commit -m "feat(scheduler): JobSpec + jobs.yaml loader"
```

### Task 4: Node affinity resolution

**Files:**
- Modify: `src/skcapstone/scheduler_jobs.py`
- Test: `tests/test_scheduler_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_scheduler_jobs.py
from skcapstone.scheduler_jobs import job_runs_here, JobSpec


def test_job_runs_here_all():
    j = JobSpec(name="x", nodes="all")
    assert job_runs_here(j, host_aliases={"cbrd21-laptop12thgenintelcore", ".41"})


def test_job_runs_here_match_and_miss():
    j = JobSpec(name="x", nodes=[".41"])
    assert job_runs_here(j, host_aliases={".41"})
    assert not job_runs_here(j, host_aliases={".158", "noroc2027"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -k job_runs_here -v`
Expected: FAIL — `cannot import name 'job_runs_here'`

- [ ] **Step 3: Add the function**

```python
# add to src/skcapstone/scheduler_jobs.py
def job_runs_here(job: JobSpec, host_aliases: set[str]) -> bool:
    """True if this host (any of its aliases) is in the job's affinity."""
    if job.nodes == "all":
        return True
    if isinstance(job.nodes, list):
        return any(n in host_aliases for n in job.nodes)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -k job_runs_here -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduler_jobs.py tests/test_scheduler_jobs.py
git commit -m "feat(scheduler): per-job node-affinity resolution"
```

### Task 5: Due-check (cron + interval, with misfire catch-up)

**Files:**
- Modify: `src/skcapstone/scheduler_jobs.py`
- Test: `tests/test_scheduler_jobs.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_scheduler_jobs.py
from datetime import datetime, timedelta, timezone
from skcapstone.scheduler_jobs import is_due, JobSpec


def test_interval_due():
    j = JobSpec(name="x", every_seconds=300)
    now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert is_due(j, last_run=None, now=now)                       # never run
    assert not is_due(j, last_run=now - timedelta(seconds=100), now=now)
    assert is_due(j, last_run=now - timedelta(seconds=301), now=now)


def test_cron_due_at_scheduled_minute():
    j = JobSpec(name="x", schedule="0 6 * * *")  # daily 06:00
    six_am = datetime(2026, 6, 8, 6, 0, 30, tzinfo=timezone.utc)
    # never run, and we are at/after today's 06:00 slot -> due (catch-up)
    assert is_due(j, last_run=None, now=six_am)
    # already ran after today's slot -> not due again
    assert not is_due(j, last_run=six_am, now=six_am + timedelta(minutes=5))
    # ran yesterday, now past today's slot -> due
    assert is_due(j, last_run=six_am - timedelta(days=1), now=six_am)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -k is_due -v`
Expected: FAIL — `cannot import name 'is_due'`

- [ ] **Step 3: Implement due-check**

```python
# add to src/skcapstone/scheduler_jobs.py
from datetime import datetime, timezone  # ensure imported at top


def is_due(job: JobSpec, last_run: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """Return True if the job should fire now.

    Interval jobs: due when elapsed >= every_seconds (or never run).
    Cron jobs: due when the most recent scheduled slot at/just-before `now`
    is later than last_run (gives single catch-up after downtime).
    """
    now = now or datetime.now(timezone.utc)

    if job.every_seconds is not None:
        if last_run is None:
            return True
        return (now - last_run).total_seconds() >= job.every_seconds

    if job.schedule:
        from croniter import croniter

        itr = croniter(job.schedule, now)
        prev_slot = itr.get_prev(datetime)  # most recent scheduled time <= now
        if prev_slot.tzinfo is None:
            prev_slot = prev_slot.replace(tzinfo=timezone.utc)
        if last_run is None:
            return True
        return last_run < prev_slot

    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py -k is_due -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduler_jobs.py tests/test_scheduler_jobs.py
git commit -m "feat(scheduler): cron + interval due-check with misfire catch-up"
```

---

## Phase 2 — Node-local state, locks, and the job runner

### Task 6: Node-local state store

**Files:**
- Create: `src/skcapstone/scheduler_state.py`
- Test: `tests/test_scheduler_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_state.py
from datetime import datetime, timezone
from pathlib import Path
from skcapstone.scheduler_state import SchedulerState


def test_state_roundtrip(tmp_path: Path):
    st = SchedulerState(root=tmp_path, hostname="hostA")
    assert st.last_run("job1") is None
    now = datetime(2026, 6, 8, 6, 0, tzinfo=timezone.utc)
    st.record_run("job1", now=now, ok=True)
    # New instance reads persisted state
    st2 = SchedulerState(root=tmp_path, hostname="hostA")
    assert st2.last_run("job1") == now
    rec = st2.get("job1")
    assert rec["run_count"] == 1 and rec["error_count"] == 0


def test_state_path_is_host_scoped(tmp_path: Path):
    st = SchedulerState(root=tmp_path, hostname="hostA")
    assert st.state_file == tmp_path / "scheduler" / "hostA" / "state.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_state.py -v`
Expected: FAIL — `No module named 'skcapstone.scheduler_state'`

- [ ] **Step 3: Write implementation**

```python
# src/skcapstone/scheduler_state.py
"""Node-local (never-synced) state for the skscheduler."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.scheduler_state")


class SchedulerState:
    """Per-host job state at <root>/scheduler/<hostname>/state.json."""

    def __init__(self, root: Path, hostname: str) -> None:
        self.state_file = Path(root) / "scheduler" / hostname / "state.json"
        self._data: dict = {}
        if self.state_file.exists():
            try:
                self._data = json.loads(self.state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}

    def get(self, job: str) -> dict:
        return self._data.get(job, {"run_count": 0, "error_count": 0, "last_run": None})

    def last_run(self, job: str) -> Optional[datetime]:
        raw = self.get(job).get("last_run")
        return datetime.fromisoformat(raw) if raw else None

    def record_run(self, job: str, now: Optional[datetime] = None, ok: bool = True,
                   error: str = "") -> None:
        now = now or datetime.now(timezone.utc)
        rec = self.get(job)
        rec["last_run"] = now.isoformat()
        rec["last_status"] = "ok" if ok else "error"
        rec["last_error"] = "" if ok else error
        rec["run_count"] = rec.get("run_count", 0) + (1 if ok else 0)
        rec["error_count"] = rec.get("error_count", 0) + (0 if ok else 1)
        self._data[job] = rec
        self._flush()

    def all(self) -> dict:
        return dict(self._data)

    def _flush(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduler_state.py tests/test_scheduler_state.py
git commit -m "feat(scheduler): node-local (never-synced) state store"
```

### Task 7: Job runner (dispatch + overlap lock + logs)

**Files:**
- Create: `src/skcapstone/scheduler_runner.py`
- Test: `tests/test_scheduler_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_runner.py
from pathlib import Path
from skcapstone.scheduler_jobs import JobSpec
from skcapstone.scheduler_runner import JobRunner


def test_python_job_calls_callback(tmp_path: Path):
    called = {}
    import skcapstone.scheduler_runner as sr
    sr._TEST_HOOK = lambda: called.setdefault("hit", True)  # type: ignore
    job = JobSpec(name="t", type="python", callback="skcapstone.scheduler_runner:_TEST_HOOK")
    runner = JobRunner(log_dir=tmp_path)
    result = runner.run(job)
    assert result.ok and called.get("hit") is True


def test_shell_job_runs_command(tmp_path: Path):
    job = JobSpec(name="echo", type="shell", command="echo hello", timeout=10)
    result = JobRunner(log_dir=tmp_path).run(job)
    assert result.ok
    assert "hello" in result.output


def test_shell_job_nonzero_is_error(tmp_path: Path):
    job = JobSpec(name="fail", type="shell", command="exit 3", timeout=10)
    result = JobRunner(log_dir=tmp_path).run(job)
    assert not result.ok and result.exit_code == 3


def test_overlap_lock_blocks_second_run(tmp_path: Path):
    runner = JobRunner(log_dir=tmp_path)
    job = JobSpec(name="locked", type="shell", command="echo x", timeout=10)
    with runner.lock(job) as got:
        assert got
        with runner.lock(job) as second:
            assert not second
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_runner.py -v`
Expected: FAIL — `No module named 'skcapstone.scheduler_runner'`

- [ ] **Step 3: Write implementation**

```python
# src/skcapstone/scheduler_runner.py
"""Executes JobSpecs by type (python | shell | agent) with overlap locking."""
from __future__ import annotations

import contextlib
import importlib
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .scheduler_jobs import JobSpec

logger = logging.getLogger("skcapstone.scheduler_runner")


@dataclass
class JobResult:
    ok: bool
    exit_code: int = 0
    output: str = ""
    error: str = ""


class JobRunner:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)

    @contextlib.contextmanager
    def lock(self, job: JobSpec):
        """Per-job lockfile; yields False if already held (overlap guard)."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.log_dir / f"{job.name}.lock"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            yield False
            return
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield True
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()

    def run(self, job: JobSpec) -> JobResult:
        if job.type == "python":
            return self._run_python(job)
        if job.type == "shell":
            return self._run_subprocess(job, shlex.split(job.command or ""))
        if job.type == "agent":
            cmd = ["claude", "-p", job.prompt or ""]
            if job.agent:
                cmd += ["--agent", job.agent]
            return self._run_subprocess(job, cmd)
        return JobResult(ok=False, error=f"unknown job type: {job.type}")

    def _run_python(self, job: JobSpec) -> JobResult:
        try:
            mod_name, _, fn_name = (job.callback or "").partition(":")
            fn = getattr(importlib.import_module(mod_name), fn_name)
            fn()
            return JobResult(ok=True)
        except Exception as exc:  # noqa: BLE001 - jobs must never crash the loop
            logger.error("python job '%s' failed: %s", job.name, exc)
            return JobResult(ok=False, error=str(exc))

    def _run_subprocess(self, job: JobSpec, cmd: list[str]) -> JobResult:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = self.log_dir / f"{job.name}-{ts}.log"
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=job.timeout
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            log_path.write_text(out, encoding="utf-8")
            return JobResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                output=out,
                error="" if proc.returncode == 0 else out[-500:],
            )
        except subprocess.TimeoutExpired:
            return JobResult(ok=False, exit_code=-1, error=f"timeout after {job.timeout}s")
        except (OSError, ValueError) as exc:
            return JobResult(ok=False, exit_code=-1, error=str(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduler_runner.py tests/test_scheduler_runner.py
git commit -m "feat(scheduler): job runner (python/shell/agent) with overlap lock"
```

---

## Phase 3 — Wire config jobs into TaskScheduler

### Task 8: Tick-loop integration of config jobs

**Files:**
- Modify: `src/skcapstone/scheduled_tasks.py` (`TaskScheduler`)
- Test: `tests/test_scheduler_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_integration.py
import threading
from datetime import datetime, timezone
from pathlib import Path
from skcapstone.scheduler_jobs import JobSpec
from skcapstone.scheduled_tasks import TaskScheduler


def test_due_config_job_for_this_host_fires(tmp_path: Path):
    sched = TaskScheduler(home=tmp_path, stop_event=threading.Event())
    fired = []
    job = JobSpec(name="j", type="shell", command="true", every_seconds=1, nodes=["hostA"])
    sched.load_config_jobs(
        jobs=[job], hostname="hostA", host_aliases={"hostA"}, state_root=tmp_path
    )
    # patch the runner to record instead of subprocess
    sched._job_runner.run = lambda j: fired.append(j.name) or _ok()  # type: ignore
    sched.tick_config_jobs(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    assert fired == ["j"]


def test_job_not_for_this_host_skipped(tmp_path: Path):
    sched = TaskScheduler(home=tmp_path, stop_event=threading.Event())
    fired = []
    job = JobSpec(name="j", type="shell", command="true", every_seconds=1, nodes=[".41"])
    sched.load_config_jobs(jobs=[job], hostname="hostB", host_aliases={"hostB"}, state_root=tmp_path)
    sched._job_runner.run = lambda j: fired.append(j.name)  # type: ignore
    sched.tick_config_jobs()
    assert fired == []


def _ok():
    from skcapstone.scheduler_runner import JobResult
    return JobResult(ok=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -v`
Expected: FAIL — `TaskScheduler` has no `load_config_jobs`.

- [ ] **Step 3: Extend `TaskScheduler`**

Add imports near the top of `scheduled_tasks.py`:

```python
from .scheduler_jobs import JobSpec, is_due, job_runs_here
from .scheduler_runner import JobRunner
from .scheduler_state import SchedulerState
```

Add to `TaskScheduler.__init__` (after `self._thread = None`):

```python
        self._config_jobs: list[JobSpec] = []
        self._host_aliases: set[str] = set()
        self._state: Optional[SchedulerState] = None
        self._job_runner: Optional[JobRunner] = None
```

Add methods to `TaskScheduler`:

```python
    def load_config_jobs(
        self,
        jobs: list[JobSpec],
        hostname: str,
        host_aliases: set[str],
        state_root: Path,
    ) -> None:
        """Attach config-driven jobs filtered to this host's affinity."""
        self._host_aliases = host_aliases
        self._state = SchedulerState(root=state_root, hostname=hostname)
        self._job_runner = JobRunner(log_dir=state_root / "scheduler" / hostname / "logs")
        self._config_jobs = [
            j for j in jobs if j.enabled and job_runs_here(j, host_aliases)
        ]
        logger.info("Loaded %d config job(s) for host %s", len(self._config_jobs), hostname)

    def tick_config_jobs(self, now: Optional[datetime] = None) -> None:
        """Fire due config jobs once (called each scheduler tick)."""
        if not self._config_jobs or self._state is None or self._job_runner is None:
            return
        now = now or datetime.now(timezone.utc)
        for job in self._config_jobs:
            if not is_due(job, self._state.last_run(job.name), now):
                continue
            with self._job_runner.lock(job) as got:
                if not got:
                    logger.debug("job '%s' still running — skip", job.name)
                    continue
                result = self._job_runner.run(job)
                self._state.record_run(
                    job.name, now=now, ok=result.ok, error=result.error
                )
                if not result.ok:
                    logger.warning("job '%s' failed: %s", job.name, result.error)
```

Then call it from the loop — in `_run`, after the existing `task.run()` loop and before `self._stop_event.wait(...)`:

```python
            self.tick_config_jobs(now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduled_tasks.py tests/test_scheduler_integration.py
git commit -m "feat(scheduler): fire config jobs from the TaskScheduler tick loop"
```

### Task 9: Load jobs.yaml in `build_scheduler`

**Files:**
- Modify: `src/skcapstone/scheduled_tasks.py` (`build_scheduler`)
- Test: `tests/test_scheduler_integration.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_scheduler_integration.py
import socket as _socket
from skcapstone.scheduled_tasks import build_scheduler


def test_build_scheduler_loads_jobs_yaml(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "jobs.yaml").write_text(
        "jobs:\n  noop:\n    every: 60s\n    type: shell\n    command: 'true'\n    nodes: all\n",
        encoding="utf-8",
    )
    sched = build_scheduler(home=tmp_path, stop_event=threading.Event())
    assert any(j.name == "noop" for j in sched._config_jobs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -k build_scheduler -v`
Expected: FAIL — `_config_jobs` is empty (no loading yet).

- [ ] **Step 3: Implement loading + alias resolution helper**

Add a helper to `scheduler_jobs.py`:

```python
# src/skcapstone/scheduler_jobs.py
import socket


def current_host_aliases() -> set[str]:
    """Aliases identifying this host (hostname + any configured short alias)."""
    aliases = {socket.gethostname()}
    # Optional override so jobs.yaml can use friendly aliases like ".41"
    import os
    extra = os.environ.get("SK_NODE_ALIAS", "")
    aliases.update(a.strip() for a in extra.split(",") if a.strip())
    return aliases
```

In `build_scheduler` (after the built-in `scheduler.register(...)` calls, before `return scheduler`):

```python
    # Config-driven jobs (jobs.yaml) — the unified registry
    from .scheduler_jobs import load_jobs, current_host_aliases

    jobs_path = Path(home) / "config" / "jobs.yaml"
    jobs = load_jobs(jobs_path)
    if jobs:
        aliases = current_host_aliases()
        scheduler.load_config_jobs(
            jobs=jobs,
            hostname=socket.gethostname(),
            host_aliases=aliases,
            state_root=Path(home),
        )
```

Add `import socket` at the top of `scheduled_tasks.py` if not present.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -k build_scheduler -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduled_tasks.py src/skcapstone/scheduler_jobs.py tests/test_scheduler_integration.py
git commit -m "feat(scheduler): load jobs.yaml into build_scheduler with host-alias resolution"
```

---

## Phase 4 — CLI

### Task 10: `skcapstone scheduler` command group

**Files:**
- Create: `src/skcapstone/cli/scheduler_cmd.py`
- Modify: `src/skcapstone/cli/__init__.py`
- Test: `tests/test_scheduler_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_cli.py
import click
from click.testing import CliRunner
from skcapstone.cli.scheduler_cmd import register_scheduler_commands


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "jobs.yaml").write_text(
        "jobs:\n  demo:\n    every: 60s\n    type: shell\n    command: 'echo hi'\n    nodes: all\n",
        encoding="utf-8",
    )

    @click.group()
    def main():
        pass

    register_scheduler_commands(main)
    return main


def test_scheduler_list(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["scheduler", "list"])
    assert res.exit_code == 0 and "demo" in res.output


def test_scheduler_run_now(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["scheduler", "run", "demo"])
    assert res.exit_code == 0 and "hi" in res.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_cli.py -v`
Expected: FAIL — `No module named 'skcapstone.cli.scheduler_cmd'`

- [ ] **Step 3: Write the CLI module**

```python
# src/skcapstone/cli/scheduler_cmd.py
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


def _jobs_path() -> Path:
    return Path(os.environ.get("SKCAPSTONE_HOME", AGENT_HOME)) / "config" / "jobs.yaml"


def _state_root() -> Path:
    return Path(os.environ.get("SKCAPSTONE_HOME", AGENT_HOME))


def register_scheduler_commands(main: click.Group) -> None:
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
            sched = j.schedule or (f"every {int(j.every_seconds)}s" if j.every_seconds else "—")
            mark = "✓" if (j.enabled and job_runs_here(j, here)) else " "
            click.echo(f"[{mark}] {j.name:24s} {j.type:6s} {sched:18s} nodes={j.nodes}")

    @scheduler.command("status")
    @click.option("--json", "as_json", is_flag=True)
    def status(as_json: bool) -> None:
        """Show last-run status for this node."""
        st = SchedulerState(root=_state_root(), hostname=socket.gethostname())
        data = st.all()
        if as_json:
            click.echo(json.dumps(data, indent=2))
            return
        if not data:
            click.echo("No run history on this node yet.")
            return
        for name, rec in data.items():
            click.echo(f"{name:24s} last={rec.get('last_run')} "
                       f"status={rec.get('last_status')} runs={rec.get('run_count')} "
                       f"errors={rec.get('error_count')}")

    @scheduler.command("run")
    @click.argument("job_name")
    def run_now(job_name: str) -> None:
        """Run a job now (respects node affinity)."""
        jobs = {j.name: j for j in load_jobs(_jobs_path())}
        job = jobs.get(job_name)
        if not job:
            raise click.ClickException(f"Unknown job: {job_name}")
        runner = JobRunner(log_dir=_state_root() / "scheduler" / socket.gethostname() / "logs")
        result = runner.run(job)
        if result.output:
            click.echo(result.output.strip())
        if not result.ok:
            raise click.ClickException(f"Job failed: {result.error}")
        click.echo(f"✓ {job_name} done")

    @scheduler.command("logs")
    @click.argument("job_name")
    @click.option("--tail", default=40, show_default=True)
    def logs(job_name: str, tail: int) -> None:
        """Show the latest log for a job on this node."""
        log_dir = _state_root() / "scheduler" / socket.gethostname() / "logs"
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
        click.echo(f"✓ enabled {job_name}")

    @scheduler.command("disable")
    @click.argument("job_name")
    def disable(job_name: str) -> None:
        """Disable a job (sets enabled: false in jobs.yaml)."""
        _set_enabled(job_name, False)
        click.echo(f"✓ disabled {job_name}")


def _set_enabled(job_name: str, value: bool) -> None:
    import yaml

    path = _jobs_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = data.get("jobs") or {}
    if job_name not in jobs:
        raise click.ClickException(f"Unknown job: {job_name}")
    jobs[job_name]["enabled"] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
```

- [ ] **Step 4: Wire into the CLI**

In `src/skcapstone/cli/__init__.py`, add an import alongside the others (after line ~61):

```python
from .scheduler_cmd import register_scheduler_commands
```

and call it where the other `register_*_commands(main)` calls live (search for `register_daemon_commands(main)` and add below it):

```python
register_scheduler_commands(main)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_cli.py -v`
Expected: PASS

- [ ] **Step 6: Verify the command is wired**

Run: `~/.skenv/bin/skcapstone scheduler list`
Expected: prints configured jobs (or "No jobs configured.")

- [ ] **Step 7: Commit**

```bash
git add src/skcapstone/cli/scheduler_cmd.py src/skcapstone/cli/__init__.py tests/test_scheduler_cli.py
git commit -m "feat(scheduler): skcapstone scheduler CLI (list/status/run/logs/enable/disable)"
```

---

## Phase 5 — Rollout on .41

### Task 11: `.stignore` for node-local scheduler state

**Files:**
- Modify: `~/.skcapstone/.stignore` (live config on .41 — not the repo)

- [ ] **Step 1: Add the ignore rule**

Ensure `~/.skcapstone/.stignore` contains (append if missing):

```
// skscheduler node-local state/logs must never sync (avoids the very conflicts it prevents)
scheduler
```

- [ ] **Step 2: Verify syncthing picks it up**

Run: `grep -n "^scheduler" ~/.skcapstone/.stignore`
Expected: prints the `scheduler` line.

- [ ] **Step 3: (no commit — live config, not repo)**

### Task 12: Author `jobs.yaml` with the GTD-triage job (on .41)

**Files:**
- Create: `~/.skcapstone/config/jobs.yaml` (live, synced)
- Create (repo): `docs/superpowers/examples/jobs.yaml` (committed reference copy)

- [ ] **Step 1: Write the reference example into the repo**

```yaml
# docs/superpowers/examples/jobs.yaml — reference for ~/.skcapstone/config/jobs.yaml
jobs:
  gtd-inbox-triage:
    schedule: "0 6 * * *"      # daily 06:00
    type: agent
    nodes: [".41"]
    agent: lumina
    prompt: >
      Triage the GTD inbox: for each item, clarify into next-action / project /
      someday-maybe, or archive noise; move resolved-ITIL items to done; surface
      stale projects. Use the gtd_* and itil_* MCP tools. Keep it concise.
    timeout: 900
    enabled: true
```

- [ ] **Step 2: Install it as the live config on .41**

Run:
```bash
mkdir -p ~/.skcapstone/config
cp docs/superpowers/examples/jobs.yaml ~/.skcapstone/config/jobs.yaml
SK_NODE_ALIAS=.41 ~/.skenv/bin/skcapstone scheduler list
```
Expected: `gtd-inbox-triage` listed with a `✓` (runs here, because alias `.41`).

- [ ] **Step 3: Smoke-test a manual run is wired (will invoke claude -p)**

Run: `~/.skenv/bin/skcapstone scheduler list`
Expected: job present. (Do NOT auto-run the agent in CI; manual `scheduler run gtd-inbox-triage` is a human-initiated check.)

- [ ] **Step 4: Commit the repo reference**

```bash
git add docs/superpowers/examples/jobs.yaml
git commit -m "docs(scheduler): reference jobs.yaml with daily gtd-inbox-triage job"
```

### Task 13: Activate the skcapstone daemon (systemd user) on .41

**Files:**
- Live: `~/.config/systemd/user/skcapstone.service` (from repo `systemd/skcapstone.service`)

- [ ] **Step 1: Confirm the daemon runs the scheduler**

Run: `grep -n "build_scheduler\|self._scheduler.start" src/skcapstone/daemon.py`
Expected: shows the daemon builds + starts the scheduler (it does — lines ~981, ~798).

- [ ] **Step 2: Set the node alias for the user service**

Add `Environment=SK_NODE_ALIAS=.41` to `~/.config/systemd/user/skcapstone.service` under `[Service]` (so `nodes: ['.41']` matches). Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now skcapstone.service
systemctl --user status skcapstone.service --no-pager | head -15
```
Expected: `active (running)`.

- [ ] **Step 3: Verify the scheduler loaded the config job**

Run: `journalctl --user -u skcapstone.service --no-pager | grep -i "config job" | tail`
Expected: a line like `Loaded 1 config job(s) for host cbrd21-laptop12thgenintelcore`.

- [ ] **Step 4: (no commit — live system change)**

---

## Phase 6 — Migration (after confirmation)

### Task 14: Migrate legacy crontab + dead timer (confirm each first)

**Files:**
- Live: user `crontab`, `~/.config/systemd/user/`
- Live: `~/.skcapstone/config/jobs.yaml`

- [ ] **Step 1: List what would migrate**

Run:
```bash
crontab -l
systemctl --user list-timers --all --no-pager
```
For EACH legacy crontab entry, confirm with Chef whether it is still wanted (the `~/dkloud.douno.it/...` path predates skcapstone and may be dead). Do not migrate blindly.

- [ ] **Step 2: Add confirmed jobs to jobs.yaml as `shell` type**

For each kept job, add an entry (example shape):

```yaml
  memory-eod-rollup:
    schedule: "55 23 * * *"
    type: shell
    nodes: [".41"]
    command: "/home/cbrd21/dkloud.douno.it/p/gentistrust/skstack01/docs/memory/memory/scripts/memory-eod-rollup.sh"
    enabled: true
```

- [ ] **Step 3: Remove migrated entries from crontab; retire dead `skcomm-heartbeat`**

```bash
crontab -l | grep -v 'memory-eod-rollup.sh' | crontab -   # repeat per migrated line
systemctl --user disable skcomm-heartbeat.timer 2>/dev/null || true
```

- [ ] **Step 4: Verify**

Run: `~/.skenv/bin/skcapstone scheduler list`
Expected: migrated jobs appear; `scheduler status` shows them running over time.

- [ ] **Step 5: (no commit — live config)**

---

## Final verification

- [ ] Run the full new test set:

```bash
~/.skenv/bin/python -m pytest tests/test_scheduler_jobs.py tests/test_scheduler_state.py \
  tests/test_scheduler_runner.py tests/test_scheduler_integration.py \
  tests/test_scheduler_cli.py tests/test_itil_gtd_lifecycle.py -v
```
Expected: all PASS.

- [ ] Confirm no regressions in existing scheduler/itil tests:

```bash
~/.skenv/bin/python -m pytest tests/ -k "scheduled or itil or daemon" -q
```

- [ ] Push branch and open PR:

```bash
git push -u origin feat/skscheduler
gh pr create --base main --title "skscheduler: unified fleet job scheduler" --body "Implements docs/superpowers/specs/2026-06-08-skscheduler-design.md. Refs prb-7810b08e, inc-455b1a64."
```

---

## Self-review notes (coverage vs spec)

- Config registry (jobs.yaml) → Tasks 3, 9, 12. ✓
- cron + interval schedules (croniter) → Tasks 2, 5. ✓
- three job types (python/shell/agent, agent via `claude -p`) → Task 7. ✓
- per-node affinity → Tasks 4, 8, 9. ✓
- node-local non-synced state + .stignore → Tasks 6, 11. ✓
- overlap-guard lockfiles → Task 7. ✓
- misfire catch-up → Task 5. ✓
- CLI list/status/run/enable/disable/logs → Task 10. ✓
- daemon activation on .41 → Task 13. ✓
- gtd-inbox-triage job → Task 12. ✓
- migration of crontab/timers → Task 14. ✓
- ITIL problem→project lifecycle fix (quick win) → Task 1. ✓
- `service_health` multi-write fix (prb-7810b08e): **out of scope here** — tracked separately; this plan only makes affinity available for it.
