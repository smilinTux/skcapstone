# Fleet Control Plane, Phase 3: Implementation Plan (TDD, bite-sized)

Date: 2026-07-28
Author: Fable
Parent spec: `2026-07-27-skworld-fleet-control-plane-design.md` (rev 2)
Executes: Phase 3 (Cards 3.1, 3.2, 3.3, 3.4, 3.5)
Prior plans: `2026-07-27-fleet-phase1-implementation-plan.md`,
`2026-07-28-fleet-phase2-implementation-plan.md` (both merged; every
interface referenced below was verified against the merged code on main,
not the plan texts)

## Goal

Ship declarative, self-healing Services: a Service kind with spec
conventions, sknoded ACTUATION (drive `systemctl --user` and Docker to match
desired state, with crash-loop backoff and degrade-safe rules), a
ServiceController on the control-plane side (placement requests, drift
visibility, manual-default failover with sk-alert), a pilot fleet set,
`skfleet drain`, onboarding of the remaining long-running services, and the
signing hardening card (capauth/PGP signed spec and placement writes,
verified by sknoded before actuating, permissive-then-enforce).

## Architecture

Actuation lives entirely in sknoded (spec section 6, steps 2 to 4): a
30-second converge loop reads placements addressed to this node plus the
corresponding Service specs, diffs against local `systemctl --user` and
Docker state through a small verb library (`fleet/actuation.py`, the
trustee-verb actuation library: state, start, restart, logs-on-failure,
every action audited to the event log), and converges. Every actuation path
checks `store.actuation_allowed(paths)` first, actuation is opt-in per node
via an operator-owned `spec.actuate` flag on the Node object (all nodes are
born report-only), and an unreadable spec or unreachable tree never touches
a running service. The ServiceController runs on the control-plane node: it
requests placements through the Phase 2 scheduler (place-once policy: an
existing placement is never moved unless the node is Dead AND
`spec.failover` is `auto`), raises drift at read time, and alerts on
node-Dead under the `manual` default instead of re-placing. Card 3.5 fills
the Phase 1 writer-identity seam: spec and placement writes gain a detached
capauth/PGP signature over canonical payload bytes, and sknoded verifies
before actuating, warn-only first, refuse under enforce.

## Tech stack

- Python 3.11+, stdlib + click, pytest. Same venv and conventions as
  Phases 1 and 2.
- Repo: `/home/cbrd21/clawd/skcapstone-repos/skcapstone/` (src layout,
  package `skcapstone.fleet`). Test command from repo root:
  `~/.skenv/bin/python -m pytest tests/fleet/ -v`
- Reused Phase 1 + 2 code (real, merged signatures, verified):
  `FleetPaths` (`spec_path`, `placement_path`, `status_path`,
  `node_status_dir`, `freeze_path`), `default_paths()`, `self_node_name()`,
  `valid_name`, `store.Writer(role, node, identity)`,
  `store.write_spec(paths, kind, name, spec, *, writer, labels=None)`,
  `store.read_spec`, `store.list_specs`, `store.write_status(paths, kind,
  name, *, node, status, conditions, observed_generation, writer)`,
  `store.read_status`, `store.merged`, `store.is_frozen`,
  `store.set_frozen`, `store.actuation_allowed(paths)`,
  `store.write_placement(paths, kind, name, *, node, reason, writer)`,
  `store.read_placement`, `store.list_placements`, `store.OwnershipError`,
  `store.writer_identity()`,
  `events.emit(paths, writer, *, kind, name, type, reason, message,
  now=None)`, `events.read`, `events.reset_dedupe`,
  `scheduler.Workload(kind, name, node_selector, tolerations, requests)`,
  `scheduler.DEFAULT_REQUESTS`, `scheduler.select`, `scheduler.place(paths,
  workload, *, writer, views=None)`,
  `node_controller.NodeView`, `node_controller.node_views(paths, *,
  now=None)`, `node_controller.cordon(paths, name, cordoned, *, writer)`,
  `conditions.node_conditions(capacity, fleet_root, now_iso)`,
  `conditions.merge_transitions(new, old)`,
  `sknoded.run_once(paths, node)`, `sknoded.build_node_report`,
  `sknoded.main_loop`, `explain.KINDS`, `admission.PRESETS`, the `fleet`
  click group in `cli.py` with `_operator()`, and the systemd unit
  `systemd/sknoded.service`.
- capauth (Card 3.5, lazy soft imports only):
  `capauth.resolve_capauth_home()`, `capauth.crypto.get_backend()` with
  `backend.sign(data: bytes, private_key_armor: str, passphrase: str) ->
  str` and `backend.verify(data: bytes, signature_armor: str,
  public_key_armor: str) -> bool` (read from the merged capauth repo). Key
  material layout: `<capauth_home>/identity/private.asc` and `public.asc`.
- sk-alert: best-effort subprocess, same pattern as the merged
  `scheduled_tasks.TaskScheduler._maybe_notify` (`shutil.which("sk-alert")`
  fallback `~/.skenv/bin/sk-alert`, `subprocess.run([alert, "-l", level,
  msg], timeout=30, check=False)`, never raises).

## Global constraints (binding, copied from the spec)

1. Single-writer-per-FILE, fleet-wide: every file in the fleet tree has
   exactly one writer in the whole fleet, ever. sknoded writes only
   `status/$(self)/`; it NEVER writes placements or spec. The scheduler
   writes only placements. The ServiceController originates placement
   requests through the scheduler write path and emits events; it never
   writes status and never edits spec.
2. Actuation gates on freeze: every actuation path checks
   `store.actuation_allowed(paths)` FIRST, before any verb. While frozen,
   sknoded halts ALL actuation (running services are left running,
   self-report and status probing continue) and the scheduler writes no
   placements (`scheduler.place` already self-gates).
3. Failover default is `manual` plus sk-alert on node-Dead. Never
   auto-replace in v1 unless the Service explicitly sets
   `spec.failover: auto`. An existing placement is never moved by capacity
   changes, only by Dead-plus-auto.
4. Degrade-safe (spec 6 step 4): an unreadable spec, an unreadable
   placement, or an unreachable fleet tree keeps last-known services
   running. sknoded never stops or restarts a unit because it cannot read
   desired state; it skips, emits a deduped event, and moves on.
5. Report-only by default: sknoded actuates only when the operator-owned
   Node spec carries `actuate: true`. Every node (and especially the
   local/interactive box) is born report-only until explicitly opted in.
6. Crash-loop backoff, bounded: restart delays 10s doubling to a 300s cap
   (spec 3.3), a bounded number of attempts per episode, then a
   `CrashLooping` condition plus one deduped alert instead of restart
   storms. Never infinite restarts.
7. R2 flood discipline: write-on-change-else-skip for all status files;
   events deduped and bounded exactly as in Phase 1; alerts fire only when
   the corresponding event actually appended (the event dedupe window is
   the alert rate cap).
8. Every spec file carries a `generation`; every status file carries
   `observedGeneration`; every placement carries a `placementGeneration`.
   Staleness is always detectable, never silent; stale data renders as
   `Unknown`, not `False`.
9. Hermetic tests: every test runs against a `tmp_path` fleet tree
   (`FleetPaths` fixture / `SKFLEET_ROOT`). Tests NEVER start, stop, or
   inspect a real systemd unit or container: every actuation verb takes an
   injectable runner and tests always inject a fake. `subprocess` is never
   invoked by the test suite's actuation paths.
10. Dash ban: NEVER use em dashes or en dashes anywhere (code, docstrings,
    comments, docs, commit messages). Regular hyphens are fine.
11. New code lives in `skcapstone/fleet/`; on disk `src/skcapstone/fleet/`,
    tests under `tests/fleet/`. Type hints everywhere, Google-style
    docstrings, black formatting, commits end with the standard
    `Co-Authored-By` trailer.

Card mapping: Tasks 1-4 are Card 3.1, Tasks 5-6 are Card 3.2, Tasks 7-8 are
Card 3.3, Task 9 is Card 3.4, Tasks 10-11 are Card 3.5.

No conftest changes are needed: the existing `paths`, `operator`,
`noded41`, and `scheduler_writer` fixtures cover Phase 3. Tests that need a
different sknoded node construct `store.Writer(role="sknoded", ...)`
inline, and every converge test builds its own fake runner.

---

## Task 1: Service kind model + explain registry (services.py)

Card: 3.1. The spec-side half of the Service kind: normalization with
defaults, validation, and the mapping from a Service spec file to a
scheduler `Workload`. Registered in `explain.KINDS` so the self-describing
surface grows with the kind, as the Phase 2 self-review promised.

Files:
- Create `src/skcapstone/fleet/services.py`
- Modify `src/skcapstone/fleet/explain.py` (add the "service" entry)
- Create `tests/fleet/test_services.py`

Interfaces (produced):

```python
RUNTIMES: frozenset = frozenset({"systemd-user", "docker"})
FAILOVER_MODES: frozenset = frozenset({"manual", "auto"})
RESTART_POLICIES: frozenset = frozenset({"on-failure", "never"})
class ServiceSpecError(ValueError)
def normalize_service_spec(spec: dict) -> dict
# full spec with defaults: runtime, unit, replicas(=1), nodeSelector,
# tolerations, resources, healthCheck, restartPolicy, failover, paused,
# compose (docker only), deleted
def service_workload(payload: dict) -> Workload
# payload is a full spec FILE dict {"name", "labels", "spec", ...}
```

Consumes (real code): `scheduler.Workload`, `scheduler.DEFAULT_REQUESTS`,
`explain.KINDS` dict shape (one entry per kind: kind, description, spec,
status, conditions, actions).

Steps:

1. Write the failing test, `tests/fleet/test_services.py`:

```python
"""Tests for the Service kind model (spec 5.2): defaults, validation, workload."""
from __future__ import annotations

import pytest

from skcapstone.fleet import explain, services


def test_defaults_are_conservative() -> None:
    spec = services.normalize_service_spec({"unit": "skgateway.service"})
    assert spec["runtime"] == "systemd-user"
    assert spec["replicas"] == 1
    assert spec["nodeSelector"] == {}
    assert spec["tolerations"] == []
    assert spec["resources"] == {"cores": 1, "ram_gb": 2.0}
    assert spec["healthCheck"] is None
    assert spec["restartPolicy"] == "on-failure"
    assert spec["failover"] == "manual"          # never auto by default (R4)
    assert spec["paused"] is False
    assert spec["deleted"] is False
    assert spec["compose"] is None


def test_explicit_fields_survive() -> None:
    spec = services.normalize_service_spec(
        {
            "unit": "coturn",
            "runtime": "docker",
            "nodeSelector": {"always-on": "true"},
            "tolerations": [{"key": "dedicated"}],
            "resources": {"cores": 2, "ram_gb": 4.0},
            "healthCheck": {"port": 3478},
            "failover": "auto",
            "paused": True,
            "compose": {"file": "/opt/coturn/compose.yml", "service": "coturn"},
        }
    )
    assert spec["runtime"] == "docker"
    assert spec["healthCheck"] == {"port": 3478}
    assert spec["failover"] == "auto"
    assert spec["paused"] is True
    assert spec["compose"]["service"] == "coturn"


def test_replicas_clamped_to_one_in_v1() -> None:
    spec = services.normalize_service_spec({"unit": "u.service", "replicas": 3})
    assert spec["replicas"] == 1


def test_validation_errors() -> None:
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({})                       # no unit
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "runtime": "kubelet"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "failover": "instant"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "restartPolicy": "always"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "healthCheck": {"exec": "x"}})


def test_service_workload_mapping() -> None:
    payload = {
        "kind": "Service",
        "name": "skgateway",
        "labels": {"tier": "core"},
        "generation": 3,
        "spec": {
            "unit": "skgateway.service",
            "nodeSelector": {"always-on": "true"},
            "tolerations": [{"key": "dedicated", "value": "model-serving"}],
            "resources": {"cores": 2, "ram_gb": 1.0},
        },
    }
    wl = services.service_workload(payload)
    assert wl.kind == "service" and wl.name == "skgateway"
    assert wl.node_selector == {"always-on": "true"}
    assert wl.tolerations == ({"key": "dedicated", "value": "model-serving"},)
    assert wl.requests == {"cores": 2, "ram_gb": 1.0}


def test_explain_registers_service() -> None:
    assert "service" in explain.explain()["kinds"]
    entry = explain.explain("service")
    assert entry["kind"] == "Service"
    for field in ("runtime", "unit", "replicas", "nodeSelector", "tolerations",
                  "resources", "healthCheck", "restartPolicy", "failover",
                  "paused"):
        assert field in entry["spec"]
    for cond in ("Ready", "Progressing", "CrashLooping", "SpecUnverified"):
        assert cond in entry["conditions"]
```

2. Run to fail:
   `~/.skenv/bin/python -m pytest tests/fleet/test_services.py -v`
   Expected: `ImportError: cannot import name 'services' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/services.py`:

```python
"""Service kind model (spec 5.2): normalization, validation, workload map.

The spec side only. Actuation lives in converge.py (sknoded side) and
placement policy in service_controller.py (control-plane side).
"""

from __future__ import annotations

from .scheduler import DEFAULT_REQUESTS, Workload

RUNTIMES = frozenset({"systemd-user", "docker"})
FAILOVER_MODES = frozenset({"manual", "auto"})
RESTART_POLICIES = frozenset({"on-failure", "never"})


class ServiceSpecError(ValueError):
    """A Service spec is malformed and must not be actuated."""


def normalize_service_spec(spec: dict) -> dict:
    """Return a full Service spec with defaults applied, or raise.

    Defaults are deliberately conservative (R4): failover manual, restart
    on-failure with backoff, one replica, not paused. A spec that fails
    validation must never reach an actuation verb; callers treat
    ServiceSpecError as "do not touch the unit" (degrade-safe).

    Raises:
        ServiceSpecError: missing unit, unknown runtime/failover/policy,
            or an unsupported healthCheck shape.
    """
    unit = spec.get("unit")
    if not unit or not isinstance(unit, str):
        raise ServiceSpecError("spec.unit is required (unit name or container)")
    runtime = spec.get("runtime", "systemd-user")
    if runtime not in RUNTIMES:
        raise ServiceSpecError(f"unknown runtime {runtime!r} (known: {sorted(RUNTIMES)})")
    failover = spec.get("failover", "manual")
    if failover not in FAILOVER_MODES:
        raise ServiceSpecError(f"unknown failover {failover!r} (known: {sorted(FAILOVER_MODES)})")
    policy = spec.get("restartPolicy", "on-failure")
    if policy not in RESTART_POLICIES:
        raise ServiceSpecError(
            f"unknown restartPolicy {policy!r} (known: {sorted(RESTART_POLICIES)})"
        )
    health = spec.get("healthCheck")
    if health is not None and (not isinstance(health, dict) or "port" not in health):
        raise ServiceSpecError("healthCheck must be {'port': int} in v1")
    return {
        "runtime": runtime,
        "unit": unit,
        "replicas": 1,  # v1: always one (spec 5.2, replicas almost always 1)
        "nodeSelector": dict(spec.get("nodeSelector", {})),
        "tolerations": list(spec.get("tolerations", [])),
        "resources": dict(spec.get("resources", DEFAULT_REQUESTS)),
        "healthCheck": dict(health) if health else None,
        "restartPolicy": policy,
        "failover": failover,
        "paused": bool(spec.get("paused", False)),
        "deleted": bool(spec.get("deleted", False)),
        "compose": dict(spec["compose"]) if spec.get("compose") else None,
    }


def service_workload(payload: dict) -> Workload:
    """Map a full Service spec file to the scheduler's Workload."""
    spec = normalize_service_spec(payload.get("spec", {}))
    return Workload(
        kind="service",
        name=payload["name"],
        node_selector=spec["nodeSelector"],
        tolerations=tuple(spec["tolerations"]),
        requests=spec["resources"],
    )
```

   In `src/skcapstone/fleet/explain.py`, add to `KINDS` after the "node"
   entry:

