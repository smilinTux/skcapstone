# skfleet install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `skfleet install` verb that reads this node's bound profile from the synced fleet store, reports drift (`--check`), and closes every `missing_required` package/unit (`--apply`) by driving the existing per-repo installers as backends.

**Architecture:** An orchestrator (`fleet/installer.py`) turns a `profile_doctor.DriftReport` into an ordered `InstallPlan` and executes each step through a backend registry (`fleet/install_backends.py`) that shells out to the proven per-repo installers. It reads the applied profile from `~/.skcapstone/fleet/objects/profile/<role>.json` (Syncthing-synced), reuses the existing freeze/actuation gates, and never removes anything (only adds `missing_required`).

**Tech Stack:** Python 3.11+, argparse (existing `fleet/cli.py`), pytest, existing `skcapstone.fleet` modules (`profile_doctor`, `store`, `nodeinventory`, `profiles`).

## Global Constraints

- All code lives in `skcapstone/src/skcapstone/fleet/`; tests in `skcapstone/tests/fleet/`.
- Python 3.11+, type hints everywhere, Google-style docstrings (repo CLAUDE.md).
- Format with `black`, run tests with `~/.skenv/bin/python -m pytest`.
- Reads the profile from the SYNCED store (`store.read_spec(paths, "profile", role)`), never from `deploy/fleet-objects/`.
- Only actuates `missing_required`. Never touches `forbidden`/`unexpected`/`allowed`.
- `--apply` is gated by `store.is_frozen(paths)` (refuse if frozen) and `store.actuation_allowed(paths)` (refuse if this node has not opted in). `--check`/`--dry-run` are always allowed.
- Commit after every task with a `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` trailer.

---

### Task 1: Install data model

**Files:**
- Create: `src/skcapstone/fleet/installer.py`
- Test: `tests/fleet/test_installer_model.py`

**Interfaces:**
- Produces: `InstallStep(name: str, kind: str, tier: int, backend_id: str)`; `InstallPlan(steps: list[InstallStep])`; `InstallResult(step: InstallStep, status: str, detail: str = "")` where `status` in `{"ok","wrote","would-write","warn","failed","skipped","needs_manual"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_installer_model.py
from skcapstone.fleet.installer import InstallStep, InstallPlan, InstallResult

def test_install_step_and_plan_are_immutable_records():
    step = InstallStep(name="skgateway.service", kind="unit", tier=4, backend_id="skchat")
    plan = InstallPlan(steps=[step])
    assert plan.steps[0].name == "skgateway.service"
    assert plan.steps[0].tier == 4

def test_install_result_carries_status_and_detail():
    step = InstallStep(name="capauth", kind="package", tier=1, backend_id="packages")
    r = InstallResult(step=step, status="ok")
    assert r.status == "ok" and r.detail == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_model.py -v`
Expected: FAIL with `ModuleNotFoundError` / `ImportError: cannot import name 'InstallStep'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/fleet/installer.py
"""Profile-aware stack installer (orchestrator). See
docs/superpowers/specs/2026-08-16-skfleet-install-orchestrator-design.md."""
from __future__ import annotations

from dataclasses import dataclass, field

@dataclass(frozen=True)
class InstallStep:
    name: str
    kind: str  # "unit" | "package"
    tier: int
    backend_id: str

@dataclass(frozen=True)
class InstallPlan:
    steps: list[InstallStep] = field(default_factory=list)

@dataclass(frozen=True)
class InstallResult:
    step: InstallStep
    status: str  # ok|wrote|would-write|warn|failed|skipped|needs_manual
    detail: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_model.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/fleet/installer.py tests/fleet/test_installer_model.py
git commit -m "feat(fleet): install data model (InstallStep/Plan/Result)"
```

---

### Task 2: Backend registry + resolution

