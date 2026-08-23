# Dreaming → skscheduler Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the `dreaming_reflection` built-in `ScheduledTask` (hard-coded 900s interval in `scheduled_tasks.py`) onto the declarative `jobs.yaml` path (skscheduler), so it gains `SchedulerState` run-history, `skcapstone scheduler status/run/logs` observability, retries/jitter/notify, and a config-driven (not code-driven) schedule — without changing its runtime behavior or breaking `--no-consciousness` operation.

**Architecture:** skscheduler is *already* merged into `TaskScheduler` (`_run()` ticks built-ins inline, then calls `tick_config_jobs()` which dispatches `jobs.yaml` entries via `scheduler_jobs.py` → `scheduler_runner.py` → `scheduler_state.py`). This is a **registration migration**, not new infra. A `python`-type job's `callback: "module:fn"` is a **zero-argument** callable (`JobRunner._run_python` does `fn()` with no args), and it runs **in-process**, on a short-lived `threading.Thread` spawned by the daemon's own tick loop — `importlib.import_module` returns the *same* already-loaded module object, so module-level state set earlier in the same process (e.g. by `daemon.py` at startup) is visible to the job when it fires later. That in-process fact is what lets the new entrypoint reach the live `consciousness_loop` without a closure, and it's why `is_idle()`'s mood.json fallback (used whenever `consciousness_loop is None`, which is always true under `--no-consciousness`) continues to work unchanged.

**Tech Stack:** Python 3.11+, pytest, pydantic (`DreamingConfig`), PyYAML (`jobs.yaml`), click (`skcapstone scheduler` CLI).

## Global Constraints