```python
    "service": {
        "kind": "Service",
        "description": "A long-running workload (systemd --user unit or Docker container).",
        "spec": {
            "runtime": "systemd-user | docker",
            "unit": "systemd unit name, or container name for docker",
            "replicas": "always 1 in v1",
            "nodeSelector": "label map used by the scheduler (exact match, AND)",
            "tolerations": "list of {key, optional value} tolerating NoSchedule taints",
            "resources": "requested {cores, ram_gb}, advisory, checked as headroom",
            "healthCheck": "{'port': int} tcp probe, or null",
            "restartPolicy": "on-failure (heal with backoff) | never",
            "failover": "manual (default: alert on node-Dead) | auto (re-place)",
            "paused": "bool; true stops healing, never stops the unit",
            "compose": "docker only: {'file': path, 'service': name} for compose",
            "deleted": "tombstone; stops management, never stops the unit",
        },
        "status": {
            "state": "active | failed | inactive | activating | missing | unknown",
            "pid": "main PID when running",
            "since": "ActiveEnterTimestamp (or container StartedAt)",
            "restarts": "heal attempts in the current episode",
            "conditions": "list of {type, status, reason, message, lastTransition}",
        },
        "conditions": {
            "Ready": "unit active and health check (if any) passing",
            "Progressing": "sknoded is actively converging this service",
            "CrashLooping": "bounded restart attempts exhausted; healing stopped",
            "SpecUnverified": "spec/placement signature missing or invalid (Card 3.5)",
        },
        "actions": [
            "skfleet apply -f <file>",
            "skfleet services",
            "skfleet describe service <name>",
            "skfleet reconcile",
            "skfleet drain <node>",
        ],
    },
```

4. Run to pass, then the whole `tests/fleet/` suite (the Phase 1 explain
   test asserts on the node entry only and stays green).
5. Commit: `feat(fleet): Service kind model + explain registry (Card 3.1)`

---

## Task 2: systemd actuation verb library (actuation.py)

Card: 3.1. The trustee-verb actuation library for sknoded (spec section 10:
trustee_* is the verb vocabulary, wrapped for unit-level use): state, start,
restart, logs-on-failure. Reality note, from reading the merged code: the
literal `trustee_ops.TrusteeOps` verbs bind to TeamEngine deployments
(agent teams behind a provider), not to arbitrary systemd units, so sknoded
gets a thin unit-level verb module with the same verb semantics and audit
discipline, modeled on the merged `skcapstone.systemd._systemctl` pattern.
The trustee MCP tools stay untouched as the manual surface. Every verb
takes an injectable runner; tests never touch subprocess.

Files:
- Create `src/skcapstone/fleet/actuation.py`
- Create `tests/fleet/test_actuation.py`

Interfaces (produced):

```python
Runner = Callable[[list[str]], subprocess.CompletedProcess]
def default_runner(cmd: list[str]) -> subprocess.CompletedProcess
@dataclass(frozen=True)
class UnitState:
    state: str          # active|failed|inactive|activating|missing|unknown
    pid: int | None
    since: str
def systemd_state(unit: str, *, runner: Runner) -> UnitState
def systemd_start(unit: str, *, runner: Runner) -> bool
def systemd_restart(unit: str, *, runner: Runner) -> bool
def systemd_logs(unit: str, lines: int = 30, *, runner: Runner) -> str
```

Consumes: stdlib only (subprocess for the default runner). Docker verbs
arrive in Task 7 in this same module.

Steps:

1. Write the failing test, `tests/fleet/test_actuation.py`:

```python
"""Tests for the systemd --user actuation verbs (all runners faked)."""
from __future__ import annotations

from subprocess import CompletedProcess

from skcapstone.fleet import actuation


class FakeRunner:
    """Records every command; replies from a canned map."""

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out = self.replies.get(" ".join(cmd), (0, ""))
        return CompletedProcess(cmd, code, stdout=out, stderr="")


SHOW = ("systemctl --user show skgateway.service "
        "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp")


def test_state_active() -> None:
    runner = FakeRunner({SHOW: (0, "LoadState=loaded\nActiveState=active\n"
                                   "MainPID=4242\n"
                                   "ActiveEnterTimestamp=Mon 2026-07-27 09:00:00 UTC\n")})
    st = actuation.systemd_state("skgateway.service", runner=runner)
    assert st.state == "active" and st.pid == 4242
    assert st.since == "Mon 2026-07-27 09:00:00 UTC"


def test_state_failed_and_missing_and_unknown() -> None:
    runner = FakeRunner({SHOW: (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n"
                                   "ActiveEnterTimestamp=\n")})
    assert actuation.systemd_state("skgateway.service", runner=runner).state == "failed"
    runner = FakeRunner({SHOW: (0, "LoadState=not-found\nActiveState=inactive\n"
                                   "MainPID=0\nActiveEnterTimestamp=\n")})
    assert actuation.systemd_state("skgateway.service", runner=runner).state == "missing"
    runner = FakeRunner({SHOW: (1, "")})
    st = actuation.systemd_state("skgateway.service", runner=runner)
    assert st.state == "unknown" and st.pid is None


def test_start_restart_and_logs() -> None:
    runner = FakeRunner({
        "systemctl --user start u.service": (0, ""),
        "systemctl --user restart u.service": (1, ""),
        "journalctl --user -u u.service -n 30 --no-pager": (0, "boom line\n"),
    })
    assert actuation.systemd_start("u.service", runner=runner) is True
    assert actuation.systemd_restart("u.service", runner=runner) is False
    assert actuation.systemd_logs("u.service", runner=runner) == "boom line"
    assert runner.calls[0] == ["systemctl", "--user", "start", "u.service"]


def test_runner_exception_degrades_to_unknown() -> None:
    def boom(cmd: list[str]):
        raise OSError("no systemd here")

    st = actuation.systemd_state("u.service", runner=boom)
    assert st.state == "unknown"
    assert actuation.systemd_start("u.service", runner=boom) is False
    assert actuation.systemd_logs("u.service", runner=boom) == ""


def test_default_runner_shape(monkeypatch) -> None:
    import subprocess

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    actuation.default_runner(["systemctl", "--user", "--version"])
    assert seen["cmd"] == ["systemctl", "--user", "--version"]
    assert seen["kw"]["capture_output"] is True and seen["kw"]["timeout"] == 30
```

2. Run to fail. Expected:
   `ImportError: cannot import name 'actuation' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/actuation.py`:

```python
"""Unit-level actuation verbs for sknoded (spec 5.2, section 6 step 2).

The trustee verb vocabulary (state, start/restart, logs on failure)
applied to systemd --user units, modeled on skcapstone.systemd's
_systemctl pattern. Docker verbs live here too (Task 7). Every verb takes
an injectable runner so tests never touch a real unit or container, and
every failure degrades to a safe answer (unknown / False / "") instead of
raising into the converge loop.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run one actuation command, captured, bounded, never check=True."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)


@dataclass(frozen=True)
class UnitState:
    """Observed state of one unit or container.

    Attributes:
        state: active|failed|inactive|activating|missing|unknown.
        pid: Main PID when running, else None.
        since: Start timestamp string as reported, "" when unknown.
    """

    state: str
    pid: int | None
    since: str


_UNKNOWN = UnitState(state="unknown", pid=None, since="")


def systemd_state(unit: str, *, runner: Runner) -> UnitState:
    """Observe one systemd --user unit. Degrades to unknown, never raises."""
    try:
        out = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp",
            ]
        )
    except Exception:
        return _UNKNOWN
    if out.returncode != 0:
        return _UNKNOWN
    props: dict[str, str] = {}
    for line in (out.stdout or "").splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    if props.get("LoadState") == "not-found":
        return UnitState(state="missing", pid=None, since="")
    active = props.get("ActiveState", "unknown")
    if active not in {"active", "failed", "inactive", "activating"}:
        active = "unknown"
    pid: int | None = None
    try:
        pid = int(props.get("MainPID", "0")) or None
    except ValueError:
        pid = None
    return UnitState(state=active, pid=pid, since=props.get("ActiveEnterTimestamp", ""))


def _verb(cmd: list[str], runner: Runner) -> bool:
    try:
        return runner(cmd).returncode == 0
    except Exception:
        return False


def systemd_start(unit: str, *, runner: Runner) -> bool:
    """Start a unit. True on rc=0, False on failure (never raises)."""
    return _verb(["systemctl", "--user", "start", unit], runner)


def systemd_restart(unit: str, *, runner: Runner) -> bool:
    """Restart a unit. True on rc=0, False on failure (never raises)."""
    return _verb(["systemctl", "--user", "restart", unit], runner)


def systemd_logs(unit: str, lines: int = 30, *, runner: Runner) -> str:
    """Tail the unit's journal (logs-on-failure verb). "" when unavailable."""
    try:
        out = runner(["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager"])
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()
```

4. Run to pass.
5. Commit: `feat(fleet): systemd --user actuation verb library (injectable runner)`

---

## Task 3: Crash-loop backoff + alert helper (backoff.py, alerts.py)

Card: 3.1 (constraint 6). Pure, clock-injected backoff state so the
converge loop can never storm restarts, plus the best-effort sk-alert
helper every later task reuses. The tracker is in-process (like the events
dedupe map), keyed by (node, service); persisted status carries only the
attempt count, so status files stay write-on-change friendly.

Files:
- Create `src/skcapstone/fleet/backoff.py`
- Create `src/skcapstone/fleet/alerts.py`
- Create `tests/fleet/test_backoff.py`

Interfaces (produced):

```python
# backoff.py
BACKOFF_BASE_S: float = 10.0
BACKOFF_CAP_S: float = 300.0
CRASH_LOOP_AFTER: int = 5
HEALTHY_RESET_S: float = 120.0
def reset_trackers() -> None
def tracker(node: str, name: str) -> dict     # {"attempts": int, "last_attempt": float}
def next_delay(attempts: int) -> float
def allowed(track: dict, now: float) -> bool
def record_attempt(track: dict, now: float) -> None
def record_healthy(track: dict, now: float) -> None
def is_crash_looping(track: dict) -> bool

# alerts.py
def send_alert(message: str, *, level: str = "warn") -> bool
```

Consumes: stdlib only.

Steps:

1. Write the failing test, `tests/fleet/test_backoff.py`:

```python
"""Tests for bounded crash-loop backoff (10s doubling to 300s, then stop)."""
from __future__ import annotations

import pytest

from skcapstone.fleet import alerts, backoff


@pytest.fixture(autouse=True)
def _fresh():
    backoff.reset_trackers()
    yield
    backoff.reset_trackers()


def test_delay_schedule_doubles_and_caps() -> None:
    assert backoff.next_delay(0) == 0.0            # first heal is immediate
    assert backoff.next_delay(1) == 10.0
    assert backoff.next_delay(2) == 20.0
    assert backoff.next_delay(3) == 40.0
    assert backoff.next_delay(6) == 300.0          # capped at 5 minutes
    assert backoff.next_delay(50) == 300.0


def test_allowed_respects_delay() -> None:
    track = backoff.tracker("node-41", "skgateway")
    assert backoff.allowed(track, now=1000.0) is True
    backoff.record_attempt(track, now=1000.0)
    assert backoff.allowed(track, now=1005.0) is False   # 10s not elapsed
    assert backoff.allowed(track, now=1010.0) is True
    backoff.record_attempt(track, now=1010.0)
    assert backoff.allowed(track, now=1025.0) is False   # now needs 20s
    assert backoff.allowed(track, now=1030.0) is True


def test_bounded_attempts_then_crash_looping() -> None:
    track = backoff.tracker("node-41", "skgateway")
    now = 1000.0
    for _ in range(backoff.CRASH_LOOP_AFTER):
        assert backoff.is_crash_looping(track) is False
        now += backoff.next_delay(track["attempts"])
        assert backoff.allowed(track, now) is True
        backoff.record_attempt(track, now)
    assert backoff.is_crash_looping(track) is True       # bounded: healing stops


def test_healthy_reset_clears_the_episode() -> None:
    track = backoff.tracker("node-41", "skgateway")
    for i in range(backoff.CRASH_LOOP_AFTER):
        backoff.record_attempt(track, now=1000.0 + i * 400.0)
    assert backoff.is_crash_looping(track) is True
    last = track["last_attempt"]
    backoff.record_healthy(track, now=last + 60.0)       # too soon: no reset
    assert backoff.is_crash_looping(track) is True
    backoff.record_healthy(track, now=last + backoff.HEALTHY_RESET_S + 1.0)
    assert track["attempts"] == 0
    assert backoff.is_crash_looping(track) is False


def test_trackers_are_per_service() -> None:
    a = backoff.tracker("node-41", "svc-a")
    backoff.record_attempt(a, now=1000.0)
    assert backoff.tracker("node-41", "svc-b")["attempts"] == 0
    assert backoff.tracker("node-41", "svc-a") is a


def test_send_alert_never_raises(monkeypatch) -> None:
    import subprocess

    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert alerts.send_alert("fleet: skgateway CrashLooping", level="error") is True
    assert calls["cmd"][-1] == "fleet: skgateway CrashLooping"
    assert "-l" in calls["cmd"] and "error" in calls["cmd"]

    def boom(cmd, **kw):
        raise OSError("no sk-alert")

    monkeypatch.setattr(subprocess, "run", boom)
    assert alerts.send_alert("still fine") is False      # best-effort, no raise
```

2. Run to fail. Expected:
   `ImportError: cannot import name 'alerts' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/backoff.py`:

```python
"""Bounded crash-loop backoff for sknoded healing (spec 3.3, R4).

Delays double from 10s to a 300s cap; after CRASH_LOOP_AFTER attempts in
one episode the service is declared CrashLooping and healing STOPS until
the unit is observed healthy for HEALTHY_RESET_S (or sknoded restarts).
Trackers are in-process state, like the events dedupe map: a daemon
restart forgives the episode, which is the desired manual-recovery lever.
"""

from __future__ import annotations

BACKOFF_BASE_S = 10.0
BACKOFF_CAP_S = 300.0
CRASH_LOOP_AFTER = 5
HEALTHY_RESET_S = 120.0

_trackers: dict[tuple[str, str], dict] = {}


def reset_trackers() -> None:
    """Clear all backoff state (tests, daemon restart)."""
    _trackers.clear()


def tracker(node: str, name: str) -> dict:
    """The mutable backoff record for one service on one node."""
    return _trackers.setdefault((node, name), {"attempts": 0, "last_attempt": 0.0})


def next_delay(attempts: int) -> float:
    """Seconds to wait before the next heal attempt (0 for the first)."""
    if attempts <= 0:
        return 0.0
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempts - 1)))


def allowed(track: dict, now: float) -> bool:
    """True when the backoff window for the next attempt has passed."""
    return (now - float(track["last_attempt"])) >= next_delay(int(track["attempts"]))


def record_attempt(track: dict, now: float) -> None:
    """Count one heal attempt (start or restart), successful or not."""
    track["attempts"] = int(track["attempts"]) + 1
    track["last_attempt"] = now


def record_healthy(track: dict, now: float) -> None:
    """Reset the episode after the unit has been stably healthy."""
    if track["attempts"] and (now - float(track["last_attempt"])) >= HEALTHY_RESET_S:
        track["attempts"] = 0
        track["last_attempt"] = 0.0


def is_crash_looping(track: dict) -> bool:
    """True when the bounded attempt budget for this episode is spent."""
    return int(track["attempts"]) >= CRASH_LOOP_AFTER
```

   and `src/skcapstone/fleet/alerts.py`:

```python
"""Best-effort sk-alert notifications for fleet components.

Same discipline as scheduled_tasks._maybe_notify: locate the sk-alert
CLI, bounded subprocess, never raise into a control loop. Callers gate
alerts on events.emit() returning True, so the event dedupe window is
also the alert rate cap (R2).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("skcapstone.fleet.alerts")


def send_alert(message: str, *, level: str = "warn") -> bool:
    """Fire one sk-alert. Returns False (never raises) on any failure."""
    alert = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        out = subprocess.run([alert, "-l", level, message], timeout=30, check=False)
        return out.returncode == 0
    except Exception as exc:
        logger.warning("sk-alert failed: %s", exc)
        return False
```

4. Run to pass.
5. Commit: `feat(fleet): bounded crash-loop backoff + sk-alert helper`

---

## Task 4: sknoded converge loop (converge.py) + main_loop wiring

Card: 3.1, the heart of Phase 3. The 30s actuation pass of spec section 6
steps 2 to 4. Order of gates inside one pass, each independently tested:

1. Tree readable? If listing placements raises, return mode "degraded"
   having touched NOTHING (degrade-safe: last-known services keep running).
2. Freeze first: `store.actuation_allowed(paths)` is checked before any
   verb. Frozen means probe and report only.
3. Opt-in: actuation requires `actuate: true` on the operator-owned Node
   spec (`skfleet actuation <node> --enable`, Task 6). Default report-only
   for every node, which is exactly the local-box rule.