**Files:**
- Create: `src/skcapstone/fleet/install_backends.py`
- Test: `tests/fleet/test_install_backends.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Backend(id: str, install: Callable[[list[str], InstallOpts], InstallOutcome])` where `InstallOpts(dry_run: bool, enable: bool, start: bool)` and `InstallOutcome(status: str, detail: str)`; `resolve(name: str, kind: str) -> str` returns a backend id; `TIER_OF: dict[str, int]`; the module constant `UNSUPPORTED = "unsupported"`.
- Backends map by ordered (glob, backend_id, tier): packages->("*","packages",1); `capauth-authz*`->("capauth-authz",2); `skcomms*`->("skcomms",3); `skcapstone*`,`sknoded*`,`skgateway*`->("core",4); `skchat*`,`livekit-server*`,`jarvis-heartbeat*`,`skchat-coturn*`->("skchat",5); `skmemory-*@*`,`skwhisper@*`,`cloud9-daemon@*`->("agent",6). No match for a required unit -> `UNSUPPORTED` (tier 9).

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_install_backends.py
from skcapstone.fleet.install_backends import resolve, tier_of, UNSUPPORTED

def test_resolve_maps_units_to_owning_backend():
    assert resolve("skgateway.service", "unit") == "core"
    assert resolve("skchat-webui@lumina.service", "unit") == "skchat"
    assert resolve("skwhisper@lumina.service", "unit") == "agent"
    assert resolve("capauth-authz.service", "unit") == "capauth-authz"

def test_packages_kind_always_resolves_to_packages_backend():
    assert resolve("capauth", "package") == "packages"

def test_unknown_required_unit_is_unsupported_not_silent():
    assert resolve("totally-made-up.service", "unit") == UNSUPPORTED

def test_tier_orders_packages_before_skchat_plane():
    assert tier_of("packages") < tier_of("skchat")
    assert tier_of("capauth-authz") < tier_of("skcomms") < tier_of("core")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_backends.py -v`
Expected: FAIL with `ImportError: cannot import name 'resolve'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/skcapstone/fleet/install_backends.py
"""Maps a required package/unit to the repo installer that provides it."""
from __future__ import annotations

import fnmatch

UNSUPPORTED = "unsupported"

# (glob, backend_id) checked in order; first match wins.
_UNIT_RULES: list[tuple[str, str]] = [
    ("capauth-authz*", "capauth-authz"),
    ("skcomms*", "skcomms"),
    ("skmemory-*@*", "agent"),
    ("skwhisper@*", "agent"),
    ("cloud9-daemon@*", "agent"),
    ("skchat*", "skchat"),
    ("livekit-server*", "skchat"),
    ("jarvis-heartbeat*", "skchat"),
    ("skcapstone*", "core"),
    ("sknoded*", "core"),
    ("skgateway*", "core"),
]

_TIER: dict[str, int] = {
    "packages": 1, "capauth-authz": 2, "skcomms": 3, "core": 4,
    "skchat": 5, "agent": 6, UNSUPPORTED: 9,
}

def tier_of(backend_id: str) -> int:
    return _TIER.get(backend_id, 9)