- Dreaming must keep working when `skcapstone@lumina` (or any agent's daemon) runs with `--no-consciousness` — the consciousness loop is off but the scheduler tick loop still runs. `DreamingEngine.is_idle()` already falls back to `agents/<agent>/mood.json` when `consciousness_loop is None`; this migration must not remove or bypass that fallback, and must not require a non-None `consciousness_loop` to function.
- Must not reintroduce the chat-inbox race: never run the dream cycle synchronously on a thread that also drains the chat inbox, and never make `is_idle()` return a false positive because the live `consciousness_loop` reference silently stopped propagating to the job.
- `python`-type `jobs.yaml` callbacks are zero-argument (`job.callback = "module:fn"`, invoked as `fn()`) — no per-call arguments are possible. Any state a job needs beyond what it can import/derive itself must come from a module-level reference set once, in-process, at daemon startup.
- Scheduler run-state (`~/.skcapstone/scheduler/<host>/state.json`, logs) is node-local and must stay outside anything Syncthing-synced — do not write dreaming job run-history anywhere else.
- Python 3.11+, PEP 8, type hints, Google-style docstrings (per `skcapstone/CLAUDE.md`). Tests: pytest, `~/.skenv/bin/python -m pytest`.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` (per `~/.claude/CLAUDE.md` Conventions).
- All commands below assume `cd /home/cbrd21/clawd/skcapstone-repos/skcapstone` unless stated otherwise (repo root, not a git repo per environment info — no branch/PR steps; commits are local history only, adjust if a git remote exists at execution time).

## Key Decisions (locked)

1. **Entrypoint:** new module `src/skcapstone/dreaming_job.py`, function `run_dreaming_job()` — zero-arg, registered in `jobs.yaml` as `callback: "skcapstone.dreaming_job:run_dreaming_job"`.
2. **consciousness_loop access:** a module-level mutable reference cell in `dreaming_job.py` (`set_consciousness_loop()` / `get_consciousness_loop()`), populated once by `daemon.py::_load_components()` right after `self._consciousness` is finalized (works because `_run_python` imports and calls the *same* in-process module — confirmed by reading `scheduler_runner.py::_run_python`, which never subprocesses `python`-type jobs). No IPC, no re-derivation per run.
3. **Built-in registration:** **REMOVE** (not flag) — delete the `dreaming_reflection` `scheduler.register(...)` call and the now-dead `make_dreaming_task()` factory from `scheduled_tasks.py`. The on/off switch going forward is the job's own `enabled:` field in `jobs.yaml` (the mechanism the skscheduler design doc already specifies for every other job), avoiding a second permanent flag and matching the "one declarative registry" goal in `docs/skscheduler.md`.
4. **Execution model:** in-process (confirmed, not subprocess) — `python`-type jobs never get a per-run log file (`_run_python` doesn't write to `log_dir`; only `_run_subprocess` does). `skcapstone scheduler logs dreaming-reflection` will report "No logs" — this is a pre-existing skscheduler gap for *all* `python`-type jobs, not something this migration can silently paper over. Task 7 documents it; `skcapstone scheduler status` (backed by `SchedulerState`, which *does* work for `python` jobs) remains the source of truth for run history.

---

### Task 1: `dreaming_job` module — consciousness_loop reference cell

**Files:**
- Create: `src/skcapstone/dreaming_job.py`
- Test: `tests/test_dreaming_job.py`

**Interfaces:**
- Produces: `dreaming_job.set_consciousness_loop(loop: object | None) -> None`, `dreaming_job.get_consciousness_loop() -> object | None`. Task 2 and Task 3 both depend on these exact names.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for skcapstone.dreaming_job — the jobs.yaml entrypoint for dreaming."""
from skcapstone import dreaming_job


def test_get_consciousness_loop_defaults_to_none():
    dreaming_job.set_consciousness_loop(None)
    assert dreaming_job.get_consciousness_loop() is None


def test_set_and_get_consciousness_loop_round_trips():
    sentinel = object()
    dreaming_job.set_consciousness_loop(sentinel)
    try:
        assert dreaming_job.get_consciousness_loop() is sentinel
    finally:
        dreaming_job.set_consciousness_loop(None)  # reset shared module state
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_dreaming_job.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'skcapstone.dreaming_job'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Entrypoint module for the ``dreaming-reflection`` skscheduler job.

The `python`-type job in ``jobs.yaml`` (``callback: skcapstone.dreaming_job:run_dreaming_job``)
is a zero-argument callable (see ``scheduler_runner.py::JobRunner._run_python``). Because
that job runs in-process (same Python process as the daemon, on a short-lived worker
thread — never a subprocess), a module-level reference cell is enough to hand the job
the live ``consciousness_loop`` instance without changing the callback signature.

``daemon.py`` calls :func:`set_consciousness_loop` once, right after the consciousness
loop is loaded (or confirmed absent under ``--no-consciousness``), during
``_load_components()``. :func:`run_dreaming_job` (added in a follow-up commit) reads it
back via :func:`get_consciousness_loop`.
"""
from __future__ import annotations

_consciousness_loop: object | None = None


def set_consciousness_loop(loop: object | None) -> None:
    """Register the in-process consciousness_loop reference for the dreaming job.

    Args:
        loop: The active ``ConsciousnessLoop`` instance, or ``None`` when
            consciousness is disabled (e.g. ``--no-consciousness``).
    """
    global _consciousness_loop
    _consciousness_loop = loop


def get_consciousness_loop() -> object | None:
    """Return whatever consciousness_loop reference was last registered.

    Returns:
        The ``ConsciousnessLoop`` instance passed to the most recent
        :func:`set_consciousness_loop` call, or ``None`` if never set or
        explicitly cleared.
    """
    return _consciousness_loop
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_dreaming_job.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/dreaming_job.py tests/test_dreaming_job.py
git commit -m "$(cat <<'EOF'
feat(scheduler): add dreaming_job module with consciousness_loop reference cell

First piece of the dreaming -> jobs.yaml migration: a module-level ref
cell so the in-process python-type job can reach the live
consciousness_loop without changing the zero-arg callback signature.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `run_dreaming_job()` entrypoint

**Files:**
- Modify: `src/skcapstone/dreaming_job.py`
- Test: `tests/test_dreaming_job.py`

**Interfaces:**
- Consumes: `dreaming_job.get_consciousness_loop()` (Task 1); `skcapstone.shared_home() -> Path`; `skcapstone.consciousness_config.load_dreaming_config(home: Path) -> DreamingConfig`; `skcapstone.dreaming.DreamingEngine(home, config, consciousness_loop).dream() -> Optional[DreamResult]`; `DreamResult.memories_created: list[str]`, `DreamResult.skipped_reason: Optional[str]` (all as defined in `src/skcapstone/dreaming.py`).
- Produces: `dreaming_job.run_dreaming_job() -> None`, the exact string registered as `jobs.yaml`'s `callback:` in Task 5.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dreaming_job.py`:

```python
def test_run_dreaming_job_skips_when_disabled(monkeypatch):
    from skcapstone.dreaming import DreamingConfig

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=False)
    )
    built = []
    monkeypatch.setattr(dreaming_job, "DreamingEngine", lambda **kw: built.append(kw))
    dreaming_job.run_dreaming_job()
    assert built == []


def test_run_dreaming_job_passes_registered_consciousness_loop(monkeypatch):
    from skcapstone.dreaming import DreamingConfig, DreamResult

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=True)
    )
    dreaming_job.set_consciousness_loop("fake-loop")
    captured = {}

    class FakeEngine:
        def __init__(self, **kw):
            captured.update(kw)

        def dream(self):
            return DreamResult(memories_created=["mem-1"])

    monkeypatch.setattr(dreaming_job, "DreamingEngine", FakeEngine)
    try:
        dreaming_job.run_dreaming_job()
    finally:
        dreaming_job.set_consciousness_loop(None)
    assert captured["consciousness_loop"] == "fake-loop"


def test_run_dreaming_job_works_with_no_consciousness_loop(monkeypatch):
    """Critical constraint: must not require a non-None consciousness_loop."""
    from skcapstone.dreaming import DreamingConfig, DreamResult

    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=True)
    )
    dreaming_job.set_consciousness_loop(None)
    captured = {}

    class FakeEngine:
        def __init__(self, **kw):
            captured.update(kw)

        def dream(self):
            return DreamResult(skipped_reason="agent not idle")

    monkeypatch.setattr(dreaming_job, "DreamingEngine", FakeEngine)
    dreaming_job.run_dreaming_job()
    assert captured["consciousness_loop"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_dreaming_job.py -v`
Expected: FAIL with `AttributeError: module 'skcapstone.dreaming_job' has no attribute 'load_dreaming_config'` (or `'run_dreaming_job'`)

- [ ] **Step 3: Write minimal implementation**

Add to the top of `src/skcapstone/dreaming_job.py` (after the module docstring, before `_consciousness_loop`) and at the bottom:

```python
import logging

from . import shared_home
from .consciousness_config import load_dreaming_config
from .dreaming import DreamingEngine

logger = logging.getLogger("skcapstone.dreaming_job")
```

```python
def run_dreaming_job() -> None:
    """Zero-arg entrypoint for the ``dreaming-reflection`` jobs.yaml job.

    Resolves the shared home, loads the dreaming config from
    ``consciousness.yaml``, and — if enabled — runs one DreamingEngine cycle
    using whatever consciousness_loop was registered via
    :func:`set_consciousness_loop`. ``None`` (the `--no-consciousness` case)
    is a fully supported value: ``DreamingEngine.is_idle()`` falls back to
    ``mood.json`` when its ``consciousness_loop`` is ``None``.
    """
    home = shared_home()
    config = load_dreaming_config(home)
    if config is None or not config.enabled:
        logger.debug("Dreaming job: disabled via config — skipping")
        return
    engine = DreamingEngine(
        home=home, config=config, consciousness_loop=get_consciousness_loop()
    )
    result = engine.dream()
    if result and result.memories_created:
        logger.info(
            "Dreaming: %d memories created from reflection",
            len(result.memories_created),
        )
    elif result and result.skipped_reason:
        logger.debug("Dreaming skipped: %s", result.skipped_reason)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_dreaming_job.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/dreaming_job.py tests/test_dreaming_job.py
git commit -m "$(cat <<'EOF'
feat(scheduler): add run_dreaming_job() zero-arg jobs.yaml entrypoint

Mirrors scheduled_tasks.make_dreaming_task's gating/logging but resolves
home + consciousness_loop itself, since a python-type jobs.yaml callback
takes no arguments. Explicitly tested with consciousness_loop=None to
lock in the --no-consciousness support requirement.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wire `daemon.py` to register the live consciousness_loop

**Files:**
- Modify: `src/skcapstone/daemon.py:1143-1153` (the "Build task scheduler" block in `_load_components`)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `dreaming_job.set_consciousness_loop` (Task 1); `DaemonService._consciousness` (existing attribute, `None` when `config.consciousness_enabled=False` or the loop fails to load).
- Produces: guarantee that after `_load_components()` runs, `dreaming_job.get_consciousness_loop()` reflects `DaemonService._consciousness` for that call — depended on by Task 6/7's end-to-end checks.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_daemon.py` (near `test_load_components_initializes_beacon`, same `TestDaemonService`-style class — match its imports/fixtures):

```python
def test_load_components_wires_none_consciousness_loop_when_disabled(self, daemon_home):
    """Critical constraint: --no-consciousness must still register (a None) loop ref."""
    import sys
    from skcapstone import dreaming_job

    config = DaemonConfig(home=daemon_home, port=0, consciousness_enabled=False)
    svc = DaemonService(config)

    mock_runtime = MagicMock()
    mock_runtime.manifest.name = "test-agent"
    mock_runtime.is_initialized = True
    mock_runtime_mod = MagicMock()
    mock_runtime_mod.get_runtime.return_value = mock_runtime

    patched = {
        "skcomms": MagicMock(),
        "skcomms.core": MagicMock(),
        "skcapstone.runtime": mock_runtime_mod,
        "skcapstone.heartbeat": MagicMock(),
        "skcapstone.consciousness_config": MagicMock(),
        "skcapstone.consciousness_loop": MagicMock(),
        "skcapstone.self_healing": MagicMock(),
    }
    dreaming_job.set_consciousness_loop("stale-from-a-previous-agent")
    try:
        with patch.dict(sys.modules, patched):
            svc._load_components()
        assert svc._consciousness is None
        assert dreaming_job.get_consciousness_loop() is None
    finally:
        dreaming_job.set_consciousness_loop(None)


def test_load_components_wires_active_consciousness_loop_when_enabled(self, daemon_home):
    import sys
    from skcapstone import dreaming_job

    config = DaemonConfig(home=daemon_home, port=0, consciousness_enabled=True)
    svc = DaemonService(config)

    mock_runtime = MagicMock()
    mock_runtime.manifest.name = "test-agent"
    mock_runtime.is_initialized = True
    mock_runtime_mod = MagicMock()
    mock_runtime_mod.get_runtime.return_value = mock_runtime

    mock_loop_instance = MagicMock()
    mock_loop_cls = MagicMock(return_value=mock_loop_instance)
    mock_loop_instance._config.enabled = True

    mock_cfg_mod = MagicMock()
    mock_cfg_mod.load_consciousness_config.return_value = MagicMock(enabled=True)
    mock_loop_mod = MagicMock()
    mock_loop_mod.ConsciousnessLoop = mock_loop_cls

    patched = {
        "skcomms": MagicMock(),
        "skcomms.core": MagicMock(),
        "skcapstone.runtime": mock_runtime_mod,
        "skcapstone.heartbeat": MagicMock(),
        "skcapstone.consciousness_config": mock_cfg_mod,
        "skcapstone.consciousness_loop": mock_loop_mod,
        "skcapstone.self_healing": MagicMock(),
    }
    try:
        with patch.dict(sys.modules, patched):
            svc._load_components()
        assert svc._consciousness is mock_loop_instance
        assert dreaming_job.get_consciousness_loop() is mock_loop_instance
    finally:
        dreaming_job.set_consciousness_loop(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_daemon.py -k "wires" -v`
Expected: FAIL — `assert dreaming_job.get_consciousness_loop() is None` fails on the first test (still `"stale-from-a-previous-agent"`, proving the wiring doesn't exist yet).

- [ ] **Step 3: Write minimal implementation**

In `src/skcapstone/daemon.py`, inside the existing "Build task scheduler" `try:` block (currently lines 1137-1153), add the `set_consciousness_loop` call right after `self._scheduler = build_scheduler(...)`:

```python
        # Build task scheduler (beacon + consciousness must be ready first)
        try:
            from .scheduled_tasks import build_scheduler
            from .dreaming_job import set_consciousness_loop

            # Get sync_watcher from consciousness loop if available
            _sync_watcher = getattr(self._consciousness, "_sync_watcher", None)
            self._scheduler = build_scheduler(
                home=self.config.home,
                stop_event=self._stop_event,
                consciousness_loop=self._consciousness,
                beacon=self._beacon,
                sync_watcher=_sync_watcher,
            )
            # Give the (in-process) dreaming-reflection jobs.yaml job the same
            # consciousness_loop reference the built-in task used to receive
            # directly — None under --no-consciousness, an active loop otherwise.
            set_consciousness_loop(self._consciousness)
            logger.info("Task scheduler built — %d task(s)", len(self._scheduler._tasks))
        except Exception as exc:
            logger.warning("Task scheduler failed to build: %s", exc)
            self.state.record_error(f"Scheduler build: {exc}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_daemon.py -k "wires" -v`
Expected: PASS (2 passed)

Then run the full daemon + dreaming_job suites to check for regressions:

Run: `~/.skenv/bin/python -m pytest tests/test_daemon.py tests/test_dreaming_job.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/daemon.py tests/test_daemon.py
git commit -m "$(cat <<'EOF'
feat(daemon): wire consciousness_loop into dreaming_job at startup

_load_components() now calls dreaming_job.set_consciousness_loop()
right after build_scheduler(), so the in-process dreaming-reflection
jobs.yaml job (Task 2) sees the same consciousness_loop reference the
built-in scheduled task used to receive directly - None under
--no-consciousness, the live loop otherwise.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Remove the built-in `dreaming_reflection` registration

**Files:**
- Modify: `src/skcapstone/scheduled_tasks.py:1-18` (module docstring), `:564-596` (delete `make_dreaming_task`), `:657-676` (docstring table), `:732-737` (delete registration)
- Test: `tests/test_scheduled_tasks.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_scheduler(...)` no longer registers a task named `"dreaming_reflection"`; `make_dreaming_task` no longer exists in `scheduled_tasks.py` (confirmed unused elsewhere via `grep -rn "make_dreaming_task" src/ tests/` returning only its own definition before this task).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scheduled_tasks.py`, in `class TestBuildScheduler`:

```python
    def test_dreaming_reflection_is_not_a_builtin_task(self, tmp_path):
        """dreaming moved to jobs.yaml (2026-07-09) - must not double-fire as a builtin."""
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        names = {s["name"] for s in scheduler.status()}
        assert "dreaming_reflection" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduled_tasks.py -k dreaming_reflection -v`
Expected: FAIL — `AssertionError: assert 'dreaming_reflection' not in {...}`

- [ ] **Step 3: Remove the built-in registration and dead factory**

In `src/skcapstone/scheduled_tasks.py`, delete the registration block (currently lines 732-737):

```python
    # Dreaming — idle-time self-reflection via NVIDIA NIM
    scheduler.register(
        name="dreaming_reflection",
        interval_seconds=900,  # 15 minutes
        callback=make_dreaming_task(home, consciousness_loop),
    )

```

Delete the entire `make_dreaming_task` factory function (currently lines 564-596, from `def make_dreaming_task(` through its closing `return _run`).

Update the module docstring's built-in task list (currently lines 7-12) to drop the dreaming line:

```python
Built-in recurring tasks:
    - heartbeat_pulse        — every 30 seconds
    - backend_reprobe        — every 5 minutes
    - memory_promotion_sweep — every hour
    - profile_freshness_check — every 24 hours

Dreaming moved to a jobs.yaml config job (dreaming-reflection) on 2026-07-09 —
see docs/superpowers/plans/2026-07-09-dreaming-skscheduler-migration.md.
```

Update the `build_scheduler` docstring table (currently lines 657-676) to drop the `dreaming_reflection` row:

```python
    +--------------------------+------------+
    | Task                     | Interval   |
    +==========================+============+
    | heartbeat_pulse          | 30 s       |
    +--------------------------+------------+
    | sync_inbox_scan          | 30 s       |
    +--------------------------+------------+
    | backend_reprobe          | 5 min      |
    +--------------------------+------------+
    | service_health_check     | 5 min      |
    +--------------------------+------------+
    | memory_promotion_sweep   | 1 hour     |
    +--------------------------+------------+
    | profile_freshness_check  | 24 hours   |
    +--------------------------+------------+
```

- [ ] **Step 4: Run tests to verify pass + no regressions**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduled_tasks.py -v`
Expected: all PASS, including the new `test_dreaming_reflection_is_not_a_builtin_task`

Run a repo-wide grep to confirm nothing else references the deleted factory:

Run: `grep -rn "make_dreaming_task" src/ tests/`
Expected: no output (empty)

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/scheduled_tasks.py tests/test_scheduled_tasks.py
git commit -m "$(cat <<'EOF'
refactor(scheduler): remove built-in dreaming_reflection registration

Superseded by the dreaming-reflection jobs.yaml job (Task 5). Deletes
the now-dead make_dreaming_task() factory and updates docstrings.
Prevents double-firing once jobs.yaml is registered.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Register the `dreaming-reflection` job in `jobs.yaml`

**Files:**
- Modify: `~/.skcapstone/config/jobs.yaml` (operator config, not part of the git repo — back it up first)

**Interfaces:**
- Consumes: `skcapstone.dreaming_job:run_dreaming_job` (Task 2).
- Produces: a `JobSpec` named `dreaming-reflection` that `load_jobs_with_dropins()` (already called by `build_scheduler()`) will pick up on the next daemon restart.

- [ ] **Step 1: Back up the current file**

Run: `cp ~/.skcapstone/config/jobs.yaml ~/.skcapstone/config/jobs.yaml.bak-2026-07-09`

- [ ] **Step 2: Append the job entry**

Add this block under the existing `jobs:` key in `~/.skcapstone/config/jobs.yaml` (matches the file's existing field-comment style, e.g. `sktrip-weekly`):

```yaml
  dreaming-reflection:
    every: 15m                  # was scheduled_tasks.py's hard-coded 900s; now config-driven
    type: python
    nodes: all                  # runs wherever this node's skcapstone daemon runs (unconditional before too)
    callback: "skcapstone.dreaming_job:run_dreaming_job"
    timeout: 300                 # dreaming.request_timeout is 120s; this gives LLM-call headroom
    retries: 0                   # matches pre-migration behavior: no retry on a skipped/failed dream
    notify: off                  # matches pre-migration behavior: silent, DEBUG-only logging
    enabled: true
```

- [ ] **Step 3: Validate the YAML parses and the job is visible**

Run: `~/.skenv/bin/python -c "
from pathlib import Path
from skcapstone.scheduler_jobs import load_jobs_with_dropins
jobs = load_jobs_with_dropins(Path('~/.skcapstone/config/jobs.yaml').expanduser())
job = next(j for j in jobs if j.name == 'dreaming-reflection')
print(job)
assert job.type == 'python'
assert job.callback == 'skcapstone.dreaming_job:run_dreaming_job'
assert job.every_seconds == 900.0
assert job.enabled
print('OK')
"`
Expected: prints the `JobSpec(...)` repr followed by `OK`, no traceback

Run: `skcapstone scheduler list | grep dreaming-reflection`
Expected: a line like `[x] dreaming-reflection          python every 900s          nodes=all`

- [ ] **Step 4: No automated test for this step** — `jobs.yaml` is operator config outside the repo; Task 6 provides the automated-test equivalent using a synthetic `tmp_path` config with the same YAML shape.

- [ ] **Step 5: No commit** (nothing in the git repo changed). If this repo is later given a template/example `jobs.yaml` under version control, mirror this same block there in a follow-up commit.

---

### Task 6: End-to-end integration test (`jobs.yaml` → `TaskScheduler` → `run_dreaming_job`)

**Files:**
- Modify: `tests/test_scheduler_integration.py`

**Interfaces:**
- Consumes: `skcapstone.scheduled_tasks.build_scheduler` (existing), `skcapstone.dreaming_job.{load_dreaming_config,DreamingEngine}` (Task 2, monkeypatched here), `skcapstone.scheduler_state.SchedulerState` (existing).
- Produces: a regression test proving the full plumbing works without hitting a real LLM — this is what CI actually exercises for this migration (Task 5's `jobs.yaml` change itself isn't covered by CI since it lives outside the repo).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler_integration.py`:

```python
def test_dreaming_reflection_job_fires_end_to_end(tmp_path, monkeypatch):
    """Full plumbing: jobs.yaml -> TaskScheduler -> JobRunner -> run_dreaming_job()."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "jobs.yaml").write_text(
        "jobs:\n"
        "  dreaming-reflection:\n"
        "    every: 1s\n"
        "    type: python\n"
        "    nodes: all\n"
        "    callback: skcapstone.dreaming_job:run_dreaming_job\n",
        encoding="utf-8",
    )

    from skcapstone import dreaming_job
    from skcapstone.dreaming import DreamingConfig

    calls = []
    monkeypatch.setattr(
        dreaming_job, "load_dreaming_config", lambda home: DreamingConfig(enabled=True)
    )

    class FakeEngine:
        def __init__(self, **kw):
            calls.append(kw)

        def dream(self):
            return None

    monkeypatch.setattr(dreaming_job, "DreamingEngine", FakeEngine)

    from skcapstone.scheduled_tasks import build_scheduler

    sched = build_scheduler(home=tmp_path, stop_event=threading.Event())
    sched.tick_config_jobs()

    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.05)
    assert calls, "run_dreaming_job should have fired via the jobs.yaml config-job path"
    assert calls[0]["consciousness_loop"] is None  # nothing registered it in this test process

    from skcapstone.scheduler_state import SchedulerState
    import socket

    st = SchedulerState(root=tmp_path, hostname=socket.gethostname())
    assert st.last_run("dreaming-reflection") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -k dreaming_reflection_job_fires -v`
Expected: FAIL — either `assert calls` (job never dispatched) if Tasks 1-2 aren't present yet in this checkout, or PASS already if run after Tasks 1-2. If it fails for import reasons (`dreaming_job` missing), that confirms sequencing; if it fails on `assert calls`, debug via the same `systematic-debugging` skill before proceeding — do not weaken the assertion.

- [ ] **Step 3: No implementation change expected**

This task is pure verification of Tasks 1-4 wired together; if it fails after those tasks are merged, treat it as a bug in Task 1-4's implementation and fix there, not by special-casing this test.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/test_scheduler_integration.py -v`
Expected: all PASS, including the new test

- [ ] **Step 5: Commit**

```bash
git add tests/test_scheduler_integration.py
git commit -m "$(cat <<'EOF'
test(scheduler): add end-to-end coverage for the dreaming-reflection job

Exercises the full jobs.yaml -> TaskScheduler.tick_config_jobs ->
JobRunner._run_python -> dreaming_job.run_dreaming_job path with a fake
DreamingEngine, and asserts SchedulerState records the run. This is the
CI-visible regression guard for the migration (jobs.yaml itself lives
outside the repo).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Manual verification against the live daemon (`--no-consciousness`)

**Files:** none (operational verification only)

**Interfaces:**
- Consumes: `skcapstone scheduler {list,status,run}` CLI (existing, `src/skcapstone/cli/scheduler_cmd.py`).

- [ ] **Step 1: Confirm the currently-running daemon has picked up the change**

Run: `systemctl --user status skcapstone@lumina.service | head -5`

If the change requires a restart to load (editable install + daemon already running), restart it:

Run: `systemctl --user restart skcapstone@lumina.service`

- [ ] **Step 2: Confirm the daemon is running with consciousness OFF (the constraint case)**

Run: `systemctl --user show skcapstone@lumina.service -p ExecStart | grep -o -- '--no-consciousness'`
Expected: `--no-consciousness` printed (confirms this is the chat-coexistence mode the constraint targets). If it's not present, note that and still proceed — this step is diagnostic, not a gate.

- [ ] **Step 3: Confirm the job is registered and not double-firing as a builtin**

Run: `skcapstone scheduler list | grep dreaming`
Expected: exactly one line, `[x] dreaming-reflection ...` — no second `dreaming_reflection` builtin line (Task 4 removed it).

- [ ] **Step 4: Force a manual run and confirm it succeeds without needing consciousness_loop**

Run: `skcapstone scheduler run dreaming-reflection`
Expected: `OK dreaming-reflection done`, or a clean `skipped_reason` path (e.g. still within cooldown / not idle per mood.json) surfaced as success — either is fine; a Python exception/traceback is not.

- [ ] **Step 5: Confirm run-history is visible (status works; per-job log file does not — known gap)**

Run: `skcapstone scheduler status | grep dreaming-reflection`
Expected: a line with `last=<timestamp> status=ok runs=<n>`

Run: `skcapstone scheduler logs dreaming-reflection`
Expected: `No logs for 'dreaming-reflection'.` — this is expected and documented (Task 8), because `JobRunner._run_python` never writes a per-run log file (only `_run_subprocess`-backed `shell`/`agent` jobs do). Do not treat this as a failure; do not attempt to fix it as part of this migration — it's a pre-existing skscheduler limitation affecting every `python`-type job, out of scope here.

- [ ] **Step 6: Confirm chat is unaffected (no inbox race)**

Send a normal message to the agent via its usual chat path (Telegram/skchat) while the dreaming job is not mid-run, and confirm a normal, prompt reply — i.e. the manual `scheduler run` in Step 4 did not leave any lock/thread state that blocks message processing. No code change if this passes; if it does NOT pass, stop and escalate — this directly violates the "must not reintroduce the chat-inbox race" constraint and needs a fresh investigation (via `superpowers:systematic-debugging`), not a quick patch.

- [ ] **No commit** (operational verification, no file changes in this task, aside from possibly Task 5's `jobs.yaml`/backup already committed to nothing since it's outside the repo).

---

### Task 8: Update `docs/skscheduler.md`

**Files:**
- Modify: `docs/skscheduler.md` (the "Architecture" mermaid diagram and the "Migration" section)

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the mermaid diagram's built-in task list**

In `docs/skscheduler.md`, the "Architecture" mermaid block currently has:

```
      TS -->|interval callbacks| IC["heartbeat · memory_promotion<br/>dreaming_reflection · reprobe"]
```

Change it to:

```
      TS -->|interval callbacks| IC["heartbeat · memory_promotion<br/>profile_freshness · reprobe"]
```

- [ ] **Step 2: Add a line to the "Migration" section**

In `docs/skscheduler.md`'s `## Migration` section, add:

```markdown
- `dreaming_reflection` — migrated 2026-07-09 from a `scheduled_tasks.py` built-in
  (hard-coded 900s) to the `dreaming-reflection` config job (`every: 15m`, `type: python`,
  `callback: skcapstone.dreaming_job:run_dreaming_job`). See
  [`docs/superpowers/plans/2026-07-09-dreaming-skscheduler-migration.md`](superpowers/plans/2026-07-09-dreaming-skscheduler-migration.md).
  Known gap: `python`-type jobs get no per-run `logs/<job>-<ts>.log` file (only
  `shell`/`agent` jobs do, via `_run_subprocess`) — use `skcapstone scheduler status`
  for run history instead of `scheduler logs`.
```

- [ ] **Step 3: Verify no other doc references the removed builtin as still-a-builtin**

Run: `grep -rn "dreaming_reflection" docs/ src/`
Expected: only the "migrated 2026-07-09" doc line just added, the `scheduled_tasks.py` module docstring's migration note (Task 4), and the `docs/superpowers/specs/2026-06-08-skscheduler-design.md` historical mention (leave that one alone — it's a dated design doc describing the state *before* this migration, not a live reference).

- [ ] **Step 4: No test** (documentation-only change; Step 3's grep is the verification).

- [ ] **Step 5: Commit**

```bash
git add docs/skscheduler.md
git commit -m "$(cat <<'EOF'
docs(scheduler): document the dreaming_reflection -> jobs.yaml migration

Updates the architecture diagram's builtin-task list and adds a
Migration-section entry pointing at the 2026-07-09 plan, including the
known python-job logging gap (scheduler logs shows nothing; use
scheduler status instead).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

- **Spec coverage:** entrypoint decision (Task 2), remove-vs-flag decision (Task 4, decided: remove), in-process-vs-subprocess confirmation (plan header + Task 1 docstring, sourced from reading `scheduler_runner.py::_run_python`), `--no-consciousness` preservation (Task 2's third test + Task 3's first test explicitly assert `consciousness_loop=None` still works), no-inbox-race constraint (Task 7 Step 6 manual gate), config-driven schedule (Task 5's `every: 15m` replacing the hard-coded `900` in code) — all covered.
- **Placeholder scan:** every step has literal file contents/commands; no "add appropriate handling"-style steps.
- **Type/name consistency:** `set_consciousness_loop`/`get_consciousness_loop` (Task 1) are the exact names used in Tasks 2, 3, and the daemon.py patch; `run_dreaming_job` (Task 2) is the exact `callback:` value used in Task 5 and Task 6; `dreaming-reflection` (hyphenated, matching the repo's existing `sktrip-weekly`-style naming) is used consistently as the `jobs.yaml` key/CLI job name, distinct from the old `dreaming_reflection` (underscored) builtin task name being removed — intentional, so `grep dreaming_reflection` cleanly finds only historical/removed references while `grep dreaming-reflection` finds only the new job.