4. Per service: placement addressed to me, spec readable and valid,
   `paused` honored, then runtime diff and heal under backoff. An
   unreadable or invalid spec skips the service with a deduped event and
   zero verbs.

Status is written through `store.write_status` (sknoded-owned files,
write-on-change) so the ownership guard makes it physically impossible for
converge to write outside `status/<self>/`. Every actuation action and
anomaly is one deduped event; the CrashLooping alert fires only when its
event actually appended.

Files:
- Create `src/skcapstone/fleet/converge.py`
- Modify `src/skcapstone/fleet/sknoded.py` (main_loop interleaves converge)
- Modify `src/skcapstone/fleet/cli.py` (sknoded command gains
  `--actuation-interval`)
- Create `tests/fleet/test_converge.py`

Interfaces (produced):

```python
ACTUATION_INTERVAL_S: int = 30
def tcp_probe(check: dict) -> bool               # {"port": int}, localhost connect
def actuation_enabled(paths: FleetPaths, node: str) -> bool
def local_services(paths: FleetPaths, node: str) -> list[dict]
# [{"name": str, "placement": dict, "spec_payload": dict | None}, ...]
def converge_once(paths: FleetPaths, node: str, *,
                  runner: Runner | None = None,
                  prober: Callable[[dict], bool] | None = None,
                  now: float | None = None) -> dict
# {"mode": "actuate"|"report-only"|"frozen"|"degraded", "services": {name: summary}}

# sknoded.py (changed)
def main_loop(paths: FleetPaths, node: str, *, interval: int = HEARTBEAT_INTERVAL_S,
              once: bool = False,
              actuation_interval: int | None = None) -> None
```

Consumes (real code): `store.actuation_allowed`, `store.read_spec`,
`store.list_placements`, `store.write_status`, `store.Writer`,
`store.writer_identity`, `events.emit`, `conditions.merge_transitions`,
`services.normalize_service_spec` (Task 1), `actuation.*` (Task 2),
`backoff.*` and `alerts.send_alert` (Task 3).

Steps:

1. Write the failing test, `tests/fleet/test_converge.py`:

```python
"""Tests for the sknoded converge loop: gates, healing, degrade-safe."""
from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import backoff, converge, events, store

NODE = "node-41"
SHOW = ("systemctl --user show skgateway.service "
        "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp")
ACTIVE = (0, "LoadState=loaded\nActiveState=active\nMainPID=42\n"
             "ActiveEnterTimestamp=t0\n")
FAILED = (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n"
             "ActiveEnterTimestamp=\n")


class FakeRunner:
    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = dict(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out = self.replies.get(" ".join(cmd), (0, ""))
        return CompletedProcess(cmd, code, stdout=out, stderr="")

    def verbs(self) -> list[str]:
        return [" ".join(c) for c in self.calls
                if c[:2] == ["systemctl", "--user"] and c[2] in ("start", "restart")]


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    backoff.reset_trackers()
    yield
    events.reset_dedupe()
    backoff.reset_trackers()


def _fleet(paths, operator, scheduler_writer, *, actuate=True, spec=None) -> None:
    node_spec = {"cordoned": False, "taints": []}
    if actuate:
        node_spec["actuate"] = True
    store.write_spec(paths, "node", NODE, node_spec, writer=operator)
    store.write_spec(paths, "service", "skgateway",
                     spec or {"unit": "skgateway.service"}, writer=operator)
    store.write_placement(paths, "service", "skgateway", node=NODE,
                          reason="pinned for test", writer=scheduler_writer)


def test_healthy_service_writes_status_and_no_verbs(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner({SHOW: ACTIVE})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "actuate"
    assert runner.verbs() == []                       # in sync: no actuation
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "active" and st["status"]["pid"] == 42
    assert st["observedGeneration"] == 1
    conds = {c["type"]: c["status"] for c in st["conditions"]}
    assert conds["Ready"] == "True" and conds["CrashLooping"] == "False"


def test_failed_service_is_healed_with_logs_event(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner({
        SHOW: FAILED,
        "systemctl --user restart skgateway.service": (0, ""),
        "journalctl --user -u skgateway.service -n 30 --no-pager": (0, "segv\n"),
    })
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    reasons = [e["reason"] for e in logged]
    assert "FailureLogs" in reasons and "Restarted" in reasons
    assert any(e["message"] == "segv" for e in logged if e["reason"] == "FailureLogs")


def test_missing_unit_is_started(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner({
        SHOW: (0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\n"
                  "ActiveEnterTimestamp=\n"),
        "systemctl --user start skgateway.service": (0, ""),
    })
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user start skgateway.service"]


def test_freeze_halts_all_actuation_but_not_reporting(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    store.set_frozen(paths, True, writer=operator, reason="drill")
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "frozen"
    assert runner.verbs() == []                       # kill-switch: zero verbs
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "failed"          # self-report continues


def test_report_only_without_opt_in(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer, actuate=False)
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "report-only"
    assert runner.verbs() == []
    assert store.read_status(paths, "service", "skgateway", NODE) is not None


def test_paused_spec_stops_healing(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer,
           spec={"unit": "skgateway.service", "paused": True})
    runner = FakeRunner({SHOW: FAILED})
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []
    st = store.read_status(paths, "service", "skgateway", NODE)
    prog = {c["type"]: c for c in st["conditions"]}["Progressing"]
    assert prog["status"] == "False" and prog["reason"] == "Paused"


def test_unreadable_spec_never_touches_the_unit(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    paths.spec_path("service", "skgateway").write_text("not json")
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []                       # degrade-safe: no verbs
    assert runner.calls == []                         # not even a state probe
    assert out["services"]["skgateway"]["skipped"] == "spec unreadable"
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    assert [e["reason"] for e in logged] == ["SpecUnreadable"]


def test_unreachable_tree_is_degraded_noop(paths, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("syncthing tree gone")

    monkeypatch.setattr(converge.store, "list_placements", boom)
    out = converge.converge_once(paths, NODE, runner=FakeRunner({}), now=1000.0)
    assert out == {"mode": "degraded", "services": {}}


def test_crash_loop_backoff_and_condition(paths, operator, scheduler_writer, monkeypatch) -> None:
    _fleet(paths, operator, scheduler_writer)
    alerted: list[str] = []
    monkeypatch.setattr(converge.alerts, "send_alert",
                        lambda msg, **kw: alerted.append(msg) or True)
    runner = FakeRunner({
        SHOW: FAILED,
        "systemctl --user restart skgateway.service": (0, ""),
        "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
    })
    now = 1000.0
    for i in range(backoff.CRASH_LOOP_AFTER):
        converge.converge_once(paths, NODE, runner=runner, now=now)
        now += backoff.next_delay(i + 1)
    heals = len(runner.verbs())
    assert heals == backoff.CRASH_LOOP_AFTER          # bounded attempt budget
    converge.converge_once(paths, NODE, runner=runner, now=now + 1.0)
    assert len(runner.verbs()) == heals               # looping: healing stopped
    st = store.read_status(paths, "service", "skgateway", NODE)
    conds = {c["type"]: c["status"] for c in st["conditions"]}
    assert conds["CrashLooping"] == "True"
    assert any("CrashLooping" in m for m in alerted)  # alerted exactly via event
    assert len(alerted) == 1                          # dedupe window caps alerts


def test_backoff_window_skips_early_retry(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner({
        SHOW: FAILED,
        "systemctl --user restart skgateway.service": (0, ""),
        "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
    })
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    converge.converge_once(paths, NODE, runner=runner, now=1005.0)   # inside 10s
    assert len(runner.verbs()) == 1                   # second pass waited


def test_health_probe_gates_ready(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer,
           spec={"unit": "skgateway.service", "healthCheck": {"port": 18780}})
    runner = FakeRunner({SHOW: ACTIVE})
    converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                           prober=lambda check: False)
    st = store.read_status(paths, "service", "skgateway", NODE)
    ready = {c["type"]: c for c in st["conditions"]}["Ready"]
    assert ready["status"] == "False" and ready["reason"] == "ProbeFailed"


def test_placement_elsewhere_is_ignored(paths, operator, scheduler_writer) -> None:
    store.write_spec(paths, "node", NODE, {"actuate": True}, writer=operator)
    store.write_spec(paths, "service", "skgateway", {"unit": "u.service"},
                     writer=operator)
    store.write_placement(paths, "service", "skgateway", node="node-158",
                          reason="r", writer=scheduler_writer)
    runner = FakeRunner({})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["services"] == {} and runner.calls == []
```

2. Run to fail. Expected:
   `ImportError: cannot import name 'converge' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/converge.py`:

```python
"""sknoded actuation: the 30s converge pass (spec section 6, steps 2-4).

Gate order is the whole point: tree readable, then freeze, then per-node
opt-in, then per-service spec validity and pause, and only then verbs
under bounded backoff. Anything unreadable degrades to "touch nothing".
"""

from __future__ import annotations

import socket
import time
from typing import Callable

from . import actuation, alerts, backoff, events, store
from .paths import FleetPaths
from .services import ServiceSpecError, normalize_service_spec

ACTUATION_INTERVAL_S = 30


def tcp_probe(check: dict) -> bool:
    """v1 health check: TCP connect to localhost:port."""
    try:
        with socket.create_connection(("127.0.0.1", int(check["port"])), timeout=1.0):
            return True
    except Exception:
        return False


def actuation_enabled(paths: FleetPaths, node: str) -> bool:
    """True only when the operator opted this node in (spec R4, section 6).

    Missing node object, unreadable spec, or absent flag all mean
    report-only. Every node is born report-only.
    """
    spec = store.read_spec(paths, "node", node)
    if spec is None:
        return False
    return bool(spec.get("spec", {}).get("actuate"))


def local_services(paths: FleetPaths, node: str) -> list[dict]:
    """Service placements addressed to this node, joined with their specs.

    spec_payload is None when the spec file is missing or unreadable; the
    caller must treat that as "do not touch" (degrade-safe).
    """
    out: list[dict] = []
    for placement in store.list_placements(paths, "service"):
        if placement.get("node") != node:
            continue
        name = placement["name"]
        out.append(
            {
                "name": name,
                "placement": placement,
                "spec_payload": store.read_spec(paths, "service", name),
            }
        )
    return out


def _cond(type: str, active: bool, reason: str, message: str, now_iso: str) -> dict:
    return {
        "type": type,
        "status": "True" if active else "False",
        "reason": reason,
        "message": message,
        "lastTransition": now_iso,
    }


def _now_iso(now: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_service_status(
    paths: FleetPaths,
    writer: store.Writer,
    name: str,
    state: actuation.UnitState,
    spec: dict,
    generation: int,
    conds: list[dict],
    track: dict,
) -> None:
    from .conditions import merge_transitions

    previous = store.read_status(paths, "service", name, writer.node) or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
    store.write_status(
        paths,
        "service",
        name,
        node=writer.node,
        status={
            "state": state.state,
            "pid": state.pid,
            "since": state.since,
            "restarts": int(track["attempts"]),
            "runtime": spec["runtime"],
        },
        conditions=conds,
        observed_generation=generation,
        writer=writer,
    )


def _heal(
    paths: FleetPaths,
    writer: store.Writer,
    name: str,
    spec: dict,
    state: actuation.UnitState,
    track: dict,
    runner: actuation.Runner,
    now: float,
) -> None:
    """One bounded heal attempt (start or restart) with logs-on-failure."""
    if state.state == "failed":
        logs = actuation.systemd_logs(spec["unit"], runner=runner)
        events.emit(paths, writer, kind="service", name=name, type="Actuation",
                    reason="FailureLogs", message=logs[-800:], now=now)
        ok = actuation.systemd_restart(spec["unit"], runner=runner)
        reason = "Restarted" if ok else "RestartFailed"
    else:
        ok = actuation.systemd_start(spec["unit"], runner=runner)
        reason = "Started" if ok else "StartFailed"
    backoff.record_attempt(track, now)
    events.emit(paths, writer, kind="service", name=name, type="Actuation",
                reason=reason, message=f"unit={spec['unit']} attempt={track['attempts']}",
                now=now)


def converge_service(
    paths: FleetPaths,
    node: str,
    name: str,
    spec_payload: dict | None,
    *,
    writer: store.Writer,
    runner: actuation.Runner,
    prober: Callable[[dict], bool],
    mode: str,
    now: float,
) -> dict:
    """Converge one locally placed service. Returns a summary dict."""
    now_iso = _now_iso(now)
    if spec_payload is None:
        events.emit(paths, writer, kind="service", name=name, type="Degrade",
                    reason="SpecUnreadable",
                    message="spec missing or unreadable; unit left untouched", now=now)
        return {"skipped": "spec unreadable"}
    try:
        spec = normalize_service_spec(spec_payload.get("spec", {}))
    except ServiceSpecError as exc:
        events.emit(paths, writer, kind="service", name=name, type="Degrade",
                    reason="SpecInvalid", message=str(exc), now=now)
        return {"skipped": f"spec invalid: {exc}"}
    if spec["deleted"]:
        return {"skipped": "tombstoned (deleted: true); unit left untouched"}

    state = actuation.systemd_state(spec["unit"], runner=runner)
    track = backoff.tracker(node, name)
    probe_ok = True
    if spec["healthCheck"] is not None and state.state == "active":
        probe_ok = prober(spec["healthCheck"])
    healthy = state.state == "active" and probe_ok
    acted = "none"

    if healthy:
        backoff.record_healthy(track, now)
    unhealthy_unit = state.state in {"failed", "inactive", "missing"}
    may_heal = (
        mode == "actuate"
        and not spec["paused"]
        and spec["restartPolicy"] == "on-failure"
        and unhealthy_unit
    )
    if may_heal:
        if backoff.is_crash_looping(track):
            if events.emit(paths, writer, kind="service", name=name, type="Actuation",
                           reason="CrashLooping",
                           message=f"unit={spec['unit']} attempts={track['attempts']}; "
                                   "healing stopped", now=now):
                alerts.send_alert(
                    f"fleet: service {name} CrashLooping on {node} "
                    f"({track['attempts']} attempts); healing stopped", level="error")
            acted = "crash-looping"
        elif backoff.allowed(track, now):
            _heal(paths, writer, name, spec, state, track, runner, now)
            acted = "healed"
        else:
            acted = "backoff-wait"

    if healthy:
        ready = _cond("Ready", True, "UnitActive", f"unit {spec['unit']} active", now_iso)
    elif state.state == "active" and not probe_ok:
        ready = _cond("Ready", False, "ProbeFailed",
                      f"port {spec['healthCheck']['port']} closed", now_iso)
    elif state.state == "unknown":
        ready = {**_cond("Ready", False, "StateUnknown", "unit state unknown", now_iso),
                 "status": "Unknown"}
    else:
        ready = _cond("Ready", False, "UnitDown", f"unit state {state.state}", now_iso)
    if mode != "actuate":
        prog = _cond("Progressing", False,
                     "Frozen" if mode == "frozen" else "ReportOnly",
                     "actuation halted" if mode == "frozen" else "node not opted in",
                     now_iso)
    elif spec["paused"]:
        prog = _cond("Progressing", False, "Paused", "spec.paused is true", now_iso)
    else:
        prog = _cond("Progressing", acted in {"healed", "backoff-wait"},
                     "Healing" if acted in {"healed", "backoff-wait"} else "Converged",
                     f"last action: {acted}", now_iso)
    conds = [
        ready,
        prog,
        _cond("CrashLooping", backoff.is_crash_looping(track), "BackoffExhausted"
              if backoff.is_crash_looping(track) else "WithinBudget",
              f"attempts={track['attempts']}", now_iso),
    ]
    generation = int(spec_payload.get("generation", 0))
    _write_service_status(paths, writer, name, state, spec, generation, conds, track)
    return {"state": state.state, "acted": acted}


def converge_once(
    paths: FleetPaths,
    node: str,
    *,
    runner: actuation.Runner | None = None,
    prober: Callable[[dict], bool] | None = None,
    now: float | None = None,
) -> dict:
    """One actuation pass for this node (spec section 6, steps 2-4)."""
    runner = actuation.default_runner if runner is None else runner
    prober = tcp_probe if prober is None else prober
    now = time.time() if now is None else now
    try:
        if not store.actuation_allowed(paths):
            mode = "frozen"
        elif actuation_enabled(paths, node):
            mode = "actuate"
        else:
            mode = "report-only"
        entries = local_services(paths, node)
    except OSError:
        return {"mode": "degraded", "services": {}}
    writer = store.Writer(role="sknoded", node=node, identity=store.writer_identity())
    results: dict[str, dict] = {}
    for entry in entries:
        results[entry["name"]] = converge_service(
            paths, node, entry["name"], entry["spec_payload"],
            writer=writer, runner=runner, prober=prober, mode=mode, now=now)
    return {"mode": mode, "services": results}
```

   In `src/skcapstone/fleet/sknoded.py`, replace `main_loop` with the
   interleaved loop (self-report every `interval`, converge every
   `actuation_interval`):