def resolve(name: str, kind: str) -> str:
    if kind == "package":
        return "packages"
    for glob, backend in _UNIT_RULES:
        if fnmatch.fnmatch(name, glob):
            return backend
    return UNSUPPORTED
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_backends.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/fleet/install_backends.py tests/fleet/test_install_backends.py
git commit -m "feat(fleet): backend registry resolving units/packages to installers"
```

---

### Task 3: Build the ordered plan from a DriftReport

**Files:**
- Modify: `src/skcapstone/fleet/installer.py`
- Test: `tests/fleet/test_installer_plan.py`

**Interfaces:**
- Consumes: `profile_doctor.DriftReport` (fields `missing_required_units: list[str]`, `missing_required_packages: list[str]`, plus `forbidden_*`/`unexpected_*` which are IGNORED here); `install_backends.resolve`, `tier_of`.
- Produces: `plan(drift: DriftReport, *, only: list[str] | None = None) -> InstallPlan`. Steps: one per `missing_required_package` (kind "package") and per `missing_required_unit` (kind "unit"), each with its resolved `backend_id` and `tier`, sorted by `(tier, name)`. `forbidden`/`unexpected` produce NO steps. `only` filters steps to names in the set.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_installer_plan.py
from skcapstone.fleet.installer import plan
from skcapstone.fleet.profile_doctor import DriftReport

def _drift(**kw):
    return DriftReport(**kw)

def test_plan_orders_by_tier_then_name_and_ignores_forbidden():
    drift = _drift(
        missing_required_packages=["capauth"],
        missing_required_units=["skgateway.service", "skchat-webui@lumina.service"],
        forbidden_units=["comfyui.service"],       # must NOT produce a step
        unexpected_units=["random.service"],       # must NOT produce a step
    )
    p = plan(drift)
    names = [(s.tier, s.name) for s in p.steps]
    # packages(tier1) -> core skgateway(tier4) -> skchat webui(tier5)
    assert names == [(1, "capauth"), (4, "skgateway.service"), (5, "skchat-webui@lumina.service")]

def test_plan_only_filters_to_named_items():
    drift = _drift(missing_required_units=["skgateway.service", "sknoded.service"])
    p = plan(drift, only=["sknoded.service"])
    assert [s.name for s in p.steps] == ["sknoded.service"]

def test_plan_empty_when_nothing_missing():
    assert plan(_drift()).steps == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_plan.py -v`
Expected: FAIL with `ImportError: cannot import name 'plan'`.

- [ ] **Step 3: Write minimal implementation**

Append to `installer.py`:

```python
from .profile_doctor import DriftReport
from . import install_backends

def plan(drift: DriftReport, *, only: list[str] | None = None) -> InstallPlan:
    """Ordered install steps for the missing_required items only."""
    wanted = set(only) if only is not None else None
    steps: list[InstallStep] = []
    for pkg in drift.missing_required_packages:
        if wanted is None or pkg in wanted:
            bid = install_backends.resolve(pkg, "package")
            steps.append(InstallStep(pkg, "package", install_backends.tier_of(bid), bid))
    for unit in drift.missing_required_units:
        if wanted is None or unit in wanted:
            bid = install_backends.resolve(unit, "unit")
            steps.append(InstallStep(unit, "unit", install_backends.tier_of(bid), bid))
    steps.sort(key=lambda s: (s.tier, s.name))
    return InstallPlan(steps=steps)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_plan.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/skcapstone/fleet/installer.py tests/fleet/test_installer_plan.py
git commit -m "feat(fleet): plan() builds ordered install steps from drift"
```

---

### Task 4: Execute the plan (apply) with failure isolation

**Files:**
- Modify: `src/skcapstone/fleet/installer.py`, `src/skcapstone/fleet/install_backends.py`
- Test: `tests/fleet/test_installer_apply.py`

**Interfaces:**
- Consumes: `InstallPlan`; a `backends: dict[str, Callable]` mapping `backend_id -> fn(names: list[str], *, dry_run, enable, start) -> tuple[str, str]` (status, detail). Injected so tests use fakes.
- Produces: `apply(plan: InstallPlan, backends: dict, *, dry_run=False, enable=False, start=False) -> list[InstallResult]`. One `InstallResult` per step. A backend raising or returning `"failed"` -> that step is `failed` and later steps with a HIGHER tier that share its backend_id are `skipped` (dependency isolation); independent steps still run. `UNSUPPORTED` backend -> `needs_manual`, no call.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_installer_apply.py
from skcapstone.fleet.installer import apply, InstallPlan, InstallStep
from skcapstone.fleet.install_backends import UNSUPPORTED

def _plan(*steps): return InstallPlan(steps=list(steps))