```python
def main_loop(
    paths: FleetPaths,
    node: str,
    *,
    interval: int = HEARTBEAT_INTERVAL_S,
    once: bool = False,
    actuation_interval: int | None = None,
) -> None:
    """The daemon loop behind sknoded.service.

    Self-report runs every `interval` seconds; the Phase 3 converge pass
    runs every `actuation_interval` seconds (default 30, spec 3.3). The
    converge pass re-reads the freeze flag and the node's actuate opt-in
    every time, so both are live level-triggered gates.
    """
    from .converge import ACTUATION_INTERVAL_S, converge_once

    act_every = ACTUATION_INTERVAL_S if actuation_interval is None else actuation_interval
    last_report = 0.0
    while True:
        now = time.time()
        if now - last_report >= interval or last_report == 0.0:
            run_once(paths, node)
            last_report = now
        converge_once(paths, node)
        if once:
            return
        time.sleep(act_every)
```

   In `src/skcapstone/fleet/cli.py`, extend the sknoded command:

```python
@fleet.command("sknoded")
@click.option("--once", is_flag=True, help="One self-report + converge pass, then exit.")
@click.option("--interval", default=sknoded_mod.HEARTBEAT_INTERVAL_S, show_default=True)
@click.option("--actuation-interval", "actuation_interval", default=None, type=int,
              help="Seconds between converge passes (default 30).")
def sknoded_cmd(once: bool, interval: int, actuation_interval: int | None) -> None:
    """Run the node agent loop (self-report + Phase 3 converge)."""
    sknoded_mod.main_loop(
        default_paths(), self_node_name(), interval=interval, once=once,
        actuation_interval=actuation_interval,
    )
```

   Add to `tests/fleet/test_converge.py` (same file, appended; part of this
   test cycle):

```python
def test_main_loop_once_reports_and_converges(paths, monkeypatch) -> None:
    from skcapstone.fleet import sknoded

    ran: list[str] = []
    monkeypatch.setattr(sknoded, "run_once", lambda p, n: ran.append("report"))
    monkeypatch.setattr("skcapstone.fleet.converge.converge_once",
                        lambda p, n: ran.append("converge") or {"mode": "report-only",
                                                                "services": {}})
    sknoded.main_loop(paths, NODE, once=True)
    assert ran == ["report", "converge"]
```

4. Run to pass, then the whole `tests/fleet/` suite. The Phase 1 sknoded
   tests still pass: `run_once` is untouched, `main_loop(once=True)` now
   also calls converge, which is a read-only no-op on a tree with no
   placements (right-sized complexity: zero Service objects cost nothing).
5. Commit: `feat(fleet): sknoded converge loop (freeze gate, opt-in actuation, backoff, degrade-safe)`

---

## Task 5: ServiceController: place-once, manual failover, drift rows (service_controller.py)

Card: 3.2. The control-plane half. Three deliberately conservative rules,
each pinned by a test:

1. Place-once: a Service with no placement gets one via the Phase 2
   scheduler. An EXISTING placement is never rewritten because headroom
   changed; services do not migrate behind the operator's back (R4).
2. Failover: when the placed node is Dead (per NodeController phases),
   `failover: manual` (the default) fires one deduped sk-alert and changes
   nothing; only `failover: auto` re-places, and the Phase 2 `feasible`
   filter already excludes the Dead node so `scheduler.place` lands on a
   live survivor and bumps `placementGeneration`.
3. Drift is computed at READ time (`service_rows`), never persisted:
   status files stay sknoded-owned (single-writer), and stale or missing
   observations render as `Unknown`, not `False`.

The controller writes placements only through the scheduler seat and emits
events through the controller seat; it never writes status and never edits
spec.

Files:
- Create `src/skcapstone/fleet/service_controller.py`
- Create `tests/fleet/test_service_controller.py`

Interfaces (produced):

```python
@dataclass(frozen=True)
class ServiceRow:
    name: str
    node: str | None      # placement target, None when unplaced
    state: str            # observed state or "unobserved"
    ready: str            # "True" | "False" | "Unknown"
    paused: bool
    stale: bool
def reconcile_once(paths: FleetPaths, *, node: str,
                   views: list[NodeView] | None = None,
                   alert: Callable[..., bool] = alerts.send_alert) -> dict
# {"placed": [...], "failovers": [...], "alerted": [...], "kept": [...],
#  "skipped": [...]}
def service_rows(paths: FleetPaths) -> list[ServiceRow]
```

Consumes (real code): `store.list_specs`, `store.read_placement`,
`store.merged`, `store.Writer`, `store.writer_identity`, `events.emit`
(role `controller` is already in the merged emit allow-set),
`scheduler.place`, `node_controller.node_views`, `node_controller.NodeView`,
`services.service_workload` and `normalize_service_spec` (Task 1),
`alerts.send_alert` (Task 3).

Steps:

1. Write the failing test, `tests/fleet/test_service_controller.py`:

```python
"""Tests for ServiceController: place-once, manual failover, drift rows."""
from __future__ import annotations

import pytest

from skcapstone.fleet import events, service_controller, store
from skcapstone.fleet.node_controller import NodeView


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _views(dead41: bool = False) -> list[NodeView]:
    return [
        NodeView(name="node-158", phase="Ready",
                 labels={"always-on": "true", "control-plane": "true"},
                 allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Dead" if dead41 else "Ready",
                 labels={"heavy-build": "true", "always-on": "true"},
                 allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0}),
    ]


def _svc(paths, operator, name="skgateway", **spec_kw) -> None:
    spec = {"unit": f"{name}.service", "nodeSelector": {"always-on": "true"}}
    spec.update(spec_kw)
    store.write_spec(paths, "service", name, spec, writer=operator)


def test_first_reconcile_places_unplaced_services(paths, operator) -> None:
    _svc(paths, operator)
    out = service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                            alert=lambda *a, **k: True)
    assert out["placed"] == ["skgateway"]
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"


def test_place_once_never_moves_on_capacity_change(paths, operator) -> None:
    _svc(paths, operator)
    service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                      alert=lambda *a, **k: True)
    flipped = [
        NodeView(name="node-158", phase="Ready", labels={"always-on": "true"},
                 allocatable={"cores": 7, "ram_gb": 64.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Ready", labels={"always-on": "true"},
                 allocatable={"cores": 15, "ram_gb": 1.0, "disk_gb": 200.0}),
    ]
    out = service_controller.reconcile_once(paths, node="node-158", views=flipped,
                                            alert=lambda *a, **k: True)
    assert out["kept"] == ["skgateway"] and out["placed"] == []
    placement = store.read_placement(paths, "service", "skgateway")
    assert placement["node"] == "node-41"            # unmoved
    assert placement["placementGeneration"] == 1     # zero churn


def test_manual_failover_alerts_and_never_replaces(paths, operator) -> None:
    _svc(paths, operator)                            # failover defaults to manual
    service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                      alert=lambda *a, **k: True)
    alerted: list[str] = []
    out = service_controller.reconcile_once(
        paths, node="node-158", views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True)
    assert out["alerted"] == ["skgateway"] and out["failovers"] == []
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"
    assert alerted and "node-41" in alerted[0] and "skgateway" in alerted[0]
    # second pass inside the dedupe window: event suppressed, alert suppressed
    alerted.clear()
    service_controller.reconcile_once(
        paths, node="node-158", views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True)
    assert alerted == []


def test_auto_failover_replaces_onto_live_node(paths, operator) -> None:
    _svc(paths, operator, failover="auto")
    service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                      alert=lambda *a, **k: True)
    out = service_controller.reconcile_once(paths, node="node-158",
                                            views=_views(dead41=True),
                                            alert=lambda *a, **k: True)
    assert out["failovers"] == ["skgateway"]
    placement = store.read_placement(paths, "service", "skgateway")
    assert placement["node"] == "node-158"           # Dead node filtered out
    assert placement["placementGeneration"] == 2


def test_frozen_blocks_placements_but_not_the_dead_alert(paths, operator) -> None:
    _svc(paths, operator)
    service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                      alert=lambda *a, **k: True)
    store.set_frozen(paths, True, writer=operator, reason="drill")
    _svc(paths, operator, name="skcomms")            # new, unplaced service
    alerted: list[str] = []
    out = service_controller.reconcile_once(
        paths, node="node-158", views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True)
    assert store.read_placement(paths, "service", "skcomms") is None   # frozen
    assert out["alerted"] == ["skgateway"] and alerted                 # alert lives


def test_deleted_and_invalid_specs_are_skipped(paths, operator) -> None:
    _svc(paths, operator, deleted=True)
    store.write_spec(paths, "service", "broken", {"runtime": "docker"},
                     writer=operator)                # no unit: invalid
    out = service_controller.reconcile_once(paths, node="node-158", views=_views(),
                                            alert=lambda *a, **k: True)
    assert out["placed"] == []
    assert sorted(out["skipped"]) == ["broken", "skgateway"]


def test_service_rows_drift_and_unknown(paths, operator, scheduler_writer, noded41) -> None:
    _svc(paths, operator)
    store.write_placement(paths, "service", "skgateway", node="node-41",
                          reason="r", writer=scheduler_writer)
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].ready == "Unknown"      # no observation yet
    assert rows["skgateway"].state == "unobserved"
    store.write_status(paths, "service", "skgateway", node="node-41",
                       status={"state": "active", "pid": 1, "since": "t",
                               "restarts": 0, "runtime": "systemd-user"},
                       conditions=[{"type": "Ready", "status": "True",
                                    "reason": "UnitActive", "message": "ok",
                                    "lastTransition": "t"}],
                       observed_generation=1, writer=noded41)
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].ready == "True" and rows["skgateway"].state == "active"
    assert rows["skgateway"].stale is False
    store.write_spec(paths, "service", "skgateway",
                     {"unit": "skgateway.service", "paused": True}, writer=operator)
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].stale is True
    assert rows["skgateway"].ready == "Unknown"      # stale renders Unknown
    assert rows["skgateway"].paused is True
```

2. Run to fail. Expected:
   `ImportError: cannot import name 'service_controller' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/service_controller.py`:

```python
"""ServiceController (spec 5.2): place-once, conservative failover, drift.

Runs on the control-plane node (a tick wired in Task 6). It requests
placements via the Phase 2 scheduler and emits events; it never writes
status (sknoded-owned) and never edits spec (operator-owned). Failover
defaults to manual: node-Dead fires one deduped alert and moves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import alerts, events, scheduler, store
from .node_controller import NodeView, node_views
from .paths import FleetPaths
from .services import ServiceSpecError, normalize_service_spec, service_workload


@dataclass(frozen=True)
class ServiceRow:
    """One row of skfleet services (read-time merge, nothing persisted)."""

    name: str
    node: str | None
    state: str
    ready: str
    paused: bool
    stale: bool


def _ready_from(status: dict | None) -> str:
    if status is None or status.get("stale"):
        return "Unknown"
    for cond in status.get("conditions", []):
        if cond.get("type") == "Ready":
            return str(cond.get("status", "Unknown"))
    return "Unknown"


def service_rows(paths: FleetPaths) -> list[ServiceRow]:
    """All Services with placement, observed state, and staleness flags."""
    rows: list[ServiceRow] = []
    for payload in store.list_specs(paths, "service"):
        name = payload["name"]
        merged = store.merged(paths, "service", name) or {}
        placement = merged.get("placement")
        target = placement.get("node") if placement else None
        status = None
        for st in merged.get("statuses", []):
            if target is None or st.get("node") == target:
                status = st
                break
        state = "unobserved" if status is None else str(
            status.get("status", {}).get("state", "unknown"))
        if status is not None and status.get("stale"):
            state = "unobserved" if state == "unobserved" else state
        rows.append(
            ServiceRow(
                name=name,
                node=target,
                state=state,
                ready=_ready_from(status),
                paused=bool(payload.get("spec", {}).get("paused", False)),
                stale=bool(status.get("stale")) if status else False,
            )
        )
    return rows


def reconcile_once(
    paths: FleetPaths,
    *,
    node: str,
    views: list[NodeView] | None = None,
    alert: Callable[..., bool] = alerts.send_alert,
) -> dict:
    """One controller pass: place the unplaced, watch the placed.

    Placement policy (R4, deliberately conservative):
    - no placement yet: request one via scheduler.place (freeze-gated there)
    - placement exists and its node is not Dead: keep, always
    - placement node Dead + failover auto: re-place (feasible() excludes
      the Dead node, placementGeneration bumps)
    - placement node Dead + failover manual: one deduped alert, no writes
    """
    views = node_views(paths) if views is None else views
    phases = {v.name: v.phase for v in views}
    sched = store.Writer(role="scheduler", node=node, identity=store.writer_identity())
    ctrl = store.Writer(role="controller", node=node, identity=store.writer_identity())
    out: dict = {"placed": [], "failovers": [], "alerted": [], "kept": [], "skipped": []}
    for payload in store.list_specs(paths, "service"):
        name = payload["name"]
        try:
            spec = normalize_service_spec(payload.get("spec", {}))
        except ServiceSpecError as exc:
            events.emit(paths, ctrl, kind="service", name=name, type="Config",
                        reason="SpecInvalid", message=str(exc))
            out["skipped"].append(name)
            continue
        if spec["deleted"]:
            out["skipped"].append(name)
            continue
        workload = service_workload(payload)
        existing = store.read_placement(paths, "service", name)
        if existing is None:
            placed = scheduler.place(paths, workload, writer=sched, views=views)
            if placed is not None:
                out["placed"].append(name)
            continue
        phase = phases.get(existing.get("node"), "Dead")
        if phase != "Dead":
            out["kept"].append(name)
            continue
        if spec["failover"] == "auto":
            placed = scheduler.place(paths, workload, writer=sched, views=views)
            if placed is not None and placed.get("node") != existing.get("node"):
                out["failovers"].append(name)
            else:
                out["kept"].append(name)
            continue
        message = (f"service {name} is placed on {existing.get('node')} which is "
                   f"Dead; failover=manual, no automatic re-place (move it with "
                   f"skfleet or set failover: auto)")
        if events.emit(paths, ctrl, kind="service", name=name, type="Failover",
                       reason="NodeDead", message=message):
            alert(f"fleet: {message}", level="error")
        out["alerted"].append(name)
    return out
```

4. Run to pass, then the whole `tests/fleet/` suite.
5. Commit: `feat(fleet): ServiceController (place-once, manual failover + alert, drift rows)`

---

## Task 6: CLI surface + pilot fleet set (apply, services, reconcile, actuation)

Card: 3.2. The operator surface for Services plus the pilot Service
objects. `skfleet apply -f` is the minimal spec-writing verb (full dry-run
arrives with Card 8.1; this is the plain write path the ownership table
already names). The pilot set ships as JSON docs in the repo
(`docs/fleet/pilot-services/`), applied by runbook, never hand-authored
into the tree. The actuation opt-in toggle lands next to cordon in
node_controller so the flag stays an operator-owned Node spec field.

Files:
- Modify `src/skcapstone/fleet/node_controller.py` (append `set_actuation`)
- Modify `src/skcapstone/fleet/cli.py` (add `apply`, `services`,
  `reconcile`, `actuation` commands)
- Create `docs/fleet/pilot-services/skwhisper-lumina.json`
- Create `docs/fleet/pilot-services/skgateway.json`
- Create `docs/fleet/pilot-services/skcomms.json`
- Create `docs/fleet/pilot-services/skchat-daemon.json`
- Create `docs/runbooks/fleet-services.md`
- Create `tests/fleet/test_cli_services.py`

Interfaces (produced):

```python
# node_controller.py
def set_actuation(paths: FleetPaths, name: str, enabled: bool, *,
                  writer: store.Writer) -> dict
# cli.py gains:
@fleet.command("apply")        # -f FILE (JSON {kind, name, labels?, spec})
@fleet.command("services")     # table from service_controller.service_rows
@fleet.command("reconcile")    # one ServiceController pass
@fleet.command("actuation")    # NAME --enable/--disable (report-only toggle)
```

Consumes: Task 5 (`service_controller.reconcile_once`, `service_rows`),
Task 1 (`services.normalize_service_spec`), real `store.write_spec`,
`store.read_spec`, `valid_name`, `default_paths`, `self_node_name`,
`_operator()`, the merged `cordon` implementation as the pattern for
`set_actuation`.

Steps:

1. Write the failing test, `tests/fleet/test_cli_services.py`:

```python
"""Tests for skfleet apply/services/reconcile/actuation + pilot spec docs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.fleet import events, node_controller, services, store
from skcapstone.fleet.cli import fleet

PILOT_DIR = Path(__file__).resolve().parents[2] / "docs" / "fleet" / "pilot-services"
PILOTS = ["skwhisper-lumina", "skgateway", "skcomms", "skchat-daemon"]


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def test_apply_writes_and_validates(paths, tmp_path) -> None:
    runner = CliRunner()
    doc = tmp_path / "svc.json"
    doc.write_text(json.dumps({"kind": "service", "name": "skgateway",
                               "labels": {"tier": "core"},
                               "spec": {"unit": "skgateway.service"}}))
    out = runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "service/skgateway" in out.output and "generation 1" in out.output
    assert store.read_spec(paths, "service", "skgateway")["labels"] == {"tier": "core"}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kind": "service", "name": "x",
                               "spec": {"runtime": "kubelet", "unit": "u"}}))
    out = runner.invoke(fleet, ["apply", "-f", str(bad)], env=_env(paths))
    assert out.exit_code != 0 and "runtime" in out.output
    assert store.read_spec(paths, "service", "x") is None       # rejected: no write


def test_apply_rejects_malformed_docs(paths, tmp_path) -> None:
    runner = CliRunner()
    doc = tmp_path / "nokind.json"
    doc.write_text(json.dumps({"name": "x", "spec": {}}))
    assert runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths)).exit_code != 0
    doc.write_text("not json")
    assert runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths)).exit_code != 0


def test_services_table_and_reconcile(paths, operator, noded41) -> None:
    runner = CliRunner()
    store.write_spec(paths, "node", "node-41", {"cordoned": False},
                     writer=operator, labels={"always-on": "true"})
    hb = store.Writer(role="sknoded", node="node-41", identity="")
    store.write_node_file(paths, hb, "heartbeat.json",
                          {"kind": "Node", "name": "node-41", "node": "node-41",
                           "ts": "2026-07-28T00:00:00Z"}, if_changed=False)
    store.write_node_file(paths, hb, "node.json",
                          {"kind": "Node", "name": "node-41", "node": "node-41",
                           "observedGeneration": 1, "conditions": [],
                           "status": {"capacity": {"cores": 8, "ram_gb": 16.0,
                                                   "disk_gb": 100.0},
                                      "allocatable": {"cores": 7, "ram_gb": 15.0,
                                                      "disk_gb": 95.0}}})
    store.write_spec(paths, "service", "skgateway",
                     {"unit": "skgateway.service",
                      "nodeSelector": {"always-on": "true"}}, writer=operator)
    out = runner.invoke(fleet, ["services"], env=_env(paths))
    assert "skgateway" in out.output and "unplaced" in out.output
    # reconcile places it; the heartbeat above is stale so node-41 is Dead,
    # therefore nothing can be placed yet and manual failover logic stays quiet
    out = runner.invoke(fleet, ["reconcile"], env=_env(paths))
    assert out.exit_code == 0
    assert "placed=0" in out.output


def test_actuation_toggle_round_trip(paths, operator) -> None:
    runner = CliRunner()
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    out = runner.invoke(fleet, ["actuation", "node-41", "--enable"], env=_env(paths))
    assert out.exit_code == 0
    assert store.read_spec(paths, "node", "node-41")["spec"]["actuate"] is True
    out = runner.invoke(fleet, ["actuation", "node-41", "--disable"], env=_env(paths))
    assert store.read_spec(paths, "node", "node-41")["spec"]["actuate"] is False
    out = runner.invoke(fleet, ["actuation", "missing-node", "--enable"], env=_env(paths))
    assert out.exit_code != 0


def test_set_actuation_preserves_other_spec_fields(paths, operator) -> None:
    store.write_spec(paths, "node", "node-41",
                     {"cordoned": True, "taints": [{"key": "travel"}]},
                     writer=operator, labels={"heavy-build": "true"})
    node_controller.set_actuation(paths, "node-41", True, writer=operator)
    spec = store.read_spec(paths, "node", "node-41")
    assert spec["spec"]["cordoned"] is True
    assert spec["spec"]["taints"] == [{"key": "travel"}]
    assert spec["labels"] == {"heavy-build": "true"}
    assert spec["spec"]["actuate"] is True


def test_pilot_docs_are_valid_and_schedulable() -> None:
    assert sorted(p.stem for p in PILOT_DIR.glob("*.json")) == sorted(PILOTS)
    for path in PILOT_DIR.glob("*.json"):
        doc = json.loads(path.read_text())
        assert doc["kind"] == "service" and doc["name"] == path.stem
        spec = services.normalize_service_spec(doc["spec"])
        assert spec["failover"] == "manual"          # pilot set: conservative
        wl = services.service_workload(doc)
        assert wl.node_selector == {"always-on": "true"}
```

2. Run to fail. Expected: click exits with
   `Error: No such command 'apply'.`

3. Implement. Append to `src/skcapstone/fleet/node_controller.py`:

```python
def set_actuation(
    paths: FleetPaths, name: str, enabled: bool, *, writer: store.Writer
) -> dict:
    """Toggle the per-node actuation opt-in (operator action, spec R4).

    Every node is born report-only; this is the single explicit lever that
    lets sknoded on that node actuate. Preserves all other spec fields.
    """
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    new_spec = dict(current.get("spec", {}), actuate=enabled)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )
```

   In `src/skcapstone/fleet/cli.py`, add
   `from . import service_controller` and `from . import services as
   services_mod` to the imports, then the four commands after
   `placements_cmd`:

```python
@fleet.command("apply")
@click.option("-f", "--file", "file_path", required=True,
              type=click.Path(exists=True, dir_okay=False))
def apply_cmd(file_path: str) -> None:
    """Write one object spec from a JSON doc {kind, name, labels?, spec}."""
    from pathlib import Path

    try:
        doc = jsonlib.loads(Path(file_path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise click.ClickException(f"not valid JSON: {exc}") from exc
    kind, name = doc.get("kind"), doc.get("name")
    if not kind or not name:
        raise click.ClickException("doc must carry 'kind' and 'name'")
    spec = doc.get("spec", {})
    if kind == "service":
        try:
            services_mod.normalize_service_spec(spec)
        except services_mod.ServiceSpecError as exc:
            raise click.ClickException(f"invalid service spec: {exc}") from exc
    try:
        payload = store.write_spec(default_paths(), kind, name, spec,
                                   writer=_operator(), labels=doc.get("labels"))
    except store.OwnershipError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"applied {kind}/{name} (generation {payload['generation']})")


@fleet.command("services")
def services_cmd() -> None:
    """List all Services with placement, observed state, and readiness."""
    rows = service_controller.service_rows(default_paths())
    if not rows:
        click.echo("no services")
        return
    for r in rows:
        flags = "".join([" PAUSED" if r.paused else "", " STALE" if r.stale else ""])
        click.echo(f"{r.name}\t-> {r.node or 'unplaced'}\t"
                   f"state={r.state}\tready={r.ready}{flags}")


@fleet.command("reconcile")
def reconcile_cmd() -> None:
    """One ServiceController pass (place-once + failover watch)."""
    out = service_controller.reconcile_once(default_paths(), node=self_node_name())
    click.echo(f"placed={len(out['placed'])} kept={len(out['kept'])} "
               f"failovers={len(out['failovers'])} alerted={len(out['alerted'])} "
               f"skipped={len(out['skipped'])}")


@fleet.command("actuation")
@click.argument("name")
@click.option("--enable/--disable", "enabled", required=True,
              help="Opt this node in or out of actuation (default is report-only).")
def actuation_cmd(name: str, enabled: bool) -> None:
    """Toggle sknoded actuation for one node (report-only by default)."""
    try:
        node_controller.set_actuation(default_paths(), name, enabled,
                                      writer=_operator())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name} actuation {'ENABLED' if enabled else 'disabled (report-only)'}")
```

   Create the pilot docs. `docs/fleet/pilot-services/skwhisper-lumina.json`:

```json
{
  "kind": "service",
  "name": "skwhisper-lumina",
  "labels": {"tier": "agents"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "skwhisper@lumina.service",
    "nodeSelector": {"always-on": "true"},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/pilot-services/skgateway.json`:

```json
{
  "kind": "service",
  "name": "skgateway",
  "labels": {"tier": "core"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "skgateway.service",
    "nodeSelector": {"always-on": "true"},
    "healthCheck": {"port": 18780},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/pilot-services/skcomms.json`:

```json
{
  "kind": "service",
  "name": "skcomms",
  "labels": {"tier": "core"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "skcomms.service",
    "nodeSelector": {"always-on": "true"},
    "healthCheck": {"port": 9384},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/pilot-services/skchat-daemon.json`:

```json
{
  "kind": "service",
  "name": "skchat-daemon",
  "labels": {"tier": "core"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "skchat-daemon.service",
    "nodeSelector": {"always-on": "true"},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   Create `docs/runbooks/fleet-services.md` with the rollout procedure
   (operator steps, not code):

```markdown
# Fleet Services rollout (Phase 3)

## Pilot rollout, in order

1. Verify unit names on the target node BEFORE applying a spec:
   `systemctl --user list-units 'sk*'`. If a unit name differs from the
   pilot doc (for example the skchat daemon unit), fix the DOC, not the
   box.
2. Apply the pilot set on the control-plane node (.158):
   `for f in docs/fleet/pilot-services/*.json; do skfleet apply -f "$f"; done`
3. Run one controller pass and inspect:
   `skfleet reconcile && skfleet services && skfleet placements --kind service`
4. Watch one full sknoded cycle in report-only (default): statuses appear,
   ZERO actuation events. This is the safety soak.
5. Opt in actuation on .158 only: `skfleet actuation node-158 --enable`.
6. Acceptance drill (Card 3.1): `systemctl --user stop skwhisper@lumina`
   and confirm it is healed within 60s; set `"paused": true` in the doc,
   re-apply, stop again, confirm NO heal; unset paused. Kill-loop drill:
   break the unit (bad ExecStart), watch backoff events (10s, 20s, 40s),
   the CrashLooping condition, and exactly one sk-alert; repair the unit.
7. Freeze drill: `skfleet freeze --reason drill`, stop a pilot unit,
   confirm no heal and services stay up; `skfleet unfreeze`, confirm heal.
8. Wire the controller tick: add an skscheduler config job on .158 running
   `skfleet reconcile` every 60s (same jobs.yaml mechanism as existing
   jobs, notify: on_failure).
9. Enable actuation on node-41 after one clean day on .158. The local box
   stays report-only until explicitly decided otherwise (R4).

## Reversal

- One service: `"paused": true` + `skfleet apply -f <doc>`.
- One node: `skfleet actuation <node> --disable` (back to report-only).
- Fleet-wide: `skfleet freeze --reason <why>` (kill-switch; services keep
  running, all actuation halts everywhere).
```

4. Run to pass, then the whole `tests/fleet/` suite, then the wiring smoke:
   `~/.skenv/bin/pip install -e /home/cbrd21/clawd/skcapstone-repos/skcapstone`
   `~/.skenv/bin/skfleet apply --help && ~/.skenv/bin/skfleet services`
5. Commit: `feat(fleet): apply/services/reconcile/actuation CLI + pilot service set (Card 3.2)`

---

## Task 7: Docker runtime verbs + converge runtime dispatch

Card: 3.3. Docker/compose-backed Services converge exactly like systemd
ones: the verb library gains docker verbs plus a runtime dispatch layer,
and converge.py switches from calling `systemd_*` directly to the dispatch
functions. Same injectable runner, same degrade-safe rules, zero real
docker calls in tests.

Files:
- Modify `src/skcapstone/fleet/actuation.py` (append docker verbs +
  dispatch)
- Modify `src/skcapstone/fleet/converge.py` (use the dispatch functions)
- Create `tests/fleet/test_docker_actuation.py`

Interfaces (produced):

```python
# actuation.py gains:
def docker_state(container: str, *, runner: Runner) -> UnitState
def docker_start(container: str, *, runner: Runner) -> bool
def docker_restart(container: str, *, runner: Runner) -> bool
def compose_up(file: str, service: str, *, runner: Runner) -> bool
def docker_logs(container: str, lines: int = 30, *, runner: Runner) -> str
# runtime dispatch (what converge calls from now on):
def state_of(spec: dict, *, runner: Runner) -> UnitState
def start(spec: dict, *, runner: Runner) -> bool
def restart(spec: dict, *, runner: Runner) -> bool
def failure_logs(spec: dict, *, runner: Runner) -> str
```

`spec` is a NORMALIZED service spec (Task 1): dispatch keys off
`spec["runtime"]`; for docker, `spec["unit"]` is the container name and a
non-null `spec["compose"]` makes `start` use `docker compose -f <file> up
-d <service>` instead of `docker start`.

Consumes: Task 1 (`normalize_service_spec` output shape), Task 2
(`UnitState`, `Runner`, the systemd verbs), Task 4 (converge call sites).

Steps:

1. Write the failing test, `tests/fleet/test_docker_actuation.py`:

```python
"""Tests for docker verbs + runtime dispatch (all runners faked)."""
from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import actuation, backoff, converge, events, store
from skcapstone.fleet.services import normalize_service_spec


class FakeRunner:
    def __init__(self, replies: dict[str, tuple[int, str, str]]) -> None:
        self.replies = dict(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out, err = self.replies.get(" ".join(cmd), (0, "", ""))
        return CompletedProcess(cmd, code, stdout=out, stderr=err)


INSPECT = ("docker inspect -f "
           "{{.State.Status}}|{{.State.Pid}}|{{.State.StartedAt}} coturn")


def test_docker_state_running_exited_missing() -> None:
    runner = FakeRunner({INSPECT: (0, "running|314|2026-07-28T00:00:00Z\n", "")})
    st = actuation.docker_state("coturn", runner=runner)
    assert st.state == "active" and st.pid == 314
    assert st.since == "2026-07-28T00:00:00Z"
    runner = FakeRunner({INSPECT: (0, "exited|0|t\n", "")})
    assert actuation.docker_state("coturn", runner=runner).state == "failed"
    runner = FakeRunner({INSPECT: (0, "restarting|0|t\n", "")})
    assert actuation.docker_state("coturn", runner=runner).state == "activating"
    runner = FakeRunner({INSPECT: (1, "", "Error: No such object: coturn")})
    assert actuation.docker_state("coturn", runner=runner).state == "missing"
    runner = FakeRunner({INSPECT: (1, "", "Cannot connect to the Docker daemon")})
    assert actuation.docker_state("coturn", runner=runner).state == "unknown"


def test_docker_verbs_and_logs() -> None:
    runner = FakeRunner({
        "docker start coturn": (0, "", ""),
        "docker restart coturn": (0, "", ""),
        "docker logs --tail 30 coturn": (0, "turn ready\n", ""),
    })
    assert actuation.docker_start("coturn", runner=runner) is True
    assert actuation.docker_restart("coturn", runner=runner) is True
    assert actuation.docker_logs("coturn", runner=runner) == "turn ready"


def test_compose_up() -> None:
    runner = FakeRunner({
        "docker compose -f /opt/coturn/compose.yml up -d coturn": (0, "", ""),
    })
    assert actuation.compose_up("/opt/coturn/compose.yml", "coturn",
                                runner=runner) is True


def test_dispatch_by_runtime() -> None:
    sysd = normalize_service_spec({"unit": "u.service"})
    dock = normalize_service_spec({"unit": "coturn", "runtime": "docker"})
    comp = normalize_service_spec({"unit": "coturn", "runtime": "docker",
                                   "compose": {"file": "/opt/c.yml",
                                               "service": "coturn"}})
    runner = FakeRunner({
        "systemctl --user start u.service": (0, "", ""),
        "docker start coturn": (0, "", ""),
        "docker compose -f /opt/c.yml up -d coturn": (0, "", ""),
    })
    assert actuation.start(sysd, runner=runner) is True
    assert actuation.start(dock, runner=runner) is True
    assert actuation.start(comp, runner=runner) is True
    assert [" ".join(c) for c in runner.calls] == [
        "systemctl --user start u.service",
        "docker start coturn",
        "docker compose -f /opt/c.yml up -d coturn",
    ]


def test_docker_service_converges_like_systemd(paths, operator, scheduler_writer) -> None:
    events.reset_dedupe()
    backoff.reset_trackers()
    store.write_spec(paths, "node", "node-41", {"actuate": True}, writer=operator)
    store.write_spec(paths, "service", "coturn",
                     {"unit": "coturn", "runtime": "docker"}, writer=operator)
    store.write_placement(paths, "service", "coturn", node="node-41",
                          reason="pinned", writer=scheduler_writer)
    runner = FakeRunner({
        ("docker inspect -f {{.State.Status}}|{{.State.Pid}}|"
         "{{.State.StartedAt}} coturn"): (0, "exited|0|t\n", ""),
        "docker logs --tail 30 coturn": (0, "bye\n", ""),
        "docker restart coturn": (0, "", ""),
    })
    out = converge.converge_once(paths, "node-41", runner=runner, now=1000.0)
    assert out["services"]["coturn"]["acted"] == "healed"
    assert ["docker", "restart", "coturn"] in runner.calls
    st = store.read_status(paths, "service", "coturn", "node-41")
    assert st["status"]["runtime"] == "docker"
    events.reset_dedupe()
    backoff.reset_trackers()
```

2. Run to fail. Expected:
   `AttributeError: module 'skcapstone.fleet.actuation' has no attribute 'docker_state'`

3. Implement. Append to `src/skcapstone/fleet/actuation.py`:

```python
def docker_state(container: str, *, runner: Runner) -> UnitState:
    """Observe one container. Degrades to unknown, never raises."""
    try:
        out = runner(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.Pid}}|{{.State.StartedAt}}",
                container,
            ]
        )
    except Exception:
        return _UNKNOWN
    if out.returncode != 0:
        if "No such object" in (out.stderr or ""):
            return UnitState(state="missing", pid=None, since="")
        return _UNKNOWN
    parts = (out.stdout or "").strip().split("|")
    if len(parts) != 3:
        return _UNKNOWN
    status, pid_raw, since = parts
    mapping = {
        "running": "active",
        "restarting": "activating",
        "created": "inactive",
        "paused": "inactive",
        "exited": "failed",
        "dead": "failed",
    }
    try:
        pid = int(pid_raw) or None
    except ValueError:
        pid = None
    return UnitState(state=mapping.get(status, "unknown"), pid=pid, since=since)