def test_dry_run_calls_backends_with_dry_run_and_reports_would_write():
    calls = []
    backends = {"core": lambda names, **kw: (calls.append(("core", names, kw)) or ("would-write", ""))}
    step = InstallStep("sknoded.service", "unit", 4, "core")
    results = apply(_plan(step), backends, dry_run=True)
    assert results[0].status == "would-write"
    assert calls[0][2]["dry_run"] is True

def test_backend_failure_isolates_and_skips_same_backend_dependents():
    def boom(names, **kw): return ("failed", "exit 1")
    backends = {"core": boom}
    s1 = InstallStep("sknoded.service", "unit", 4, "core")
    s2 = InstallStep("skgateway.service", "unit", 4, "core")
    results = apply(_plan(s1, s2), backends)
    assert results[0].status == "failed"
    assert results[1].status == "skipped"  # same backend, after the failure

def test_unsupported_backend_is_needs_manual_no_call():
    called = []
    backends = {"packages": lambda names, **kw: called.append(names) or ("ok", "")}
    step = InstallStep("made-up.service", "unit", 9, UNSUPPORTED)
    results = apply(_plan(step), backends)
    assert results[0].status == "needs_manual"
    assert called == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_apply.py -v`
Expected: FAIL with `ImportError: cannot import name 'apply'`.

- [ ] **Step 3: Write minimal implementation**

Append to `installer.py`:

```python
from .install_backends import UNSUPPORTED

def apply(plan: InstallPlan, backends: dict, *, dry_run=False, enable=False, start=False) -> list[InstallResult]:
    """Execute each step through its backend; isolate failures per backend."""
    results: list[InstallResult] = []
    failed_backends: set[str] = set()
    # Group names per backend so a backend that installs many at once is called once.
    for step in plan.steps:
        if step.backend_id == UNSUPPORTED:
            results.append(InstallResult(step, "needs_manual", "no backend for this unit"))
            continue
        if step.backend_id in failed_backends:
            results.append(InstallResult(step, "skipped", "a prior step in this backend failed"))
            continue
        fn = backends.get(step.backend_id)
        if fn is None:
            results.append(InstallResult(step, "needs_manual", f"backend {step.backend_id} unregistered"))
            continue
        try:
            status, detail = fn([step.name], dry_run=dry_run, enable=enable, start=start)
        except Exception as exc:  # a backend crash isolates to this step
            status, detail = "failed", str(exc)
        if status == "failed":
            failed_backends.add(step.backend_id)
        results.append(InstallResult(step, status, detail))
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_apply.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fleet): apply() executes the plan, isolates backend failures"
```

---

### Task 5: Real backend adapters (shell out to per-repo installers)

**Files:**
- Modify: `src/skcapstone/fleet/install_backends.py`
- Test: `tests/fleet/test_install_backends_adapters.py`

**Interfaces:**
- Produces: `default_backends(runner=subprocess.run) -> dict[str, Callable]` mapping each backend_id to a function `fn(names, *, dry_run, enable, start) -> tuple[str, str]`. Each shells to its installer via the injected `runner` (default `subprocess.run`), passing `--dry-run` when `dry_run`, `--enable`/`--start` when set. Commands: packages -> `bash <repo>/skcapstone/scripts/install.sh`; skchat -> `bash <repo>/skchat/systemd/install.sh [--diff|--enable|--start]`; skcomms -> `bash <repo>/skcomms/scripts/bootstrap.sh --no-service`; agent -> per-name (`skwhisper install --agent <a>` / `bash <repo>/skmemory/scripts/install-systemd.sh`); core -> the packages installer (units enabled via skchat/systemd where applicable) plus `systemctl --user enable` guarded by `enable`; capauth-authz -> `bash <repo>/capauth/deploy/capauth-service/deploy.sh`. A nonzero runner return -> `("failed", stderr_tail)`; dry_run -> `("would-write", cmd)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_install_backends_adapters.py
from skcapstone.fleet.install_backends import default_backends