def docker_start(container: str, *, runner: Runner) -> bool:
    """Start a container. True on rc=0 (never raises)."""
    return _verb(["docker", "start", container], runner)


def docker_restart(container: str, *, runner: Runner) -> bool:
    """Restart a container. True on rc=0 (never raises)."""
    return _verb(["docker", "restart", container], runner)


def compose_up(file: str, service: str, *, runner: Runner) -> bool:
    """Bring one compose service up detached. True on rc=0 (never raises)."""
    return _verb(["docker", "compose", "-f", file, "up", "-d", service], runner)


def docker_logs(container: str, lines: int = 30, *, runner: Runner) -> str:
    """Tail container logs (logs-on-failure verb). "" when unavailable."""
    try:
        out = runner(["docker", "logs", "--tail", str(lines), container])
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def state_of(spec: dict, *, runner: Runner) -> UnitState:
    """Observe a normalized service spec's unit, whatever its runtime."""
    if spec["runtime"] == "docker":
        return docker_state(spec["unit"], runner=runner)
    return systemd_state(spec["unit"], runner=runner)


def start(spec: dict, *, runner: Runner) -> bool:
    """Start per runtime; compose-backed docker services use compose up."""
    if spec["runtime"] == "docker":
        if spec.get("compose"):
            return compose_up(spec["compose"]["file"], spec["compose"]["service"],
                              runner=runner)
        return docker_start(spec["unit"], runner=runner)
    return systemd_start(spec["unit"], runner=runner)


def restart(spec: dict, *, runner: Runner) -> bool:
    """Restart per runtime."""
    if spec["runtime"] == "docker":
        return docker_restart(spec["unit"], runner=runner)
    return systemd_restart(spec["unit"], runner=runner)


def failure_logs(spec: dict, *, runner: Runner) -> str:
    """Logs-on-failure per runtime."""
    if spec["runtime"] == "docker":
        return docker_logs(spec["unit"], runner=runner)
    return systemd_logs(spec["unit"], runner=runner)
```

   In `src/skcapstone/fleet/converge.py`, switch the three call sites to
   the dispatch layer. In `_heal` replace
   `actuation.systemd_logs(spec["unit"], runner=runner)` with
   `actuation.failure_logs(spec, runner=runner)`,
   `actuation.systemd_restart(spec["unit"], runner=runner)` with
   `actuation.restart(spec, runner=runner)`, and
   `actuation.systemd_start(spec["unit"], runner=runner)` with
   `actuation.start(spec, runner=runner)`. In `converge_service` replace
   `actuation.systemd_state(spec["unit"], runner=runner)` with
   `actuation.state_of(spec, runner=runner)`.

4. Run to pass, then the whole `tests/fleet/` suite (the Task 4 converge
   tests exercise the systemd branch of the dispatch and stay green).
5. Commit: `feat(fleet): docker runtime verbs + converge runtime dispatch (Card 3.3)`

---

## Task 8: skfleet drain (cordon + resident inventory + alert)

Card: 3.3. v1 drain is deliberately small (spec Card 3.3 + R4): cordon the
node so nothing new lands there, enumerate what runs or is placed there,
and alert with the list. Moving residents is MANUAL in v1 (the conservative
failover default extends to drains); the command says so explicitly.

Files:
- Modify `src/skcapstone/fleet/service_controller.py` (append
  `node_residents`)
- Modify `src/skcapstone/fleet/cli.py` (add the `drain` command)
- Create `tests/fleet/test_drain.py`

Interfaces (produced):

```python
# service_controller.py gains:
def node_residents(paths: FleetPaths, node: str) -> list[dict]
# [{"name": str, "via": "placement"|"status", "state": str}, ...] sorted,
# deduped by name (placement wins the "via" label)
# cli.py gains:
@fleet.command("drain")        # NAME
```

Consumes: real `store.list_placements`, `store.read_status`,
`paths.node_status_dir`, `node_controller.cordon`, `alerts.send_alert`
(Task 3), `_operator()`.

Steps:

1. Write the failing test, `tests/fleet/test_drain.py`:

```python
"""Tests for skfleet drain: cordon + resident listing + alert, no moves."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from skcapstone.fleet import alerts, service_controller, store
from skcapstone.fleet.cli import fleet


def _populate(paths, operator, scheduler_writer, noded41) -> None:
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    store.write_spec(paths, "service", "skgateway", {"unit": "u1.service"},
                     writer=operator)
    store.write_spec(paths, "service", "coturn",
                     {"unit": "coturn", "runtime": "docker"}, writer=operator)
    store.write_placement(paths, "service", "skgateway", node="node-41",
                          reason="r", writer=scheduler_writer)
    store.write_placement(paths, "service", "coturn", node="node-158",
                          reason="r", writer=scheduler_writer)
    # an observed-but-unplaced-here service (manual legacy resident)
    store.write_status(paths, "service", "legacy", node="node-41",
                       status={"state": "active", "pid": 9, "since": "t",
                               "restarts": 0, "runtime": "systemd-user"},
                       conditions=[], observed_generation=0, writer=noded41)


def test_node_residents_merges_placements_and_statuses(
        paths, operator, scheduler_writer, noded41) -> None:
    _populate(paths, operator, scheduler_writer, noded41)
    residents = service_controller.node_residents(paths, "node-41")
    assert [(r["name"], r["via"]) for r in residents] == [
        ("legacy", "status"), ("skgateway", "placement")]
    assert residents[0]["state"] == "active"
    assert service_controller.node_residents(paths, "node-158") == [
        {"name": "coturn", "via": "placement", "state": "unobserved"}]


def test_drain_cordons_lists_and_alerts(paths, operator, scheduler_writer,
                                        noded41, monkeypatch) -> None:
    _populate(paths, operator, scheduler_writer, noded41)
    alerted: list[str] = []
    monkeypatch.setattr(alerts, "send_alert",
                        lambda msg, **kw: alerted.append(msg) or True)
    runner = CliRunner()
    out = runner.invoke(fleet, ["drain", "node-41"],
                        env={"SKFLEET_ROOT": str(paths.root),
                             "SKFLEET_NODE": "node-158"})
    assert out.exit_code == 0, out.output
    assert store.read_spec(paths, "node", "node-41")["spec"]["cordoned"] is True
    assert "skgateway" in out.output and "legacy" in out.output
    assert "manual move" in out.output.lower()
    assert alerted and "node-41" in alerted[0] and "skgateway" in alerted[0]
    # placements were NOT touched: drain never moves anything in v1
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"


def test_drain_unknown_node_fails_cleanly(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["drain", "node-nope"],
                        env={"SKFLEET_ROOT": str(paths.root),
                             "SKFLEET_NODE": "node-158"})
    assert out.exit_code != 0 and "no such node" in out.output
```

2. Run to fail. Expected:
   `AttributeError: module 'skcapstone.fleet.service_controller' has no attribute 'node_residents'`

3. Implement. Append to `src/skcapstone/fleet/service_controller.py`:

```python
def node_residents(paths: FleetPaths, node: str) -> list[dict]:
    """Services placed on or observed on one node (the drain inventory).

    Placements are desired state; observed statuses catch legacy residents
    that predate fleet management. Deduped by name, placement wins.
    """
    residents: dict[str, dict] = {}
    service_dir = paths.node_status_dir(node) / "service"
    if service_dir.exists():
        for status_file in sorted(service_dir.glob("*.json")):
            name = status_file.stem
            st = store.read_status(paths, "service", name, node)
            state = str((st or {}).get("status", {}).get("state", "unknown"))
            residents[name] = {"name": name, "via": "status", "state": state}
    for placement in store.list_placements(paths, "service"):
        if placement.get("node") != node:
            continue
        name = placement["name"]
        st = store.read_status(paths, "service", name, node)
        state = str((st or {}).get("status", {}).get("state", "unobserved"))
        residents[name] = {"name": name, "via": "placement", "state": state}
    return [residents[k] for k in sorted(residents)]
```

   In `src/skcapstone/fleet/cli.py`, add `from . import alerts` to the
   imports and the command after `uncordon_cmd`:

```python
@fleet.command("drain")
@click.argument("name")
def drain_cmd(name: str) -> None:
    """Cordon a node and alert with its residents (manual move in v1)."""
    paths_ = default_paths()
    residents = service_controller.node_residents(paths_, name)
    try:
        node_controller.cordon(paths_, name, True, writer=_operator())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    names = ", ".join(r["name"] for r in residents) or "none"
    alerts.send_alert(
        f"fleet: drain {name}: cordoned; residents: {names}; "
        f"move them manually (v1 drains never auto-move)", level="warn")
    click.echo(f"{name} cordoned (drain)")
    for r in residents:
        click.echo(f"  resident: {r['name']}\tvia={r['via']}\tstate={r['state']}")
    click.echo("manual move required in v1: re-place or migrate each resident, "
               "then uncordon")
```

4. Run to pass, then the whole `tests/fleet/` suite.
5. Commit: `feat(fleet): skfleet drain = cordon + resident inventory + alert (Card 3.3)`

---

## Task 9: Onboard remaining services + skmem-pg health probe (Card 3.4)

Card: 3.4. Two halves. (a) Spec docs for the remaining long-running
services (skmemory daemon, ollama, piper-tts, nostr relay), applied by
runbook like the pilots. (b) skmem-pg stays OUT of fleet management by
prior incident decision (spec 5.2): it gets a HEALTH CONDITION only, via a
new operator-declared `healthProbes` list on the Node spec that sknoded
turns into node conditions (a TCP probe, no actuation path at all).

Files:
- Modify `src/skcapstone/fleet/conditions.py` (append `tcp_open`,
  `probe_conditions`)
- Modify `src/skcapstone/fleet/sknoded.py` (build_node_report appends
  probe conditions from the node spec)
- Create `docs/fleet/services/skmemory-daemon.json`
- Create `docs/fleet/services/ollama.json`
- Create `docs/fleet/services/piper-tts.json`
- Create `docs/fleet/services/nostr-relay.json`
- Modify `docs/runbooks/fleet-services.md` (onboarding section)
- Create `tests/fleet/test_onboarding.py`

Interfaces (produced):

```python
# conditions.py gains:
def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool
def probe_conditions(probes: list[dict], now_iso: str) -> list[dict]
# probes: [{"name": str, "port": int, "condition": str}, ...]
```

Consumes: real `conditions.node_conditions` and `merge_transitions`
shapes, real `sknoded.build_node_report` (already reads the node spec for
`observedGeneration`, so the probes list is one more read of the same
payload), Task 1 (`normalize_service_spec` for doc validation tests).

Steps:

1. Write the failing test, `tests/fleet/test_onboarding.py`:

```python
"""Tests for Card 3.4: onboarding docs + skmem-pg health-condition-only."""
from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet import conditions, services, sknoded, store

DOCS = Path(__file__).resolve().parents[2] / "docs" / "fleet" / "services"
ONBOARD = ["skmemory-daemon", "ollama", "piper-tts", "nostr-relay"]
NOW = "2026-07-28T12:00:00Z"
CAP = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None}


def test_onboarding_docs_valid_and_conservative() -> None:
    assert sorted(p.stem for p in DOCS.glob("*.json")) == sorted(ONBOARD)
    for path in DOCS.glob("*.json"):
        doc = json.loads(path.read_text())
        spec = services.normalize_service_spec(doc["spec"])
        assert spec["failover"] == "manual"


def test_ollama_targets_the_gpu_node_with_toleration() -> None:
    doc = json.loads((DOCS / "ollama.json").read_text())
    wl = services.service_workload(doc)
    assert wl.node_selector == {"gpu": "true"}
    assert {"key": "dedicated", "value": "model-serving"} in wl.tolerations


def test_skmem_pg_is_never_a_service() -> None:
    for base in (DOCS, DOCS.parent / "pilot-services"):
        for path in base.glob("*.json"):
            assert "skmem-pg" not in path.stem
            assert "skmem-pg" not in path.read_text()


def test_probe_conditions(monkeypatch) -> None:
    monkeypatch.setattr(conditions, "tcp_open",
                        lambda port, host="127.0.0.1", timeout=1.0: port == 5432)
    conds = conditions.probe_conditions(
        [{"name": "skmem-pg", "port": 5432, "condition": "SkmemPgReady"},
         {"name": "dead-thing", "port": 9999, "condition": "DeadThingReady"}], NOW)
    by_type = {c["type"]: c for c in conds}
    assert by_type["SkmemPgReady"]["status"] == "True"
    assert by_type["SkmemPgReady"]["reason"] == "TcpProbe"
    assert by_type["DeadThingReady"]["status"] == "False"
    assert conditions.probe_conditions([], NOW) == []


def test_node_report_carries_probe_conditions(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    monkeypatch.setattr(conditions, "tcp_open",
                        lambda port, host="127.0.0.1", timeout=1.0: True)
    store.write_spec(paths, "node", "node-41",
                     {"healthProbes": [{"name": "skmem-pg", "port": 5432,
                                        "condition": "SkmemPgReady"}]},
                     writer=operator)
    sknoded.run_once(paths, "node-41")
    report = store.read_node_file(paths, "node-41", "node.json")
    by_type = {c["type"]: c for c in report["conditions"]}
    assert by_type["SkmemPgReady"]["status"] == "True"


def test_no_probes_means_no_extra_conditions(paths, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")               # unadmitted: no node spec
    report = store.read_node_file(paths, "node-41", "node.json")
    types = {c["type"] for c in report["conditions"]}
    assert "SkmemPgReady" not in types
```

2. Run to fail. Expected:
   `AttributeError: module 'skcapstone.fleet.conditions' has no attribute 'probe_conditions'`

3. Implement. Append to `src/skcapstone/fleet/conditions.py`:

```python
def tcp_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """True when a TCP connect to host:port succeeds (health probe)."""
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def probe_conditions(probes: list[dict], now_iso: str) -> list[dict]:
    """Operator-declared TCP probes as node conditions (spec 5.2 skmem-pg rule).

    This is the health-condition-only surface for workloads that stay OUT
    of fleet management (skmem-pg is local-per-node by incident decision):
    visibility, never actuation.
    """
    out: list[dict] = []
    for probe in probes:
        try:
            port = int(probe["port"])
            cond_type = str(probe["condition"])
        except (KeyError, TypeError, ValueError):
            continue
        ok = tcp_open(port)
        out.append(
            _cond(cond_type, ok, "TcpProbe",
                  f"{probe.get('name', cond_type)} port {port} "
                  f"{'open' if ok else 'closed'}", now_iso)
        )
    return out
```

   In `src/skcapstone/fleet/sknoded.py`, inside `build_node_report`, the
   spec is already loaded (`spec = store.read_spec(paths, "node", node)`).
   Change the conditions block from

```python
    cap = node_capacity()
    conds = node_conditions(cap, paths.root, now_iso)
    previous = store.read_node_file(paths, node, "node.json") or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
    spec = store.read_spec(paths, "node", node)
```

   to

```python
    cap = node_capacity()
    spec = store.read_spec(paths, "node", node)
    conds = node_conditions(cap, paths.root, now_iso)
    from .conditions import probe_conditions

    probes = (spec or {}).get("spec", {}).get("healthProbes", [])
    conds.extend(probe_conditions(probes, now_iso))
    previous = store.read_node_file(paths, node, "node.json") or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
```

   (the later duplicate `spec = store.read_spec(...)` line is removed;
   `merge_transitions` runs LAST so probe conditions also keep stable
   `lastTransition` timestamps and stay write-on-change friendly).

   Create the onboarding docs. `docs/fleet/services/skmemory-daemon.json`:

```json
{
  "kind": "service",
  "name": "skmemory-daemon",
  "labels": {"tier": "core"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "skmemory-daemon.service",
    "nodeSelector": {"always-on": "true"},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/services/ollama.json`:

```json
{
  "kind": "service",
  "name": "ollama",
  "labels": {"tier": "models"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "ollama.service",
    "nodeSelector": {"gpu": "true"},
    "tolerations": [{"key": "dedicated", "value": "model-serving"}],
    "healthCheck": {"port": 11434},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/services/piper-tts.json`:

```json
{
  "kind": "service",
  "name": "piper-tts",
  "labels": {"tier": "media"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "piper-tts.service",
    "nodeSelector": {"always-on": "true"},
    "healthCheck": {"port": 18797},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   `docs/fleet/services/nostr-relay.json`:

```json
{
  "kind": "service",
  "name": "nostr-relay",
  "labels": {"tier": "comms"},
  "spec": {
    "runtime": "systemd-user",
    "unit": "nostr-relay.service",
    "nodeSelector": {"always-on": "true"},
    "healthCheck": {"port": 7447},
    "restartPolicy": "on-failure",
    "failover": "manual"
  }
}
```

   Append to `docs/runbooks/fleet-services.md`:

```markdown
## Onboarding wave 2 (Card 3.4)

1. Per service in `docs/fleet/services/`: verify the REAL unit name on the
   target node first (`systemctl --user list-units | grep -i <name>`); fix
   the doc if it differs, then `skfleet apply -f <doc>` and
   `skfleet reconcile`.
2. skmem-pg is EXCLUDED from fleet management (local-per-node by incident
   decision). Instead, declare the health probe on each node that runs it:
   add `"healthProbes": [{"name": "skmem-pg", "port": 5432, "condition":
   "SkmemPgReady"}]` to the node spec (via `skfleet apply` on a node doc).
   `SkmemPgReady=False` in `skfleet describe node <n>` is the alarm
   surface; nothing ever actuates skmem-pg.
3. Retire hand-run deploys: after one clean week, per-box `systemctl
   --user restart <unit>` habits are replaced by editing the Service doc
   and `skfleet apply` (heal is automatic); update any personal runbooks
   that mention direct systemctl for the onboarded set.
4. Acceptance (spec Card 3.4): `skfleet services` is a complete, truthful
   map of long-running fleet workloads; one full week with zero manual
   restart interventions on the onboarded set, or each intervention is
   carded as a bug.
5. R2 gate (spec Card 3.2 acceptance, checked here at full width): with
   all services onboarded, re-measure Syncthing item churn against the
   Phase 1 baseline; per-unit status files must be write-on-change quiet
   when the fleet is stable.
```

4. Run to pass, then the whole `tests/fleet/` suite (the Phase 1 sknoded
   write-on-change tests stay green: no node spec means no probes, and
   probe results are deterministic under the monkeypatched prober).
5. Commit: `feat(fleet): onboard remaining services + healthProbes node conditions (skmem-pg excluded)`

---

## Task 10: Signing primitives + signed spec/placement writes (signing.py)

Card: 3.5, part 1 of 2. Fills the Phase 1 writer-identity seam: canonical
payload bytes, a detached signature stored in the existing
`writer.signature` slot (Phase 1 wrote `None` there from day one), and
automatic signing inside `write_spec` / `write_placement` so flipping the
mode is a config change with no call-site migration. capauth wiring is a
lazy soft dependency behind factory functions; tests use fake
signer/verifier callables and never touch real keys.

Mode flag: `SKFLEET_SIGNING` env var, `off` (default) | `permissive` |
`enforce`. Trust roster: armored public keys under
`<capauth_home>/fleet-trust/*.asc` plus the node's own
`<capauth_home>/identity/public.asc` (capauth's existing key store is the
key SOURCE; the fleet-trust directory is local, never inside the synced
fleet tree, installed by the key ceremony runbook).

Files:
- Create `src/skcapstone/fleet/signing.py`
- Modify `src/skcapstone/fleet/store.py` (`write_spec` and
  `write_placement` gain `signer=None` and auto-sign)
- Create `tests/fleet/test_signing.py`

Interfaces (produced):

```python
MODES: frozenset = frozenset({"off", "permissive", "enforce"})
SIGNING_ENV: str = "SKFLEET_SIGNING"
def signing_mode() -> str
def canonical_bytes(payload: dict) -> bytes
def verify_payload(payload: dict, verifier: Callable[[bytes, str], bool]) -> tuple[str, str]
# -> (status in {"verified","unsigned","invalid"}, detail)
def default_signer() -> Callable[[bytes], str] | None    # None when mode off / no key
def capauth_signer() -> Callable[[bytes], str] | None
def load_roster() -> list[str]
def capauth_verifier() -> Callable[[bytes, str], bool] | None

# store.py (changed, backward compatible)
def write_spec(paths, kind, name, spec, *, writer, labels=None,
               signer=None) -> dict
def write_placement(paths, kind, name, *, node, reason, writer,
                    signer=None) -> tuple[dict, bool]
```

Consumes: the merged `_writer_block` (already carries `signature: None`),
`_dump`, `_load`; capauth lazily (`resolve_capauth_home`,
`crypto.get_backend().sign/verify`, key files
`identity/private.asc` + `identity/public.asc`, passphrase from the
`CAPAUTH_PASSPHRASE` env var, empty default).

Steps:

1. Write the failing test, `tests/fleet/test_signing.py`:

```python
"""Tests for Card 3.5 signing primitives + signed store writes."""
from __future__ import annotations

import hashlib

import pytest

from skcapstone.fleet import signing, store


def fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


def test_mode_flag(monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    assert signing.signing_mode() == "off"
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    assert signing.signing_mode() == "permissive"
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    assert signing.signing_mode() == "enforce"
    monkeypatch.setenv(signing.SIGNING_ENV, "garbage")
    assert signing.signing_mode() == "off"           # unknown value fails open


def test_canonical_bytes_ignore_only_the_signature() -> None:
    a = {"kind": "Service", "name": "s", "generation": 1,
         "writer": {"role": "operator", "node": "n", "identity": "i",
                    "signature": None}}
    b = {**a, "writer": {**a["writer"], "signature": "sig:abc"}}
    assert signing.canonical_bytes(a) == signing.canonical_bytes(b)
    c = {**a, "generation": 2}
    assert signing.canonical_bytes(a) != signing.canonical_bytes(c)


def test_verify_payload_states() -> None:
    payload = {"kind": "Service", "name": "s",
               "writer": {"role": "operator", "node": "n", "identity": "i",
                          "signature": None}}
    assert signing.verify_payload(payload, fake_verifier)[0] == "unsigned"
    signed = dict(payload)
    signed["writer"] = dict(payload["writer"],
                            signature=fake_signer(signing.canonical_bytes(payload)))
    assert signing.verify_payload(signed, fake_verifier)[0] == "verified"
    tampered = dict(signed, name="evil")
    assert signing.verify_payload(tampered, fake_verifier)[0] == "invalid"

    def boom(data: bytes, sig: str) -> bool:
        raise RuntimeError("backend exploded")

    assert signing.verify_payload(signed, boom)[0] == "invalid"


def test_write_spec_signs_with_explicit_signer(paths, operator) -> None:
    payload = store.write_spec(paths, "service", "skgateway",
                               {"unit": "u.service"}, writer=operator,
                               signer=fake_signer)
    assert payload["writer"]["signature"].startswith("sig:")
    on_disk = store.read_spec(paths, "service", "skgateway")
    assert signing.verify_payload(on_disk, fake_verifier)[0] == "verified"


def test_write_placement_signs_with_explicit_signer(paths, scheduler_writer) -> None:
    payload, changed = store.write_placement(paths, "service", "skgateway",
                                             node="node-41", reason="r",
                                             writer=scheduler_writer,
                                             signer=fake_signer)
    assert changed is True
    on_disk = store.read_placement(paths, "service", "skgateway")
    assert signing.verify_payload(on_disk, fake_verifier)[0] == "verified"


def test_writes_stay_unsigned_when_mode_off(paths, operator, monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    assert payload["writer"]["signature"] is None    # exact Phase 1 behavior


def test_auto_sign_via_default_signer(paths, operator, monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    monkeypatch.setattr(signing, "capauth_signer", lambda: fake_signer)
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    assert payload["writer"]["signature"].startswith("sig:")
    assert signing.verify_payload(store.read_spec(paths, "node", "node-41"),
                                  fake_verifier)[0] == "verified"


def test_default_signer_none_when_key_missing(paths, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    monkeypatch.setenv("CAPAUTH_HOME", str(tmp_path / "empty-capauth"))
    assert signing.default_signer() is None          # no key material: no signer
    assert signing.capauth_verifier() is None        # no roster: no verifier


def test_load_roster_reads_local_key_files(monkeypatch, tmp_path) -> None:
    home = tmp_path / "capauth"
    (home / "identity").mkdir(parents=True)
    (home / "identity" / "public.asc").write_text("KEY-SELF")
    (home / "fleet-trust").mkdir()
    (home / "fleet-trust" / "chef.asc").write_text("KEY-CHEF")
    monkeypatch.setenv("CAPAUTH_HOME", str(home))
    assert sorted(signing.load_roster()) == ["KEY-CHEF", "KEY-SELF"]
```

2. Run to fail. Expected:
   `ImportError: cannot import name 'signing' from 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/signing.py`:

```python
"""Signed desired state (Card 3.5, R6): the actuation trust boundary.

Spec and placement writes carry a detached capauth/PGP signature over
canonical payload bytes in the writer.signature slot the Phase 1 store
reserved. sknoded verifies before actuating (Task 11). Rollout is
permissive-then-enforce behind the SKFLEET_SIGNING env flag; off is the
default, so Phase 1/2 behavior is unchanged until the key ceremony.

capauth is a lazy soft dependency: every factory degrades to None instead
of raising, and callers treat None per mode (off ignores it, enforce
fails safe by refusing actuation, never by stopping running services).
"""

from __future__ import annotations

import json
import os
from typing import Callable

MODES = frozenset({"off", "permissive", "enforce"})
SIGNING_ENV = "SKFLEET_SIGNING"


def signing_mode() -> str:
    """The rollout mode: off (default) | permissive | enforce."""
    mode = os.environ.get(SIGNING_ENV, "off").strip().lower()
    return mode if mode in MODES else "off"


def canonical_bytes(payload: dict) -> bytes:
    """Deterministic bytes of a payload with its signature slot blanked.

    The signature covers everything else in the file, including
    generation and updatedAt, so replaying an old signed spec over a
    newer one is detectable as invalid.
    """
    body = json.loads(json.dumps(payload, sort_keys=True))
    writer = dict(body.get("writer") or {})
    writer["signature"] = None
    body["writer"] = writer
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_payload(
    payload: dict, verifier: Callable[[bytes, str], bool]
) -> tuple[str, str]:
    """Classify one payload: verified, unsigned, or invalid (with detail)."""
    signature = (payload.get("writer") or {}).get("signature")
    if not signature:
        return ("unsigned", "no signature in writer block")
    try:
        ok = verifier(canonical_bytes(payload), signature)
    except Exception as exc:
        return ("invalid", f"verifier error: {exc}")
    if ok:
        return ("verified", "signature matches a trusted key")
    return ("invalid", "signature does not match any trusted key")


def capauth_signer() -> Callable[[bytes], str] | None:
    """A signer over this seat's capauth identity key, or None.

    Reads <capauth_home>/identity/private.asc; passphrase from the
    CAPAUTH_PASSPHRASE env var (empty default). Any failure returns None:
    signing is best-effort at write time, and enforcement lives at the
    actuation boundary, not here.
    """
    try:
        from capauth import resolve_capauth_home
        from capauth.crypto import get_backend

        key_path = resolve_capauth_home() / "identity" / "private.asc"
        if not key_path.exists():
            return None
        armor = key_path.read_text(encoding="utf-8")
        passphrase = os.environ.get("CAPAUTH_PASSPHRASE", "")
        backend = get_backend()

        def _sign(data: bytes) -> str:
            return backend.sign(data, armor, passphrase)

        return _sign
    except Exception:
        return None


def default_signer() -> Callable[[bytes], str] | None:
    """The signer store writes use when none is passed: None while off."""
    if signing_mode() == "off":
        return None
    return capauth_signer()


def load_roster() -> list[str]:
    """Trusted writer public keys (armored) from the LOCAL capauth home.

    <capauth_home>/identity/public.asc (this seat) plus every *.asc under
    <capauth_home>/fleet-trust/ (installed by the key ceremony runbook).
    Never read from the synced fleet tree: the roster must not be
    writable by the thing it authenticates.
    """
    keys: list[str] = []
    try:
        from capauth import resolve_capauth_home

        home = resolve_capauth_home()
    except Exception:
        return keys
    own = home / "identity" / "public.asc"
    if own.exists():
        keys.append(own.read_text(encoding="utf-8"))
    trust_dir = home / "fleet-trust"
    if trust_dir.exists():
        for path in sorted(trust_dir.glob("*.asc")):
            keys.append(path.read_text(encoding="utf-8"))
    return keys


def capauth_verifier() -> Callable[[bytes, str], bool] | None:
    """A verifier over the local trust roster, or None when empty."""
    roster = load_roster()
    if not roster:
        return None
    try:
        from capauth.crypto import get_backend

        backend = get_backend()
    except Exception:
        return None

    def _verify(data: bytes, signature: str) -> bool:
        for key in roster:
            try:
                if backend.verify(data, signature, key):
                    return True
            except Exception:
                continue
        return False

    return _verify
```

   In `src/skcapstone/fleet/store.py`, add one helper above `write_spec`:

```python
def _maybe_sign(payload: dict, signer) -> dict:
    """Fill writer.signature via the given or default signer (Card 3.5)."""
    from .signing import canonical_bytes, default_signer

    sign = signer if signer is not None else default_signer()
    if sign is not None:
        try:
            payload["writer"]["signature"] = sign(canonical_bytes(payload))
        except Exception:
            payload["writer"]["signature"] = None
    return payload
```

   Change the `write_spec` signature to
   `def write_spec(paths, kind, name, spec, *, writer, labels=None,
   signer=None) -> dict:` and, immediately before its `_dump(path,
   payload)` line, insert `payload = _maybe_sign(payload, signer)`.
   Change `write_placement` to
   `def write_placement(paths, kind, name, *, node, reason, writer,
   signer=None) -> tuple[dict, bool]:` and insert the same
   `payload = _maybe_sign(payload, signer)` before its `_dump`. (The
   write-on-change early return stays FIRST: an unchanged decision is not
   re-signed; the signature refreshes on the next content change, noted in
   the self-review.)

4. Run to pass, then the whole `tests/fleet/` suite (with `SKFLEET_SIGNING`
   unset every existing test writes unsigned payloads, byte-identical to
   Phase 1/2 behavior).
5. Commit: `feat(fleet): signing primitives + signed spec/placement writes (Card 3.5, seam fill)`

---

## Task 11: sknoded verification gate (permissive-then-enforce)

Card: 3.5, part 2 of 2. The actuation trust boundary: before any verb for
a service, sknoded classifies the service SPEC and its PLACEMENT (the two
files actuation consumes). `off` skips classification entirely. In
`permissive`, an unverified pair actuates but emits a deduped
`SpecUnverified` event and condition (the audit soak). In `enforce`, an
unverified pair gets NO verbs (probe and status continue, running services
are untouched), the `SpecUnverified` condition is raised, and one deduped
sk-alert fires. A missing verifier under enforce counts as unverified:
fail safe means refuse new actuation, never stop anything.

Files:
- Modify `src/skcapstone/fleet/converge.py` (verification gate)
- Modify `docs/runbooks/fleet-services.md` (key ceremony + rollout flip)
- Create `tests/fleet/test_converge_signing.py`

Interfaces (produced/changed):

```python
# converge.py
def verify_desired(spec_payload: dict, placement: dict | None,
                   verifier: Callable[[bytes, str], bool] | None) -> tuple[bool, str]
def converge_once(paths, node, *, runner=None, prober=None, now=None,
                  verifier=None) -> dict
# converge_service gains: sig_mode: str = "off",
#                         verification: tuple[bool, str] = (True, "")
```

Consumes: Task 10 (`signing.signing_mode`, `signing.verify_payload`,
`signing.capauth_verifier`), Task 4 (converge structure, `_cond`,
`events.emit` + alert-on-append pattern).

Steps:

1. Write the failing test, `tests/fleet/test_converge_signing.py`:

```python
"""Tests for the sknoded verification gate (permissive-then-enforce)."""
from __future__ import annotations

import hashlib
from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import backoff, converge, events, signing, store

NODE = "node-41"
SHOW = ("systemctl --user show skgateway.service "
        "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp")
FAILED = (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n"
             "ActiveEnterTimestamp=\n")


def fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self, replies) -> None:
        self.replies = dict(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out = self.replies.get(" ".join(cmd), (0, ""))
        return CompletedProcess(cmd, code, stdout=out, stderr="")

    def verbs(self) -> list[str]:
        return [" ".join(c) for c in self.calls
                if c[:2] == ["systemctl", "--user"] and c[2] in ("start", "restart")]


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    backoff.reset_trackers()
    yield
    events.reset_dedupe()
    backoff.reset_trackers()


def _fleet(paths, operator, scheduler_writer, *, signed: bool) -> None:
    signer = fake_signer if signed else None
    store.write_spec(paths, "node", NODE, {"actuate": True}, writer=operator,
                     signer=signer)
    store.write_spec(paths, "service", "skgateway", {"unit": "skgateway.service"},
                     writer=operator, signer=signer)
    store.write_placement(paths, "service", "skgateway", node=NODE,
                          reason="pinned", writer=scheduler_writer, signer=signer)


def _runner() -> FakeRunner:
    return FakeRunner({
        SHOW: FAILED,
        "systemctl --user restart skgateway.service": (0, ""),
        "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
    })


def _unverified_cond(paths):
    st = store.read_status(paths, "service", "skgateway", NODE)
    return {c["type"]: c for c in st["conditions"]}.get("SpecUnverified")


def test_mode_off_ignores_signatures(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                           verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _unverified_cond(paths) is None           # no condition in off mode


def test_permissive_warns_but_actuates(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                           verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    cond = _unverified_cond(paths)
    assert cond["status"] == "True"
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    assert any(e["reason"] == "SpecUnverified" for e in logged)


def test_enforce_refuses_unsigned_and_alerts(paths, operator, scheduler_writer,
                                             monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    alerted: list[str] = []
    monkeypatch.setattr(converge.alerts, "send_alert",
                        lambda msg, **kw: alerted.append(msg) or True)
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                                 verifier=fake_verifier)
    assert runner.verbs() == []                      # refused: no new actuation
    assert out["services"]["skgateway"]["acted"] == "unverified"
    assert _unverified_cond(paths)["status"] == "True"
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "failed"         # probing continues
    assert len(alerted) == 1                         # one deduped alert
    converge.converge_once(paths, NODE, runner=runner, now=1030.0,
                           verifier=fake_verifier)
    assert len(alerted) == 1                         # dedupe window holds


def test_enforce_actuates_when_properly_signed(paths, operator, scheduler_writer,
                                               monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                           verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _unverified_cond(paths)["status"] == "False"


def test_enforce_refuses_tampered_spec(paths, operator, scheduler_writer,
                                       monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, signed=True)
    signed = store.read_spec(paths, "service", "skgateway")
    signed["spec"]["unit"] = "evil.service"          # tamper AFTER signing
    import json

    paths.spec_path("service", "skgateway").write_text(json.dumps(signed))
    runner = FakeRunner({})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                                 verifier=fake_verifier)
    assert runner.verbs() == []                      # tampered: refused
    assert out["services"]["skgateway"]["acted"] == "unverified"


def test_enforce_without_verifier_fails_safe(paths, operator, scheduler_writer,
                                             monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(signing, "capauth_verifier", lambda: None)
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []                      # no roster: refuse, not stop
    assert out["services"]["skgateway"]["acted"] == "unverified"


def test_flip_is_a_config_change_only(paths, operator, scheduler_writer,
                                      monkeypatch) -> None:
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    converge.converge_once(paths, NODE, runner=runner, now=1000.0,
                           verifier=fake_verifier)
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    converge.converge_once(paths, NODE, runner=runner, now=1030.0,
                           verifier=fake_verifier)
    assert len(runner.verbs()) == 2                  # signed set: both modes act
```

2. Run to fail. Expected:
   `TypeError: converge_once() got an unexpected keyword argument 'verifier'`

3. Implement. In `src/skcapstone/fleet/converge.py`, add `from . import
   signing` to the imports and the helper after `local_services`:

```python
def verify_desired(
    spec_payload: dict,
    placement: dict | None,
    verifier,
) -> tuple[bool, str]:
    """Classify the pair of files actuation consumes (Card 3.5).

    Both the service spec and its placement must verify. A missing
    verifier (no roster, capauth absent) is unverified by definition:
    under enforce that refuses NEW actuation and never touches running
    services (fail safe at the trust boundary).
    """
    if verifier is None:
        return (False, "no verifier available (empty roster or capauth missing)")
    failures: list[str] = []
    for label, payload in (("spec", spec_payload), ("placement", placement)):
        if payload is None:
            continue
        status, detail = signing.verify_payload(payload, verifier)
        if status != "verified":
            failures.append(f"{label} {status}: {detail}")
    if failures:
        return (False, "; ".join(failures))
    return (True, "spec and placement verified")
```

   Extend `converge_service` with two keyword parameters (after `now`):
   `sig_mode: str = "off"` and `verification: tuple[bool, str] = (True,
   "")`. Inside, after the `track = backoff.tracker(node, name)` line,
   add:

```python
    verified_ok, verify_detail = verification
    if sig_mode != "off" and not verified_ok:
        if events.emit(paths, writer, kind="service", name=name, type="Trust",
                       reason="SpecUnverified", message=verify_detail, now=now):
            if sig_mode == "enforce":
                alerts.send_alert(
                    f"fleet: service {name} on {node} REFUSED actuation: "
                    f"{verify_detail}", level="error")
```

   and change the `may_heal` expression to include the enforce gate:

```python
    may_heal = (
        mode == "actuate"
        and not (sig_mode == "enforce" and not verified_ok)
        and not spec["paused"]
        and spec["restartPolicy"] == "on-failure"
        and unhealthy_unit
    )
```

   After the `if may_heal:` block, add the refused marker (so the summary
   is truthful):

```python
    if (mode == "actuate" and unhealthy_unit and not spec["paused"]
            and sig_mode == "enforce" and not verified_ok):
        acted = "unverified"
```

   and in the conditions list append, before the closing bracket:

```python
        *(
            [_cond("SpecUnverified", not verified_ok,
                   "SignatureInvalid" if not verified_ok else "SignatureOk",
                   verify_detail, now_iso)]
            if sig_mode != "off"
            else []
        ),
```

   Extend `converge_once` with `verifier=None` (after `now`) and compute
   the mode and per-service verification in the loop body:

```python
    sig_mode = signing.signing_mode()
    if sig_mode != "off" and verifier is None:
        verifier = signing.capauth_verifier()
    ...
    for entry in entries:
        verification = (True, "")
        if sig_mode != "off" and entry["spec_payload"] is not None:
            verification = verify_desired(entry["spec_payload"],
                                          entry["placement"], verifier)
        results[entry["name"]] = converge_service(
            paths, node, entry["name"], entry["spec_payload"],
            writer=writer, runner=runner, prober=prober, mode=mode, now=now,
            sig_mode=sig_mode, verification=verification)
```

   Append to `docs/runbooks/fleet-services.md`:

```markdown
## Signing rollout (Card 3.5, permissive-then-enforce)

1. Key ceremony: on each actuating node, create
   `~/.skcapstone/capauth/fleet-trust/` and install the OPERATOR seat's
   armored public key (the identity that runs skfleet on .158) plus any
   other trusted writer seats. The roster is LOCAL on purpose: it must
   never live in the synced fleet tree it authenticates.
2. Turn on signing at the writers first: export `SKFLEET_SIGNING=permissive`
   in the environment of skfleet (operator shell), the reconcile job, and
   each sknoded unit (`Environment=SKFLEET_SIGNING=permissive`). New spec
   and placement writes are now signed automatically; old files stay
   unsigned until their next content change (re-apply the service docs to
   re-sign everything in one pass).
3. Soak permissive for several days: `SpecUnverified` events and
   conditions appear for anything unsigned or failing verification, but
   actuation continues. Chase every warning to zero.
4. Flip to enforce (`SKFLEET_SIGNING=enforce`, restart sknoded units): an
   unverified spec or placement is refused for actuation within one
   reconcile period, raises SpecUnverified, and alerts. Running services
   are never touched by a refusal.
5. Acceptance drill: hand-tamper one signed service spec file; confirm
   refusal + alert within 30s, then restore the file (re-apply the doc)
   and confirm healing resumes.
```

4. Run to pass, then the whole `tests/fleet/` suite (with the env unset,
   sig_mode is off and every Task 4/7 converge test is untouched).
5. Commit: `feat(fleet): sknoded signature verification gate, permissive-then-enforce (Card 3.5)`

---

## Self-review

Card coverage (Phase 3 cards -> tasks):
- Card 3.1 (Service kind + sknoded actuation, systemd --user): Tasks 1-4.
  Spec shape with conservative defaults and validation (Task 1), the
  trustee-verb actuation library with injectable runner (Task 2), bounded
  10s-doubling-to-300s backoff with the CrashLooping condition and a
  single deduped alert (Task 3), and the converge loop with the full gate
  order (Task 4). Acceptance mapping: stop-is-healed and paused-stops-
  healing are pinned by test_failed_service_is_healed_with_logs_event,
  test_missing_unit_is_started, and test_paused_spec_stops_healing; the
  kill-loop drill by test_crash_loop_backoff_and_condition (bounded
  attempts, condition, exactly one alert); the live 60s heal on
  skwhisper@lumina is the runbook drill in Task 6 step 6 since a real
  systemd heal is exactly what hermetic tests must never do.
- Card 3.2 (ServiceController + pilot set): Tasks 5-6. Place-once,
  manual-failover-with-alert (deduped, never re-places), auto failover
  re-placing off the Dead node via the existing feasible() filter, frozen
  blocking placements but never the Dead alert, and read-time drift rows
  rendering stale/missing as Unknown. Pilot objects for skwhisper@lumina,
  skgateway, skcomms, skchat daemon as repo docs applied by runbook; the
  R2 churn re-measure is a runbook step (Task 9 item 5) because it is a
  live-fleet measurement, not a unit test.
- Card 3.3 (Docker runtime + drain): Tasks 7-8. Docker verbs + runtime
  dispatch with a converge test proving a docker service converges like a
  systemd one; drain = cordon + resident inventory (placements plus
  observed statuses, catching legacy residents) + alert, explicitly
  manual-move, placements untouched.
- Card 3.4 (onboard remaining + retire hand-run deploys): Task 9.
  skmemory daemon, ollama (gpu selector + dedicated toleration), piper-tts
  (:18797), nostr relay (:7447) as docs; skmem-pg excluded by TEST
  (test_skmem_pg_is_never_a_service) and served by the new operator-
  declared healthProbes node conditions instead; runbook covers unit-name
  verification, retirement of hand-run systemctl habits, and the
  one-clean-week acceptance.
- Card 3.5 (signing): Tasks 10-11. Canonical bytes over the whole payload
  (signature slot blanked), auto-signing inside write_spec/write_placement
  so the flip is a flag plus key ceremony with zero call-site migration,
  verification at the actuation boundary only, permissive warns and
  actuates, enforce refuses NEW actuation with condition + one deduped
  alert while probing and running services continue, tampered and
  missing-verifier cases fail safe. Trust roster is local armored keys
  under the capauth home, never in the synced tree.

Safety constraints, where enforced: freeze is checked first in
converge_once (test_freeze_halts_all_actuation_but_not_reporting) and in
scheduler.place (reused, retested via test_frozen_blocks_placements_...);
failover manual default in normalize_service_spec plus
test_manual_failover_alerts_and_never_replaces; report-only default via
actuation_enabled (absent flag = report-only,
test_report_only_without_opt_in) with the local box covered by the
runbook's explicit "stays report-only"; crash-loop bounded by
CRASH_LOOP_AFTER with healing stopped afterward; degrade-safe by
test_unreadable_spec_never_touches_the_unit (zero runner calls, not even
a probe) and test_unreachable_tree_is_degraded_noop; hermetic tests by
injectable runners everywhere (constraint 9), with the two subprocess
touches (default_runner shape, send_alert) monkeypatching subprocess.run.

Placeholder scan: no TODOs, no elided bodies, every test and
implementation block is complete runnable code. Honest soft points,
stated: (1) write-on-change skips re-signing an unchanged placement, so
after enabling signing the runbook re-applies docs to re-sign the world
(documented, step 2 of the signing rollout). (2) The controller tick is
wired as an skscheduler config job by runbook, not code, matching how
NodeController derivation is consumed today. (3) skchat daemon unit name
and the wave-2 unit names must be verified on-box before apply (runbook
step; the doc is fixed, not the box).

Signature consistency, checked against the MERGED code: every consumed
helper listed in the Tech stack section exists verbatim in the merged
store/paths/events/scheduler/node_controller/conditions/sknoded/cli
modules (read before writing this plan). New helpers added by this phase:
services.py, actuation.py, backoff.py, alerts.py, converge.py,
service_controller.py, signing.py, plus store._maybe_sign and the
signer kwarg (backward compatible), node_controller.set_actuation,
conditions.tcp_open/probe_conditions, and the sknoded main_loop
interleave. Changed existing signatures, all defaulted and
backward compatible: write_spec/write_placement (+signer),
main_loop (+actuation_interval), build_node_report (internal reorder
only, same signature). events.emit's role set is UNCHANGED this phase
(sknoded and controller were already allowed; the controller seat uses
role "controller", the reconcile placement writes use role "scheduler",
exactly per the merged ownership guards).

Reality-forced adjustments versus the spec text, stated explicitly:
1. "trustee_* operation verbs as the actuation library": the merged
   TrusteeOps binds to TeamEngine deployments, not arbitrary units, so
   Phase 3 wraps the verb SEMANTICS (state/restart/logs-on-failure,
   audited) in fleet/actuation.py following skcapstone.systemd's
   _systemctl pattern; trustee MCP tools remain the manual surface.
2. "drift raises conditions" on the controller side: service conditions
   live in sknoded-owned status files (single-writer), so controller
   drift is computed at read time (service_rows) and controller-side
   anomalies are events + alerts, never status writes.
3. Actuation opt-in is a spec field (node spec `actuate: true`) rather
   than a config file, so the report-only rule is declarative, auditable,
   and flows through the same signed write path as everything else.
4. `skfleet apply` (plain, no dry-run) ships now because the ownership
   table names it as the spec write path and Phase 3 needs it for the
   pilot set; Card 8.1 later adds dry-run on top of the same command.
5. The trust roster is a local fleet-trust directory of armored keys
   under the capauth home (plus the seat's own public key), because
   capauth has no fleet-writer roster API today; the ceremony runbook
   installs it, and the roster deliberately never syncs with the tree it
   authenticates.