class _FakeRunner:
    def __init__(self, rc=0, stderr=""): self.calls=[]; self.rc=rc; self.stderr=stderr
    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        class R: pass
        R.returncode=self.rc; R.stderr=self.stderr; R.stdout=""
        return R

def test_dry_run_reports_would_write_and_passes_dry_run_flag():
    runner=_FakeRunner()
    b=default_backends(runner=runner)
    status, detail = b["skchat"](["skchat-webui@lumina.service"], dry_run=True, enable=False, start=False)
    assert status=="would-write"
    assert any("install.sh" in " ".join(map(str,c)) for c in runner.calls)

def test_nonzero_runner_is_failed_with_stderr_tail():
    runner=_FakeRunner(rc=1, stderr="boom")
    b=default_backends(runner=runner)
    status, detail = b["packages"](["capauth"], dry_run=False, enable=False, start=False)
    assert status=="failed" and "boom" in detail
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_backends_adapters.py -v`
Expected: FAIL with `ImportError: cannot import name 'default_backends'`.

- [ ] **Step 3: Write minimal implementation**

Add a `default_backends(runner=subprocess.run)` factory to `install_backends.py`. Resolve the repos root from `SKCAPSTONE_REPOS` env or the default `~/clawd/skcapstone-repos` / `~/clawd/projects`. Each backend builds its command list, calls `runner(cmd, capture_output=True, text=True)`, and maps the result: `dry_run -> ("would-write", " ".join(cmd))`; `returncode != 0 -> ("failed", stderr[-500:])`; else `("ok", "")`. Keep each adapter a few lines; do NOT reimplement installer logic.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_backends_adapters.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fleet): default backend adapters shell to per-repo installers"
```

---

### Task 6: Load the applied profile + build inventory + diff (cluster-aware)

**Files:**
- Modify: `src/skcapstone/fleet/installer.py`
- Test: `tests/fleet/test_installer_load.py`

**Interfaces:**
- Consumes: `store.read_spec(paths, "profile", role)`, `nodeinventory` (to build the live inventory dict), `profile_doctor.diff(inventory, profile)`.
- Produces: `load_drift(paths, role: str, *, inventory: dict | None = None) -> DriftReport`. Reads the APPLIED profile from the synced store; raises `ProfileNotApplied(role)` if `read_spec` returns None (never falls back to the repo). Builds the inventory via `nodeinventory` unless one is injected (tests inject).

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_installer_load.py
import pytest
from skcapstone.fleet.installer import load_drift, ProfileNotApplied

class _FakePaths: ...

def test_load_drift_raises_when_profile_not_applied(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.read_spec", lambda p,k,n: None)
    with pytest.raises(ProfileNotApplied):
        load_drift(_FakePaths(), "control", inventory={"units": {}, "packages": {}})

def test_load_drift_diffs_applied_profile_against_inventory(monkeypatch):
    profile = {"spec": {"units": {"required": ["sknoded.service"]}, "packages": {"required": []}}}
    monkeypatch.setattr("skcapstone.fleet.installer.store.read_spec", lambda p,k,n: profile)
    drift = load_drift(_FakePaths(), "control", inventory={"units": {}, "packages": {}})
    assert "sknoded.service" in drift.missing_required_units
```

Note: confirm the exact inventory/profile dict shape `profile_doctor.diff` expects by reading `profile_doctor.diff` and `nodeinventory.body`; adjust the fixture to match (the test asserts behavior, not shape).

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_load.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append to `installer.py`: import `store`, `nodeinventory`, `profile_doctor`; define `class ProfileNotApplied(RuntimeError)` and `load_drift(...)` that reads the applied profile spec, raises `ProfileNotApplied` on None, builds inventory when not injected, and returns `profile_doctor.diff(inventory, profile_spec)`. Match the exact arg shapes to what `profile_doctor.diff` and `nodeinventory` actually use (read them first).

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_load.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fleet): load_drift reads the applied profile from the synced store"
```

---

### Task 7: Safety gates + post-apply inventory refresh

**Files:**
- Modify: `src/skcapstone/fleet/installer.py`
- Test: `tests/fleet/test_installer_gates.py`

**Interfaces:**
- Consumes: `store.is_frozen(paths)`, `store.actuation_allowed(paths)`, `nodeinventory` refresh entry point.
- Produces: `run_install(paths, role, *, mode: str, dry_run, enable, start, only, backends) -> dict` where `mode` in `{"check","apply"}`. `apply` refuses (raises `Frozen` / `ActuationNotAllowed`) if frozen or not opted in. After a successful `apply`, calls the inventory refresh so the installed-set publishes to this node's synced object. Returns a JSON-able summary dict `{"role","mode","results":[...],"ok":bool}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_installer_gates.py
import pytest
from skcapstone.fleet.installer import run_install, Frozen, ActuationNotAllowed

class _P: ...

def test_apply_refuses_when_frozen(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: True)
    with pytest.raises(Frozen):
        run_install(_P(), "control", mode="apply", dry_run=False, enable=False, start=False, only=None, backends={})

def test_check_mode_allowed_even_when_frozen(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: True)
    monkeypatch.setattr("skcapstone.fleet.installer.load_drift", lambda p,r,**k: __import__("skcapstone.fleet.profile_doctor", fromlist=["DriftReport"]).DriftReport())
    out = run_install(_P(), "control", mode="check", dry_run=False, enable=False, start=False, only=None, backends={})
    assert out["mode"] == "check" and out["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_gates.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append `class Frozen(RuntimeError)`, `class ActuationNotAllowed(RuntimeError)`, and `run_install(...)`: build drift via `load_drift`; `check` -> summarize the drift and return `ok = not missing_required`; `apply` -> gate on `is_frozen`/`actuation_allowed`, build `plan`, `apply`, then on success call the inventory refresh; return the summary dict. Keep the refresh call injectable/guarded so tests need not stub systemd.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_installer_gates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fleet): run_install gates apply on freeze/opt-in, refreshes inventory"
```

---

### Task 8: Wire the `skfleet install` CLI verb

**Files:**
- Modify: `src/skcapstone/fleet/cli.py`
- Test: `tests/fleet/test_cli_install.py`

**Interfaces:**
- Consumes: `installer.run_install`, `install_backends.default_backends`.
- Produces: a `skfleet install` subcommand: `--role`, `--check` (default), `--apply`, `--dry-run`, `--enable`, `--start`, `--only NAME` (repeatable), `--json`. Resolves the role from the node's spec when `--role` is omitted. Prints a human table or (`--json`) the summary dict. Exit 0 when `ok`, else 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_cli_install.py
import json, subprocess, sys

def test_cli_install_check_json_exit_and_shape(monkeypatch, capsys):
    # Import the parser entry and invoke install --check --json against a stubbed run_install.
    from skcapstone.fleet import cli
    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install",
                        lambda *a, **k: {"role":"control","mode":"check","results":[],"ok":True})
    rc = cli.main(["install", "--role", "control", "--check", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["mode"] == "check"
```

Note: match `cli.main`'s real signature (read `cli.py:816`); if it reads `sys.argv`, adapt the test to `monkeypatch.setattr(sys, "argv", [...])` and assert `SystemExit.code`.

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_cli_install.py -v`
Expected: FAIL (no `install` subcommand / attribute).

- [ ] **Step 3: Write minimal implementation**

Add the `install` subparser to `cli.py` following the existing verb pattern in `main()`, wiring flags to `installer.run_install(..., backends=install_backends.default_backends())`, printing JSON or a table, returning/exiting the right code.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_cli_install.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(fleet): skfleet install verb (check/apply/dry-run/json)"
```

---

### Task 9: Folded cleanup + follow-up card

**Files:**
- Modify: `scripts/install.sh` (skcapstone)
- Create: a coord card note for the skos/install reconciliation
- Test: `tests/fleet/test_install_sh_no_phantom_units.py`

**Interfaces:** none (hygiene).

- [ ] **Step 1: Write the failing test**

```python
# tests/fleet/test_install_sh_no_phantom_units.py
from pathlib import Path
def test_install_sh_references_no_phantom_skchat_units():
    text = Path("scripts/install.sh").read_text()
    for phantom in ("skchat-lumina-bridge", "skchat-bridges.target"):
        assert phantom not in text, f"scripts/install.sh still references {phantom}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_sh_no_phantom_units.py -v`
Expected: FAIL (the phantom names are still present).

- [ ] **Step 3: Fix `scripts/install.sh`**

Remove/replace the `skchat-lumina-bridge` and `skchat-bridges.target` references with the real units from `skchat/systemd/units/` (or drop the skchat-enable block, since the skchat backend now owns that). Keep the change minimal.

- [ ] **Step 4: Run test to verify it passes**

Run: `~/.skenv/bin/python -m pytest tests/fleet/test_install_sh_no_phantom_units.py -v`
Expected: PASS.

- [ ] **Step 5: Create the reconciliation follow-up card + commit**

```bash
skcapstone coord create --title "Reconcile skos/install/ profile mechanism into skfleet profiles" \
  --desc "skos ships its own planner/profiles/provisioner (src/skos/install/) distinct from skcapstone fleet profiles. Subsume or bridge so there is one profile store, now that skfleet install is the actuation path." \
  --tag fleet --tag follow-up --priority medium
git add -A && git commit -m "fix(install): drop phantom skchat unit names; card skos/install reconcile"
```

---

### Task 10: End-to-end check against a fixture profile (integration)

**Files:**
- Test: `tests/fleet/test_install_e2e_check.py`

**Interfaces:** exercises `run_install(mode="check")` end to end with a temp fleet store containing a `control` profile and a stubbed inventory.

- [ ] **Step 1: Write the failing test** (writes a temp profile spec into a temp `FleetPaths`, stubs `nodeinventory` to report an empty node, calls `run_install(paths, "control", mode="check", ...)`, asserts the summary lists the profile's required units as missing and `ok is False`).

- [ ] **Step 2: Run to verify it fails**, then

- [ ] **Step 3: Make it pass** by fixing any real-shape mismatches surfaced (this is the task that proves the load/diff/plan chain matches the real `store`/`profile_doctor`/`nodeinventory` shapes).

- [ ] **Step 4: Run the whole fleet test suite**

Run: `~/.skenv/bin/python -m pytest tests/fleet/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "test(fleet): e2e check of skfleet install against a fixture profile"
```

---

## Self-Review

- **Spec coverage:** verb + flags (Task 8), orchestrator plan/apply (Tasks 1,3,4), backend registry + adapters (Tasks 2,5), read-from-synced-store + cluster-awareness (Task 6), safety gates + post-apply refresh (Task 7), reconcile contract via the skchat backend + dry-run (Tasks 5,8), folded cleanups (Task 9), integration proof (Task 10). Removal-out-of-scope and local-node-only are honored (no removal step exists; no remote step exists).
- **Placeholders:** none; each code step has real code. Task 6/8/10 explicitly instruct reading `profile_doctor.diff`/`nodeinventory`/`cli.main` real shapes before finalizing the fixture, which is a verification instruction, not a placeholder.
- **Type consistency:** `InstallStep(name,kind,tier,backend_id)`, `InstallResult(step,status,detail)`, `plan(drift,only)`, `apply(plan,backends,dry_run,enable,start)`, `resolve(name,kind)`, `tier_of(id)`, `load_drift`, `run_install`, `default_backends` are used consistently across tasks.
