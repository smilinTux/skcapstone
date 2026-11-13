"""ATLAS P3.2 fault-injection drill: assemble the safety mechanisms and burn them.

Coord card ``b993eaaa`` (epic ``fb3cc09d``). Every mechanism this drill exercises
is already MERGED and unit-tested in isolation (``act_dispatch.py``,
``safety.py``, ``loop.py``, ``action_ledger.py``, ``fleet/store.py``); what did
not exist before this module is a harness that assembles them and fires a real
sequence of proposals through the real ``loop.run_once`` end to end, against a
real (but throwaway) fleet tree, with a real signed ledger.

Safety is structural, not a convention
---------------------------------------
This drill NEVER runs against production. The root is resolved and guarded by
:func:`skcapstone.fleet.drill.resolve_drill_root` (the same function the
control-seat promotion drill uses to keep itself out of the live Syncthing
tree): any root that resolves inside :data:`skcapstone.fleet.paths.SOVEREIGN_HOME`
raises :class:`~skcapstone.fleet.drill.UnsafeDrillRootError` before a single
byte is written. ``SKFLEET_ROOT`` is read only as a *candidate* default, run
through that same guard like any other candidate; there is no path in this
module that trusts an ambient value.

The ITIL correlation store, the decisions dir, the execution-state file and
the action ledger all live under the SAME guarded, marked drill root -
nothing here ever touches ``~/.skcapstone/coordination`` or
``~/.skcapstone/fleet``.

Honesty over green
-------------------
Each scenario records what actually happened. A scenario "passes" when the
mechanism behaved as documented (including when the documented behavior is a
refusal). A scenario that finds a real gap records ``passed=True`` for "the
drill correctly demonstrated the gap" only when the task explicitly asked the
drill to surface gaps; those scenarios carry a ``finding`` string that must be
read, not hidden. Nothing in this module is permitted to change
``act_dispatch.py``, ``safety.py``, ``loop.py`` or ``action_ledger.py`` merely to
make its own assertions pass.

Gate 5 closed three gaps this drill had documented, and they are now PASS
criteria rather than findings: scenario 1 (the outcome word is ``verified`` only
after verification ran), scenario 9 (stale, missing or unparseable evidence is
refused before anything runs) and scenario 12 (a mid-pass freeze is reported at
the decision point and never feeds the circuit breaker). Scenario 22 pins the
``performed, unverified`` word for an action nothing re-observed. If any of
those regress, the scenario FAILS; it no longer "passes by documenting a gap".
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..fleet import drill as fleet_drill
from ..fleet import store
from ..fleet.paths import FleetPaths, self_node_name
from ..itil import ITILManager
from . import (
    act_dispatch,
    action_ledger,
    decisions,
    dispatch,
    loop,
    node_local,
    policy,
    proposer,
    safety,
    skchat_adapter,
)

SCHEMA_NOTE = "ATLAS P3.2 fault-injection drill (card b993eaaa, epic fb3cc09d)"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def fresh_evidence(observed_at: str | None = None) -> dict:
    """Structured evidence for a drill proposal (fresh unless a time is given)."""
    return {
        "source": "observation:skchat/BridgeAlive/telegram-bridge",
        "observed_at": observed_at or _now_iso(),
    }


class DrillHarnessError(RuntimeError):
    """Something in the harness itself (not the code under test) misbehaved."""


@dataclass
class ScenarioResult:
    """One scenario's verdict, with the evidence a reader can check by hand."""

    name: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    finding: str | None = None  # non-None => a documented mechanism gap

    def line(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        gap = f" [FINDING: {self.finding}]" if self.finding else ""
        return f"[{mark}] {self.name} - {self.note}{gap}"


@dataclass
class DrillReport:
    root: Path
    results: list[ScenarioResult]
    started_at: str
    finished_at: str

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def findings(self) -> list[str]:
        return [f"{r.name}: {r.finding}" for r in self.results if r.finding]


# ---------------------------------------------------------------------------
# Root guard + harness plumbing
# ---------------------------------------------------------------------------


def guard_drill_root(root: Path | str | None) -> Path:
    """Resolve and hard-refuse a candidate root, never trusting ambient state.

    Delegates entirely to :func:`skcapstone.fleet.drill.resolve_drill_root`,
    the already-reviewed guard that resolves symlinks/``..`` BEFORE judging the
    path and compares against the resolved, password-database-derived home
    directory (immune to a rewritten ``$HOME``). This function adds nothing
    to that guard's logic on purpose: a second, slightly-different copy of a
    production-safety check is exactly how such a check silently drifts.
    """
    return fleet_drill.resolve_drill_root(root)


def default_candidate_root() -> str:
    """SKFLEET_ROOT if set, else the documented drill default.

    Reading SKFLEET_ROOT here is safe ONLY because the value is then run
    through :func:`guard_drill_root`, which raises the moment it resolves
    into the sovereign tree. If an operator's shell has SKFLEET_ROOT pointed
    at production (the normal state on a control node), this function still
    returns that value, and the guard is what refuses it - never this one.
    """
    return os.environ.get("SKFLEET_ROOT") or "/tmp/atlas-drill-fleet"


class Clock:
    """A tiny controllable time source so cooldown/circuit-breaker scenarios
    don't need to sleep for real minutes. Advancing it only ever moves
    forward, and every advance is logged into the scenario evidence so a
    reader can see exactly how much simulated time passed and why - the same
    technique :class:`skcapstone.fleet.drill.DrillFleet.beat` uses to
    backdate heartbeats rather than sleeping for real.
    """

    def __init__(self, start: float | None = None) -> None:
        self._t = start if start is not None else time.time()

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t


@dataclass
class DrillContext:
    root: Path
    paths: FleetPaths
    execution_state: safety.ExecutionState
    ledger: action_ledger.ActionLedger | None
    ledger_signed: bool
    _signer: Callable[[bytes], str] | None
    _verifier: Callable[[bytes, str], bool] | None
    itil_home: Path
    decisions_dir: Path
    heartbeat_path: Path
    outbox_dir: Path
    clock: Clock
    calls: list[list[str]] = field(default_factory=list)
    _saved_probe: Callable[[], tuple] | None = None
    _saved_env: dict[str, str | None] = field(default_factory=dict)

    def patch_env(self, **values: str) -> None:
        for key, value in values.items():
            if key not in self._saved_env:
                self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value

    def restore_env(self) -> None:
        for key, old in self._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved_env.clear()

    def patch_daemon_probe(
        self, ready: bool = True, auth: bool = True, calling: bool = True
    ) -> None:
        """Freeze the daemon-health signal so BridgeAlive is the only moving part.

        skchat_adapter._probe_daemon_health hits a real HTTP endpoint. The
        drill controls the fault through the heartbeat file's mtime alone
        (the documented silent-wedge signature: daemon up, poll stale), so
        the daemon-health leg is pinned rather than mocked at the network
        layer - the same choice test_gameday_drill.py makes with a fake
        urlopen response.
        """
        if self._saved_probe is None:
            self._saved_probe = skchat_adapter._probe_daemon_health
        skchat_adapter._probe_daemon_health = lambda: (ready, auth, calling)  # type: ignore[assignment]

    def restore_daemon_probe(self) -> None:
        if self._saved_probe is not None:
            skchat_adapter._probe_daemon_health = self._saved_probe  # type: ignore[assignment]
            self._saved_probe = None

    # --- fault injection -----------------------------------------------

    def inject_wedge(self, age_s: float = 1200.0) -> None:
        """The controlled fault: telegram bridge poll heartbeat stale, daemon up."""
        self.heartbeat_path.write_text("stale")
        old = time.time() - age_s
        os.utime(self.heartbeat_path, (old, old))

    def heal_wedge(self) -> None:
        """What a REAL successful restart does: the poll heartbeat goes fresh."""
        self.heartbeat_path.write_text("fresh")
        now = time.time()
        os.utime(self.heartbeat_path, (now, now))

    def bridge_alive(self) -> bool:
        obs = skchat_adapter.skchat_observe()
        by_type = {c["type"]: c for c in obs["conditions"]}
        return by_type["BridgeAlive"]["status"] == "True"

    # --- runners ----------------------------------------------------------

    def runner_success_and_heal(self) -> Callable[[list[str]], subprocess.CompletedProcess]:
        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            self.calls.append(cmd)
            self.heal_wedge()
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return _run

    def runner_success_no_heal(self) -> Callable[[list[str]], subprocess.CompletedProcess]:
        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return _run

    def runner_fail(self) -> Callable[[list[str]], subprocess.CompletedProcess]:
        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "simulated systemctl failure")

        return _run

    def runner_boom(self) -> Callable[[list[str]], subprocess.CompletedProcess]:
        def _run(cmd: list[str]) -> subprocess.CompletedProcess:
            raise DrillHarnessError(
                f"runner invoked ({cmd}) when the safety mechanism under test "
                "should have suppressed the actuation before it ever reached "
                "the systemd boundary"
            )

        return _run

    # --- proposals ----------------------------------------------------------

    def wedge_proposal(self, *, with_rollback: bool = False, **extra: Any) -> dict:
        proposal = {
            "app": "skchat",
            "condition": "BridgeAlive",
            "object": "telegram-bridge",
            "action": "restart-telegram-bridge",
            "change_class": "normal",
            "rationale": "bridge poll heartbeat stale while the daemon is up (silent wedge)",
            "ts": _now_iso(),
            # Structured, fresh evidence (operator_seat.evidence): without it the
            # loop refuses to actuate, which is exactly what scenario 9 proves.
            "evidence": fresh_evidence(),
        }
        if with_rollback:
            proposal["rollback"] = {"action": "restart-daemon", "object": "skchat-daemon"}
        proposal.update(extra)
        return proposal

    # --- state file introspection ---------------------------------------

    def read_execution_state(self) -> dict:
        path = self.execution_state.path
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def backdate_last_attempt(
        self, execution_state: safety.ExecutionState, fingerprint: str, seconds_ago: float
    ) -> None:
        """Simulate elapsed real time between retries without sleeping for it.

        Mirrors DrillFleet.beat()'s heartbeat backdating: we do not change
        safety.py's arithmetic, we change what "now" the NEXT eligibility
        check will see relative to a PAST last_attempt, by rewriting the
        persisted timestamp the same way a clock advancing would. The
        subsequent real loop.run_once() call is a genuine call through the
        real code path -- only the wall-clock illusion is synthetic.
        """
        payload = execution_state._load()
        entry = payload["actions"].get(fingerprint)
        if entry is None:
            raise DrillHarnessError(f"no execution-state entry for {fingerprint}")
        entry["last_attempt"] = time.time() - seconds_ago
        execution_state._save(payload)

    # --- per-scenario namespaced state/ledger --------------------------
    #
    # skchat_adapter.skchat_observe() always reports object="telegram-bridge"
    # (it is not driven by the proposal), so every scenario's proposal MUST use
    # that same real object for post-action verification to ever match. But
    # safety.action_fingerprint(app, condition, object, action) and
    # ActionIntent.identity() are then IDENTICAL across every scenario that
    # drills the same real fault -- which is correct in production (one
    # standing condition, one cooldown/circuit-breaker lineage, one ledger
    # lineage) but means 16 independent scenarios sharing ONE ExecutionState
    # and ONE ActionLedger would falsely poison each other (an earlier
    # scenario's failure would cooldown-block a later, unrelated scenario; an
    # earlier scenario reaching a terminal ledger state would make the ledger
    # refuse EVERY later scenario's transitions with "invalid action
    # transition"). Each independent scenario therefore gets its OWN
    # namespaced ExecutionState/ActionLedger (still real, still signed, still
    # under the guarded drill root) so it tests its mechanism in isolation.
    # The canonical, unnamespaced root/atlas/{state,action-ledger} paths
    # (self.execution_state / self.ledger) are left for the FIRST scenario to
    # populate, so the deliverable artifacts a reader inspects are a genuine,
    # clean, single example -- and one dedicated scenario deliberately
    # reuses ONE identity across two passes to demonstrate what happens when
    # a ledger lineage is asked to represent a second, later occurrence of
    # the same standing condition.

    def scenario_state(
        self, slug: str, *, cooldown_seconds: float = 900.0, retry_budget: int = 3
    ) -> safety.ExecutionState:
        return safety.ExecutionState(
            self.root / "atlas" / "state-scenarios" / slug,
            cooldown_seconds=cooldown_seconds,
            retry_budget=retry_budget,
        )

    def scenario_ledger(self, slug: str) -> action_ledger.ActionLedger:
        root = self.root / "atlas" / "action-ledger-scenarios" / slug
        if self.ledger_signed:
            return action_ledger.ActionLedger(
                root, signer=self._signer, verifier=self._verifier, require_signatures=True
            )
        return action_ledger.ActionLedger(root)


def build_context(
    root: Path, *, cooldown_seconds: float = 900.0, retry_budget: int = 3
) -> DrillContext:
    paths = FleetPaths(root=root)
    # Human-only provisioning ceremony (AUTONOMY_ARCHITECTURE.md section 3.6,
    # coord card 3925d012): actuator.honor now refuses with reason
    # "unprovisioned" until a human has written the freeze store, and every
    # scenario below expects actuator.honor to attempt real actuation (or
    # refuse for a documented reason OTHER than "the kill switch was never
    # proven to exist"). Provision it off here, exactly as a real estate
    # must be provisioned before Atlas's actuation is ever turned on.
    store.set_frozen(paths, False, writer=_human_writer(), reason="drill root provisioning")
    execution_state = safety.ExecutionState(
        root / "atlas" / "state", cooldown_seconds=cooldown_seconds, retry_budget=retry_budget
    )
    signer = None
    verifier = None
    ledger_signed = False
    try:
        from ..fleet import signing

        signer = signing.capauth_signer()
        verifier = signing.capauth_verifier()
        ledger_signed = signer is not None and verifier is not None
    except Exception:  # noqa: BLE001 - capauth is a soft dependency everywhere else too
        pass
    ledger: action_ledger.ActionLedger | None
    if ledger_signed:
        ledger = action_ledger.ActionLedger(
            root / "atlas" / "action-ledger",
            signer=signer,
            verifier=verifier,
            require_signatures=True,
        )
    else:
        # Fail CLOSED on the drill's own claim, not silently degrade: a ledger
        # this drill cannot sign is not the ledger CR-9.1 documents, so scenario 6
        # (below) records this explicitly rather than quietly running unsigned.
        ledger = action_ledger.ActionLedger(root / "atlas" / "action-ledger")

    outbox_dir = root / "empty-outbox"
    outbox_dir.mkdir(parents=True, exist_ok=True)
    return DrillContext(
        root=root,
        paths=paths,
        execution_state=execution_state,
        ledger=ledger,
        ledger_signed=ledger_signed,
        _signer=signer,
        _verifier=verifier,
        itil_home=root / "itil-home",
        decisions_dir=root / "decisions",
        heartbeat_path=root / "telegram_poll.ts",
        outbox_dir=outbox_dir,
        clock=Clock(),
    )


def _human_writer(node: str = "cli") -> store.Writer:
    return store.Writer(role="operator", node=node, identity="capauth:drill@scratch")


def _freeze(paths: FleetPaths, reason: str) -> None:
    store.set_frozen(paths, True, writer=_human_writer(), reason=reason)


def _unfreeze(paths: FleetPaths) -> None:
    store.set_frozen(paths, False, writer=_human_writer(), reason="drill scenario cleanup")


def _run_pass(
    ctx: DrillContext,
    *,
    propose: Callable[[dict, str], list[dict]],
    apply_fn: Callable[[dict, dict], Any] | None,
    execution_state: safety.ExecutionState,
    ledger: action_ledger.ActionLedger | None,
    rollback_fn: Callable[[dict, dict, Any], Any] | None = None,
    execute: bool = True,
    decisions_dir: Path | None = None,
) -> dict:
    return loop.run_once(
        ctx.paths,
        now_iso=_now_iso(),
        propose=propose,
        explain=act_dispatch.merged_explain(),
        decisions_dir=str(decisions_dir if decisions_dir is not None else ctx.decisions_dir),
        apply_fn=apply_fn,
        rollback_fn=rollback_fn,
        execute=execute,
        emit=lambda _m: None,
        execution_state=execution_state,
        lifecycle_ledger=ledger,
        ledger_actor="atlas-p32-drill",
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_baseline_self_heal(ctx: DrillContext) -> ScenarioResult:
    """A wedge is injected, detected, actuated, and verified end to end.

    Uses the CANONICAL, unnamespaced execution-state.json / action-ledger/
    paths (the only scenario that does), so the drill root's headline
    artifacts are a genuine, clean, single example a reader can inspect.
    """
    ctx.inject_wedge()
    assert ctx.bridge_alive() is False, "harness bug: wedge did not fire"

    proposal = ctx.wedge_proposal()
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
    )
    calls_before = len(ctx.calls)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.execution_state,
        ledger=ctx.ledger,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")
    state = ctx.ledger.current_state(intent_id) if intent_id else None
    # Attaching lifecycle_ledger alone forces performed=True proof AND the
    # post-action re-observation, so the outcome word must be "verified" and must
    # agree with the LEDGER's own folded state (gate 5 weak spot 3: it used to
    # say "applied" here, which a reader could not tell from "not verified").
    passed = (
        outcome.get("outcome") == "verified"
        and state == action_ledger.ActionState.VERIFIED
        and len(ctx.calls) == calls_before + 1
        and ctx.bridge_alive() is True
    )
    return ScenarioResult(
        name="1. baseline self-heal (detect -> act -> verify)",
        passed=passed,
        evidence={
            "outcome": outcome,
            "ledger_state": str(state),
            "systemctl_calls": ctx.calls[calls_before:],
            "bridge_alive_after": ctx.bridge_alive(),
        },
        note="wedge injected, restarted, postcondition re-observed clear, outcome word "
        "'verified' agrees with the ledger's VERIFIED state "
        "(canonical atlas/action-ledger/ + atlas/state/execution-state.json)",
    )


def scenario_performed_false_actdispatch_gate(ctx: DrillContext) -> ScenarioResult:
    """Gate A: act_dispatch itself raises when actuator.honor reports performed=False."""
    slug = "s02-performed-false-actdispatch"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
    )
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ctx.scenario_ledger(slug),
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    raised_here = "actuation failed" in outcome.get("outcome", "")
    intent_id = outcome.get("intent_id")
    ledger = ctx.scenario_ledger(slug)
    state = ledger.current_state(intent_id) if intent_id else None
    passed = raised_here and state in (
        action_ledger.ActionState.FAILED,
        action_ledger.ActionState.ESCALATED,
    )
    return ScenarioResult(
        name="2a. performed=False (gate A: act_dispatch.build_apply_fn raises)",
        passed=passed,
        evidence={"outcome": outcome, "ledger_state": str(state)},
        note="runner returned rc=1; actuator.honor -> performed=False; "
        "act_dispatch raised before returning",
    )


def scenario_performed_false_loop_gate(ctx: DrillContext) -> ScenarioResult:
    """Gate B: loop.py itself fails a NON-honor apply_fn that RETURNS performed=False
    without raising (proves loop.py has its own independent check, not just act_dispatch's)."""
    slug = "s03-performed-false-loop-gate"
    proposal = ctx.wedge_proposal()

    def raw_apply_fn(prop: dict, cls: dict) -> dict:
        return {"performed": False, "reason": "adapter reported failure without raising"}

    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=raw_apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ctx.scenario_ledger(slug),
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    passed = "actuator reported performed=False" in outcome.get("outcome", "")
    return ScenarioResult(
        name="2b. performed=False (gate B: loop.py's own performed-flag check)",
        passed=passed,
        evidence={"outcome": outcome},
        note=(
            "a hand-rolled apply_fn that RETURNS {'performed': False} instead of raising "
            "is still caught, independently of act_dispatch"
        ),
    )


def scenario_performed_proof_omitted(ctx: DrillContext) -> ScenarioResult:
    """Gate B variant: an adapter that OMITS 'performed' entirely fails closed
    once a lifecycle ledger is attached (proof_required)."""
    slug = "s04-performed-proof-omitted"
    proposal = ctx.wedge_proposal()

    def raw_apply_fn(prop: dict, cls: dict) -> dict:
        return {"note": "did something, forgot to say what"}

    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=raw_apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ctx.scenario_ledger(slug),
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    passed = "actuator omitted performed=True proof" in outcome.get("outcome", "")
    return ScenarioResult(
        name="2c. performed proof omitted (ledger attached => fail closed)",
        passed=passed,
        evidence={"outcome": outcome},
        note="adapter dict has no 'performed' key at all; ledger attachment makes proof mandatory",
    )


def scenario_cooldown_blocks_retry(ctx: DrillContext) -> ScenarioResult:
    """Cooldown: an immediate re-proposal of the same fingerprint is suppressed
    BEFORE apply_fn is ever invoked."""
    slug = "s05-cooldown"
    proposal = ctx.wedge_proposal()
    fingerprint = safety.action_fingerprint(proposal)
    state = ctx.scenario_state(slug)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug

    first = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
        ),
        execution_state=state,
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    calls_before = len(ctx.calls)
    second = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
        ),
        execution_state=state,
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    outcome2 = second["outcomes"][0] if second["outcomes"] else {}
    suppressed = "execution suppressed: cooldown" in outcome2.get("outcome", "")
    passed = suppressed and len(ctx.calls) == calls_before  # runner_boom never ran
    return ScenarioResult(
        name="3. cooldown suppresses an immediate re-attempt",
        passed=passed,
        evidence={
            "first_outcome": first["outcomes"][0] if first["outcomes"] else {},
            "second_outcome": outcome2,
            "eligibility_now": state.eligibility(fingerprint, time.time()),
        },
        note="first attempt fails and records; second (same fingerprint, no time "
        "passed) never reaches apply_fn",
    )


def scenario_circuit_breaker_opens(ctx: DrillContext) -> ScenarioResult:
    """Circuit breaker: after retry_budget consecutive failures, the fingerprint
    stays refused ('circuit-open') even once the cooldown window has elapsed.

    Deliberately runs WITHOUT a lifecycle_ledger attached (ledger=None), the
    same shape as production's execute-without-honor annotation-only path
    (operator_seat/cli.py's `elif execute:` branch). This isolates safety.py's
    own retry_budget/circuit-breaker arithmetic from a SEPARATE gap: with a
    ledger attached and no rollback plan, a single failure already escalates
    that intent to a terminal ledger state, and every retry after the first
    fails immediately with an "invalid action transition" ledger error rather
    than a genuine re-attempt -- see scenario 11, which drills that
    specifically. Trying to demonstrate both mechanisms in the SAME run
    conflates two independent gaps into one confusing result.
    """
    slug = "s06-circuit-breaker"
    proposal = ctx.wedge_proposal()
    fingerprint = safety.action_fingerprint(proposal)
    state = ctx.scenario_state(slug)
    decisions_dir = ctx.decisions_dir / slug
    budget = state.retry_budget
    cooldown = state.cooldown_seconds

    outcomes = []
    for _attempt in range(budget):
        res = _run_pass(
            ctx,
            propose=lambda b, r: [proposal],
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
            ),
            execution_state=state,
            ledger=None,
            decisions_dir=decisions_dir,
        )
        outcomes.append(res["outcomes"][0] if res["outcomes"] else {})
        # Simulate the cooldown window having elapsed before the NEXT attempt,
        # the same way DrillFleet.beat() backdates a heartbeat instead of
        # sleeping for real minutes. safety.py's arithmetic is untouched.
        ctx.backdate_last_attempt(state, fingerprint, cooldown + 1)

    calls_before = len(ctx.calls)
    final = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
        ),
        execution_state=state,
        ledger=None,
        decisions_dir=decisions_dir,
    )
    final_outcome = final["outcomes"][0] if final["outcomes"] else {}
    circuit_open = "execution suppressed: circuit-open" in final_outcome.get("outcome", "")
    all_three_were_real_attempts = all(
        "actuation failed" in o.get("outcome", "") for o in outcomes
    )
    passed = circuit_open and all_three_were_real_attempts and len(ctx.calls) == calls_before
    state_entry = json.loads(state.path.read_text())["actions"].get(fingerprint, {})
    return ScenarioResult(
        name=f"4. circuit breaker opens after retry_budget={budget} failures",
        passed=passed,
        evidence={
            "attempt_outcomes": outcomes,
            "final_outcome_after_cooldown_elapsed": final_outcome,
            "execution_state_entry": state_entry,
        },
        note="3 real failures via the actual loop path (no ledger attached - see 11 for why); "
        "cooldown backdated between them; the 4th (even past cooldown) is refused with "
        "circuit-open",
    )


def scenario_postverification_blocks(ctx: DrillContext) -> ScenarioResult:
    """A 'successful' actuation (rc=0) that did NOT clear the underlying
    condition is still blocked by post-action verification."""
    slug = "s07-postverify"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_no_heal(), itil=None
    )
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ctx.scenario_ledger(slug),
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    blocked = "bound condition still firing after action" in outcome.get("outcome", "")
    passed = blocked and ctx.bridge_alive() is False
    ctx.heal_wedge()  # clean up so it doesn't bleed into later scenarios' observe passes
    return ScenarioResult(
        name="5. post-action verification blocks an ineffective 'success'",
        passed=passed,
        evidence={"outcome": outcome, "bridge_alive_after": False},
        note="systemctl restart reports rc=0 (performed=True) but the heartbeat was never "
        "touched; re-observation still sees BridgeAlive=False and the loop raises",
    )


def scenario_rollback_executes(ctx: DrillContext) -> ScenarioResult:
    """A failed primary action with a typed rollback plan: rollback runs and
    the ledger records ROLLED_BACK."""
    slug = "s08-rollback-ok"
    proposal = ctx.wedge_proposal(with_rollback=True)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
    )
    rollback_calls: list[list[str]] = []

    def rollback_runner(cmd: list[str]) -> subprocess.CompletedProcess:
        rollback_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    rollback_fn = act_dispatch.build_rollback_fn(ctx.paths, runner=rollback_runner)
    ledger = ctx.scenario_ledger(slug)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        rollback_fn=rollback_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")
    state = ledger.current_state(intent_id) if intent_id else None
    passed = (
        "rolled back" in outcome.get("outcome", "")
        and state == action_ledger.ActionState.ROLLED_BACK
        and len(rollback_calls) == 1
    )
    return ScenarioResult(
        name="6a. typed rollback executes on failure",
        passed=passed,
        evidence={
            "outcome": outcome,
            "ledger_state": str(state),
            "rollback_systemctl_calls": rollback_calls,
        },
        note="primary restart fails; typed rollback.action (restart-daemon) is dispatched "
        "through the same adapter boundary and succeeds",
    )


def scenario_escalation_no_rollback(ctx: DrillContext) -> ScenarioResult:
    """A failed primary action with NO rollback plan attached escalates."""
    slug = "s09-escalate-no-rollback"
    proposal = ctx.wedge_proposal(with_rollback=False)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
    )
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        rollback_fn=None,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")
    events = ledger.events(intent_id) if intent_id else []
    last = events[-1] if events else None
    passed = (
        last is not None
        and last.state == action_ledger.ActionState.ESCALATED
        and last.detail.get("rollback_error") is None
        and last.detail.get("decision_parked") is True
    )
    return ScenarioResult(
        name="6b. escalation when no rollback plan is attached",
        passed=passed,
        evidence={
            "outcome": outcome,
            "final_event": _redact_event(last) if last else None,
            "pending_decision_parked": bool(_load_decisions(decisions_dir)),
        },
        note="proposal carries no 'rollback' key; loop never calls a rollback_fn; ledger "
        "goes straight to ESCALATED and a decision is parked for a human",
    )


def scenario_escalation_rollback_fails(ctx: DrillContext) -> ScenarioResult:
    """Both the primary action AND its rollback fail: escalation, with the
    rollback failure recorded on the ledger event."""
    slug = "s10-escalate-rollback-fails"
    proposal = ctx.wedge_proposal(with_rollback=True)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
    )

    def bad_rollback_fn(prop: dict, cls: dict, result: Any) -> dict:
        raise RuntimeError("rollback target unreachable (simulated)")

    ledger = ctx.scenario_ledger(slug)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        rollback_fn=bad_rollback_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")
    events = ledger.events(intent_id) if intent_id else []
    last = events[-1] if events else None
    passed = (
        last is not None
        and last.state == action_ledger.ActionState.ESCALATED
        and last.detail.get("rollback_error") is not None
        and "rollback target unreachable" in str(last.detail.get("rollback_error"))
    )
    return ScenarioResult(
        name="6c. escalation when rollback itself fails",
        passed=passed,
        evidence={"outcome": outcome, "final_event": _redact_event(last) if last else None},
        note="rollback_fn raises; the primary failure AND the rollback failure both "
        "surface in the ESCALATED event's detail",
    )


def scenario_signed_ledger_and_itil(ctx: DrillContext) -> ScenarioResult:
    """A real capauth-signed ledger entry, plus ITIL change correlation supplied
    on the proposal. Also proves the auto-created-change correlation gap (card
    0e98a570, defect 2) is now closed: act_dispatch's OWN auto-created change
    (from apply_fn's `itil=itil`, which fires regardless of whether the
    proposer pre-supplied one) is a separate record from the frozen intent's
    proposer-scoped `itil_change_id` field -- that stays true by design, the
    field cannot be rewritten post-freeze without changing what identity it
    hashes to -- but its id is no longer LOST: loop.py now records it on the
    durable, append-only VERIFIED event's detail."""
    slug = "s11-ledger-itil"
    if not ctx.ledger_signed:
        return ScenarioResult(
            name="7. signed ledger + ITIL correlation",
            passed=False,
            evidence={},
            note="no usable capauth signer/verifier on this host for the active agent identity",
            finding="ActionLedger fell back to UNSIGNED because signing.capauth_signer()/"
            "capauth_verifier() returned None; see 'ledger_signed' in the top-level report",
        )
    from ..itil import ITILManager

    itil = ITILManager(ctx.itil_home)
    pre_created = itil.propose_change(
        title="ATLAS P3.2 drill: restart telegram bridge",
        change_type="normal",
        risk="low",
        rollback_plan="revert via controller reconcile",
        created_by="atlas-p32-drill",
        tags=["operator", "drill"],
    )
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal(change_id=pre_created.id)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=itil
    )
    ledger = ctx.scenario_ledger(slug)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")
    intent = ledger.read_intent(intent_id) if intent_id else None
    events = ledger.events(intent_id) if intent_id else []
    all_signed = bool(events) and all(e.signature for e in events)
    chain_ok = True
    prev_hash = None
    for e in events:
        if e.previous_hash != prev_hash:
            chain_ok = False
        prev_hash = e.event_hash
    # A proposal-supplied change id is NEVER trusted (review round 4): the proposal
    # pointed at an approved change for something else, and it must not become the
    # intent's authorization. With no ITIL manager on the loop the intent is simply
    # unbound; with one, the loop drafts its own change bound to this intent.
    correlated = intent is not None and intent.itil_change_id != pre_created.id

    # act_dispatch's OWN auto-created ITIL change (from apply_fn's `itil=itil`,
    # which always fires) is still a SEPARATE record from the frozen intent's
    # itil_change_id -- but it must now show up on the VERIFIED event's detail
    # (card 0e98a570 defect 2 fix), closing the correlation loss.
    auto_records = [c for c in itil.list_changes() if c.id != pre_created.id]
    auto_change_id = auto_records[-1].id if auto_records else None
    verified_event = events[-1] if events else None
    auto_change_recorded_on_ledger = (
        verified_event is not None
        and verified_event.state is action_ledger.ActionState.VERIFIED
        and verified_event.detail.get("itil_change_id") == auto_change_id
    )

    passed = all_signed and chain_ok and correlated and auto_change_recorded_on_ledger
    return ScenarioResult(
        name="7. signed ledger (real PGP) + ITIL correlation",
        passed=passed,
        evidence={
            "intent_id": intent_id,
            "events_signed": all_signed,
            "hash_chain_ok": chain_ok,
            "event_count": len(events),
            "pre_created_itil_change_id": pre_created.id,
            "ledger_itil_change_id (frozen intent field)": (
                intent.itil_change_id if intent else None
            ),
            "proposal_supplied_change_id_ignored": correlated,
            "auto_created_itil_change_id (act_dispatch's own record)": auto_change_id,
            "auto_change_id_recorded_on_verified_event_detail": auto_change_recorded_on_ledger,
            "sample_event_redacted": _redact_event(events[-1]) if events else None,
        },
        note="a proposal-supplied change_id is ignored (never trusted as an authorization), "
        "AND act_dispatch's own auto-created change (a separate record, since build_apply_fn "
        "always opens one when itil= is wired) is recorded on the VERIFIED event's detail, "
        "so an auditor can still trace the actuation from the ledger",
    )


def _major_explain() -> dict:
    """The real merged action catalog, with restart-telegram-bridge's own
    ``reversible`` flag forced False so ``plan.plan_actions`` classifies it
    MAJOR (requires a human) instead of the auto-normal tier its real catalog
    entry earns. The routing/actuation path stays the REAL skchat adapter --
    only the classification metadata is overridden, so this proposal
    genuinely parks for a human and genuinely, physically actuates once
    approved, exactly the arc the dispatcher exists to close."""
    explain = act_dispatch.merged_explain()
    explain = {**explain, "actions": [dict(a) for a in explain["actions"]]}
    for action in explain["actions"]:
        if action["name"] == "restart-telegram-bridge":
            action["reversible"] = False
    return explain


def scenario_dispatcher_full_arc(ctx: DrillContext) -> ScenarioResult:
    """The card's headline acceptance criterion (cf12b21d / SKW-AUTONOMY-E3):
    a MAJOR proposal parks, a simulated human CAB-approves via
    ``dispatch.resolve_decision`` (never the literal ``by="human"``), the
    dispatcher appends AUTHORIZED carrying the change id and the fold's own
    approval provenance, the CAPTURED actuation fires (no real systemd), the
    postcondition verifies against the REAL skchat observer, and the ledger
    lineage reads OBSERVED through VERIFIED end to end."""
    slug = "s18-dispatcher-full-arc"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug

    ctx.inject_wedge()
    assert ctx.bridge_alive() is False, "harness bug: wedge did not fire"
    proposal = ctx.wedge_proposal()

    # Pass 1: propose. No apply_fn wired -- a MAJOR proposal must never reach
    # one; this pass only observes, diagnoses, drafts the ITIL change, parks
    # the decision, and stops.
    park_result = loop.run_once(
        ctx.paths,
        now_iso=_now_iso(),
        propose=lambda b, r: [proposal],
        explain=_major_explain(),
        decisions_dir=str(decisions_dir),
        apply_fn=None,
        execute=True,
        emit=lambda _m: None,
        execution_state=ctx.scenario_state(slug),
        lifecycle_ledger=ledger,
        ledger_actor="atlas-p32-drill",
        itil=itil,
    )
    outcome0 = park_result["outcomes"][0] if park_result["outcomes"] else {}
    intent_id = outcome0.get("intent_id")
    parked = decisions.list_pending(str(decisions_dir))

    change_id = ledger.read_intent(intent_id).itil_change_id if intent_id else None
    linked_on_decision = bool(
        parked and parked[0]["options"][0].get("itil_change_id") == change_id
    )

    # A simulated human CAB-approves through the write-through path -- this
    # is the ONLY place a CAB vote gets submitted; by="human" is nowhere.
    dispatch.resolve_decision(
        str(decisions_dir),
        itil,
        parked[0]["id"],
        approve=True,
        choice=0,
        subject="capauth:drill-human@scratch",
        resolved_iso=_now_iso(),
    )
    fold_after_approval = [c for c in itil.list_changes() if c.id == change_id][0]

    # Pass 2 (the slow lane, `skoperator honor-pending`'s own code path): the
    # dispatcher independently re-reads the fold, authorizes, and actuates
    # through the CAPTURED runner.
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=itil
    )
    rollback_fn = act_dispatch.build_rollback_fn(ctx.paths)
    outcomes = dispatch.run_dispatch_pass(
        ctx.paths,
        ledger,
        itil,
        adapters=loop.ADAPTERS,
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        rollback_fn=rollback_fn,
        execution_state=ctx.scenario_state(slug),
        decisions_dir=str(decisions_dir),
        now_iso=_now_iso(),
        actor="atlas-p32-drill",
        emit=lambda _m: None,
    )
    dispatch_outcome = outcomes[0] if outcomes else None

    events = ledger.events(intent_id) if intent_id else []
    lineage = [e.state.value for e in events]
    authorized_event = next((e for e in events if e.state.value == "authorized"), None)

    passed = (
        outcome0.get("disposition") == "escalate"
        and bool(parked)
        and linked_on_decision
        and fold_after_approval.status.value == "approved"
        and dispatch_outcome is not None
        and dispatch_outcome.outcome == "verified"
        and lineage == ["observed", "diagnosed", "proposed", "authorized", "executing", "verified"]
        and authorized_event is not None
        and authorized_event.detail.get("itil_change_id") == change_id
        and "approval_provenance" in authorized_event.detail
        and ctx.bridge_alive() is True
    )
    return ScenarioResult(
        name="18. dispatcher full arc: park -> CAB approve -> AUTHORIZED -> actuate -> VERIFIED",
        passed=passed,
        evidence={
            "intent_id": intent_id,
            "itil_change_id": change_id,
            "parked_decision_linked_to_change": linked_on_decision,
            "fold_status_after_cab_vote": fold_after_approval.status.value,
            "dispatch_outcome": dispatch_outcome.outcome if dispatch_outcome else None,
            "lineage": lineage,
            "authorized_detail": authorized_event.detail if authorized_event else None,
            "bridge_alive_after": ctx.bridge_alive(),
        },
        note="the propose-to-approve-to-act handoff cf12b21d exists to build: a MAJOR "
        "proposal that used to be a dead end at park now actuates once, and only "
        "once, a human's CAB vote folds the linked ITIL change approved",
    )


def scenario_dispatcher_unapproved_never_actuates(ctx: DrillContext) -> ScenarioResult:
    """Negative test: fails against pre-change behaviour (there was no
    dispatcher to fail; nothing downstream ever re-read a resolved OR
    unresolved decision at all). A parked, NOT-yet-approved change must never
    actuate: the captured runner is `runner_boom`, which raises if it is ever
    invoked."""
    slug = "s19-dispatcher-unapproved"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug

    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    park_result = loop.run_once(
        ctx.paths,
        now_iso=_now_iso(),
        propose=lambda b, r: [proposal],
        explain=_major_explain(),
        decisions_dir=str(decisions_dir),
        apply_fn=None,
        execute=True,
        emit=lambda _m: None,
        execution_state=ctx.scenario_state(slug),
        lifecycle_ledger=ledger,
        ledger_actor="atlas-p32-drill",
        itil=itil,
    )
    intent_id = park_result["outcomes"][0]["intent_id"]
    # Deliberately NOT approved: no CAB vote submitted.

    calls_before = len(ctx.calls)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=itil
    )
    outcomes = dispatch.run_dispatch_pass(
        ctx.paths,
        ledger,
        itil,
        adapters=loop.ADAPTERS,
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        decisions_dir=str(decisions_dir),
        now_iso=_now_iso(),
        actor="atlas-p32-drill",
        emit=lambda _m: None,
    )
    outcome = outcomes[0] if outcomes else None
    passed = (
        outcome is not None
        and outcome.outcome.startswith("pending:")
        and ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
        and ctx.bridge_alive() is False
        and len(ctx.calls) == calls_before  # runner_boom would have raised had it been called
    )
    return ScenarioResult(
        name="19. unapproved change never actuates",
        passed=passed,
        evidence={
            "intent_id": intent_id,
            "dispatch_outcome": outcome.outcome if outcome else None,
            "ledger_state": ledger.current_state(intent_id).value,
        },
        note="the ITIL fold never received a qualifying CAB approval, so the "
        "dispatcher leaves the intent PROPOSED and never calls apply_fn",
    )


def scenario_dispatcher_unlinked_decision_never_actuates(ctx: DrillContext) -> ScenarioResult:
    """Negative test: fails against pre-change behaviour, where `decide
    --approve` wrote `by="human"` and printed success unconditionally, no
    matter what the decision's option carried. A decision parked with no
    itil_change_id (never bound to a change -- the failure mode of any path
    that skips the loop's own drafting step) must refuse write-through
    approval BEFORE any write, not merely fail to actuate later."""
    slug = "s20-dispatcher-unlinked-decision"
    itil = ITILManager(ctx.itil_home)
    decisions_dir = str(ctx.decisions_dir / slug)
    decisions.park(
        decisions_dir,
        [{"action": "restart-telegram-bridge", "object": "telegram-bridge"}],  # no itil_change_id
        decision_id="d-unlinked",
        created_iso=_now_iso(),
    )
    # ctx.itil_home is shared across scenarios in one drill run (same convention
    # scenario_signed_ledger_and_itil already uses), so "no change/vote was ever
    # touched" is checked by count, not by an empty-store snapshot.
    changes_before = len(itil.list_changes())

    raised = None
    try:
        dispatch.resolve_decision(
            decisions_dir,
            itil,
            "d-unlinked",
            approve=True,
            choice=0,
            subject="capauth:drill-human@scratch",
            resolved_iso=_now_iso(),
        )
    except dispatch.UnauthorizedDecisionError as exc:
        raised = str(exc)

    passed = (
        raised is not None
        and "no ITIL change linked" in raised
        and len(decisions.list_pending(decisions_dir)) == 1  # left pending, not resolved
        and len(itil.list_changes()) == changes_before  # no change/vote was ever touched
    )
    return ScenarioResult(
        name="20. decisions-store-only approval (no linked change) never actuates",
        passed=passed,
        evidence={"raised": raised, "still_pending": decisions.list_pending(decisions_dir)},
        note="resolve_decision refuses BEFORE any write when the chosen option carries no "
        "itil_change_id; a caller (the skoperator decide CLI) that fails to catch this "
        "never gets to print success either",
    )


def scenario_dispatcher_stale_catalog_generation_escalates(ctx: DrillContext) -> ScenarioResult:
    """Negative test: fails against pre-change behaviour (no re-classification
    at dispatch time existed at all). The intent is proposed and CAB-approved
    against one catalog_generation; a human then re-touches the skchat
    OperatorApp catalog record (generation bumps) before the dispatcher's
    pass runs. The stale approval must refuse and escalate, never actuate."""
    slug = "s21-dispatcher-stale-generation"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug
    human = _human_writer()

    # Establish the BASELINE live generation the proposal will be bound to
    # (ctx.paths' operatorapp/skchat record may already exist from an earlier
    # scenario in this same drill run, so read the real resulting generation
    # back from write_spec rather than assuming "1").
    baseline_record = store.write_spec(
        ctx.paths, "operatorapp", "skchat", {"ratifiedStandardActions": []}, writer=human
    )
    baseline_generation = str(baseline_record["generation"])

    ctx.inject_wedge()
    proposal = ctx.wedge_proposal(catalog_generation=baseline_generation)
    park_result = loop.run_once(
        ctx.paths,
        now_iso=_now_iso(),
        propose=lambda b, r: [proposal],
        explain=_major_explain(),
        decisions_dir=str(decisions_dir),
        apply_fn=None,
        execute=True,
        emit=lambda _m: None,
        execution_state=ctx.scenario_state(slug),
        lifecycle_ledger=ledger,
        ledger_actor="atlas-p32-drill",
        itil=itil,
    )
    intent_id = park_result["outcomes"][0]["intent_id"]
    change_id = ledger.read_intent(intent_id).itil_change_id
    parked = decisions.list_pending(str(decisions_dir))
    dispatch.resolve_decision(
        str(decisions_dir),
        itil,
        parked[0]["id"],
        approve=True,
        choice=0,
        subject="capauth:drill-human@scratch",
        resolved_iso=_now_iso(),
    )

    # The live skchat catalog moves on (a human re-touches it) AFTER approval,
    # so the approval is now stale: the live generation no longer matches
    # baseline_generation the intent was proposed and approved against.
    store.write_spec(
        ctx.paths, "operatorapp", "skchat", {"ratifiedStandardActions": []}, writer=human
    )

    calls_before = len(ctx.calls)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=itil
    )
    outcomes = dispatch.run_dispatch_pass(
        ctx.paths,
        ledger,
        itil,
        adapters=loop.ADAPTERS,
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        decisions_dir=str(decisions_dir),
        now_iso=_now_iso(),
        actor="atlas-p32-drill",
        emit=lambda _m: None,
    )
    outcome = outcomes[0] if outcomes else None
    passed = (
        outcome is not None
        and outcome.outcome.startswith("refused:")
        and "stale catalog_generation" in outcome.outcome
        and ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
        and "authorized" not in [e.state.value for e in ledger.events(intent_id)]
        and ctx.bridge_alive() is False
        and len(ctx.calls) == calls_before  # runner_boom never ran
    )
    return ScenarioResult(
        name="21. stale catalog_generation refuses without authorizing",
        passed=passed,
        evidence={
            "intent_id": intent_id,
            "itil_change_id": change_id,
            "dispatch_outcome": outcome.outcome if outcome else None,
            "ledger_state": ledger.current_state(intent_id).value,
        },
        note="a real, approved CAB vote is not enough once the ratified catalog has moved "
        "on: the dispatcher's own re-classification at dispatch time refuses the stale "
        "approval, AFTER the human-approval check, leaving the intent PROPOSED with no "
        "AUTHORIZED written (round 4: a stale catalog must not close an intent)",
    )


def scenario_duplicate_observations(ctx: DrillContext) -> ScenarioResult:
    """The same firing condition proposed twice in ONE pass: idempotent ledger
    identity + the cooldown gate prevents a double physical actuation."""
    slug = "s12-duplicate"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    duplicate = dict(proposal)  # same app/condition/object/action => same fingerprint
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
    )
    ledger = ctx.scenario_ledger(slug)
    calls_before = len(ctx.calls)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal, duplicate],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcomes = res["outcomes"]
    intent_ids = {o.get("intent_id") for o in outcomes}
    one_actuation = len(ctx.calls) - calls_before == 1
    same_intent = len(intent_ids) == 1 and None not in intent_ids
    second_suppressed = any("cooldown" in o.get("outcome", "") for o in outcomes)
    events = ledger.events(next(iter(intent_ids))) if same_intent else []
    no_duplicate_events = len(events) == len({e.sequence for e in events})
    passed = one_actuation and same_intent and second_suppressed and no_duplicate_events
    return ScenarioResult(
        name="8. duplicate observations in a single pass",
        passed=passed,
        evidence={
            "outcomes": outcomes,
            "distinct_intent_ids": list(intent_ids),
            "physical_restart_calls": len(ctx.calls) - calls_before,
            "ledger_event_count": len(events),
        },
        note="two identical proposals in one propose() return: same stable intent_id "
        "(action_ledger identity dedup), only ONE physical restart (the second is caught "
        "by the cooldown gate, not a second ledger transition)",
    )


def scenario_stale_evidence(ctx: DrillContext) -> ScenarioResult:
    """Stale, missing, unparseable, naive or future-dated evidence is refused
    BEFORE anything is written or run; fresh evidence still actuates.

    Gate 5 weak spot 1: this used to be a documented gap (a fabricated 2019
    ``evidence_ref`` was accepted verbatim). Each bad case runs through the real
    loop with a runner that would blow up on any physical call, and must leave no
    ledger intent and no execution-state entry behind.
    """
    slug = "s13-stale-evidence"
    ctx.inject_wedge()
    bad_cases: dict[str, dict[str, Any]] = {
        "seven_year_old_fabricated": {
            "evidence": {"source": "obs:fabricated", "observed_at": "2019-01-01T00:00:00Z"}
        },
        "free_text_only": {"evidence": None, "evidence_ref": f"obs:{_now_iso()}:free-text"},
        "unparseable": {"evidence": fresh_evidence("last tuesday")},
        "naive_no_timezone": {"evidence": fresh_evidence("2026-11-12T12:00:00")},
        "future_dated": {"evidence": fresh_evidence("2999-01-01T00:00:00Z")},
    }
    refused: dict[str, Any] = {}
    all_refused = True
    for label, extra in bad_cases.items():
        case_slug = f"{slug}-{label.replace('_', '-')}"
        proposal = ctx.wedge_proposal(**extra)
        if extra.get("evidence") is None:
            proposal.pop("evidence", None)
        ledger = ctx.scenario_ledger(case_slug)
        state = ctx.scenario_state(case_slug)
        calls_before = len(ctx.calls)
        res = _run_pass(
            ctx,
            propose=lambda b, r, p=proposal: [p],
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
            ),
            execution_state=state,
            ledger=ledger,
            decisions_dir=ctx.decisions_dir / case_slug,
        )
        outcome = res["outcomes"][0] if res["outcomes"] else {}
        this_refused = (
            str(outcome.get("outcome", "")).startswith("refused: ")
            and outcome.get("intent_id") is None
            and ledger.list_intents() == []
            and len(ctx.calls) == calls_before
            and not (state.path.exists() and json.loads(state.path.read_text())["actions"])
        )
        all_refused = all_refused and this_refused
        refused[label] = {"outcome": outcome.get("outcome"), "refused_cleanly": this_refused}

    control_slug = f"{slug}-fresh-control"
    control_stamp = fresh_evidence()
    control = ctx.wedge_proposal(evidence=control_stamp)
    control_ledger = ctx.scenario_ledger(control_slug)
    control_res = _run_pass(
        ctx,
        propose=lambda b, r: [control],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
        ),
        execution_state=ctx.scenario_state(control_slug),
        ledger=control_ledger,
        decisions_dir=ctx.decisions_dir / control_slug,
    )
    control_outcome = control_res["outcomes"][0] if control_res["outcomes"] else {}
    control_intent = control_outcome.get("intent_id")
    control_events = control_ledger.events(control_intent) if control_intent else []
    ledger_ref = control_events[0].evidence_ref if control_events else None
    control_ok = (
        control_outcome.get("outcome") == "verified"
        and ledger_ref is not None
        and ledger_ref.endswith("@" + control_stamp["observed_at"])
    )
    ctx.heal_wedge()
    return ScenarioResult(
        name="9. stale, missing or unparseable evidence is refused",
        passed=all_refused and control_ok,
        evidence={
            "refused_cases": refused,
            "fresh_control_outcome": control_outcome,
            "fresh_control_ledger_evidence_ref": ledger_ref,
        },
        note="five bad evidence shapes (7-year-old, free text only, unparseable, naive, "
        "future) each refused before any ledger intent, ITIL change, execution-state entry "
        "or physical call; the fresh control still actuates and the ledger records its "
        "timestamp (gate 5 weak spot 1, was a documented gap)",
    )


def scenario_auth_expiry_fails_closed(ctx: DrillContext) -> ScenarioResult:
    """A signed event, read back through a verifier that no longer trusts the
    signing key (revoked/expired), fails closed rather than silently passing."""
    slug = "s14-auth-expiry"
    if not ctx.ledger_signed:
        return ScenarioResult(
            name="10. auth expiry / key revocation fails closed",
            passed=False,
            evidence={},
            note="skipped: no real signer available to produce a signed event to revoke",
        )
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
    )
    ledger_dir = ctx.root / "atlas" / "action-ledger-scenarios" / slug
    ledger = ctx.scenario_ledger(slug)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    intent_id = outcome.get("intent_id")

    # A verifier that trusts NOTHING: the same shape a revoked/rotated/expired
    # key produces once its public key drops out of the trust roster.
    revoked_ledger = action_ledger.ActionLedger(
        ledger_dir, signer=None, verifier=lambda data, sig: False, require_signatures=False
    )
    raised = None
    try:
        revoked_ledger.events(intent_id)
    except ValueError as exc:
        raised = str(exc)
    passed = raised is not None and "signature" in raised
    return ScenarioResult(
        name="10. auth expiry / key revocation fails closed",
        passed=passed,
        evidence={"intent_id": intent_id, "raised": raised},
        note="the SAME on-disk signed events, read with a verifier that no longer trusts the "
        "signing key, raise instead of folding the (now-untrusted) state",
    )


def scenario_ledger_terminal_dead_end(ctx: DrillContext) -> ScenarioResult:
    """Card 0e98a570 (defect 1), FIXED: reuses ONE real-world condition across
    TWO separate PASSES (episodes), the second only after the first's lineage
    has reached a terminal state (VERIFIED) and cooldown has elapsed. Before
    the fix this dead-ended ('invalid action transition: verified ->
    authorized') because action_ledger.stable_intent_id() had no way to tell
    "still the same open episode" from "a later, separate occurrence". Now
    ActionLedger.resolve_occurrence() makes that call itself, from durable
    on-disk lineage state, and the second occurrence gets its OWN intent_id
    (occurrence=1) and actually re-actuates instead of dying in the ledger."""
    slug = "s15-ledger-terminal-dead-end"
    state = ctx.scenario_state(slug)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug

    # Occurrence 1: the wedge fires, self-heals cleanly, ledger reaches VERIFIED.
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    first = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
        ),
        execution_state=state,
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    first_outcome = first["outcomes"][0] if first["outcomes"] else {}
    intent_id = first_outcome.get("intent_id")
    first_state = ledger.current_state(intent_id) if intent_id else None
    first_occurrence = ledger.read_intent(intent_id).occurrence if intent_id else None

    # Time passes (cooldown elapses); the SAME real fault recurs later, in a
    # brand new pass/episode. Pre-fix this collided on the SAME intent_id
    # (stable_intent_id has no time component) and dead-ended; post-fix,
    # resolve_occurrence() sees occurrence 0's lineage is now terminal and
    # mints occurrence 1, a genuinely distinct intent.
    ctx.backdate_last_attempt(
        state, safety.action_fingerprint(proposal), state.cooldown_seconds + 1
    )
    ctx.inject_wedge()
    second_proposal = ctx.wedge_proposal()
    second = _run_pass(
        ctx,
        propose=lambda b, r: [second_proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
        ),
        execution_state=state,
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    second_outcome = second["outcomes"][0] if second["outcomes"] else {}
    second_intent_id = second_outcome.get("intent_id")
    second_state = ledger.current_state(second_intent_id) if second_intent_id else None
    second_occurrence = (
        ledger.read_intent(second_intent_id).occurrence if second_intent_id else None
    )
    ctx.heal_wedge()

    no_dead_end = "invalid action transition" not in second_outcome.get("outcome", "")
    distinct_intent = second_intent_id is not None and second_intent_id != intent_id
    second_actuated = second_outcome.get("outcome") == "verified"
    both_verified = first_state is action_ledger.ActionState.VERIFIED and (
        second_state is action_ledger.ActionState.VERIFIED
    )
    occurrences_advanced = first_occurrence == 0 and second_occurrence == 1
    passed = (
        no_dead_end
        and distinct_intent
        and second_actuated
        and both_verified
        and (occurrences_advanced)
    )
    return ScenarioResult(
        name="11. a genuinely later occurrence gets its own intent and re-actuates",
        passed=passed,
        evidence={
            "first_occurrence_outcome": first_outcome,
            "first_occurrence_final_ledger_state": str(first_state),
            "first_occurrence_field": first_occurrence,
            "second_occurrence_outcome": second_outcome,
            "second_occurrence_final_ledger_state": str(second_state),
            "second_occurrence_field": second_occurrence,
            "distinct_intent_id": distinct_intent,
        },
        note="occurrence 1 self-heals to VERIFIED; a later pass's genuinely separate firing "
        "of the SAME condition/object/action (cooldown elapsed) now gets its OWN intent_id "
        "(occurrence=1, via ActionLedger.resolve_occurrence) and re-actuates cleanly instead "
        "of dead-ending with 'invalid action transition: verified -> authorized' (card "
        "0e98a570, defect 1 fix)",
    )


def scenario_duplicate_observation_survives_terminal_mid_pass(ctx: DrillContext) -> ScenarioResult:
    """Card 0e98a570 regression guard: TWO identical proposals in the SAME
    pass (same episode) must still collapse onto ONE intent_id even though
    the first one's lineage reaches VERIFIED (a terminal state) before the
    second is examined, sequentially, later in the SAME planned-list loop.
    Without loop.py's per-pass occurrence memo, resolve_occurrence() would
    see the first's now-terminal lineage and (correctly, for a LATER pass)
    mint a fresh occurrence for the second -- which would be WRONG here,
    since both proposals are the same real-world observation within one
    episode. This is the dedup half of the fix that scenario 11 does not
    cover (scenario 11 uses two separate passes on purpose)."""
    slug = "s18-duplicate-survives-mid-pass-terminal"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    duplicate = dict(proposal)
    ledger = ctx.scenario_ledger(slug)
    calls_before = len(ctx.calls)
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal, duplicate],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
        ),
        execution_state=ctx.scenario_state(slug),
        ledger=ledger,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcomes = res["outcomes"]
    intent_ids = {o.get("intent_id") for o in outcomes}
    same_intent = len(intent_ids) == 1 and None not in intent_ids
    one_actuation = len(ctx.calls) - calls_before == 1
    only_intent_id = next(iter(intent_ids)) if same_intent else None
    occurrence = ledger.read_intent(only_intent_id).occurrence if only_intent_id else None
    events = ledger.events(only_intent_id) if only_intent_id else []
    no_duplicate_events = len(events) == len({e.sequence for e in events})
    passed = same_intent and one_actuation and occurrence == 0 and no_duplicate_events
    return ScenarioResult(
        name="14. same-episode duplicate still dedupes even though occurrence 1 finishes "
        "VERIFIED before occurrence 2 is examined",
        passed=passed,
        evidence={
            "outcomes": outcomes,
            "distinct_intent_ids": list(intent_ids),
            "occurrence_field": occurrence,
            "physical_restart_calls": len(ctx.calls) - calls_before,
        },
        note="two identical proposals in ONE pass: proposal 1 runs to completion (VERIFIED, "
        "a terminal state) before proposal 2 is even looked at, but loop.py's per-pass "
        "occurrence memo keeps proposal 2 on occurrence=0 (same intent_id) rather than "
        "resolve_occurrence() seeing the now-terminal lineage and minting occurrence=1 -- "
        "only ONE physical restart happens (the second is caught by cooldown, same as "
        "scenario 8)",
    )


def scenario_retry_budget_reachable_with_ledger(ctx: DrillContext) -> ScenarioResult:
    """Card 0e98a570 (defect 1's compounding effect on the circuit breaker),
    FIXED: retry_budget consecutive real failures on the --honor path WITH a
    lifecycle_ledger attached (the actual production shape: operator_seat/
    cli.py always attaches a ledger when --honor is on) and no rollback plan.
    Before the fix, ONE failure already escalated the intent to a terminal
    ledger state (ESCALATED), so the retry that safety.py's retry_budget=3 is
    supposed to allow failed immediately with 'invalid action transition:
    escalated -> authorized' instead of a genuine second attempt -- the
    circuit breaker's 3-strike budget was moot. Now each retry (after cooldown
    is backdated past, same as scenario 4) is a genuinely new occurrence with
    its own intent, so all three real attempts actually reach the actuator,
    and the circuit only opens on attempt 4 -- safety.py's execution_state is
    the limiting mechanism again, exactly as CR-9.1 documents it."""
    slug = "s19-retry-budget-with-ledger"
    proposal = ctx.wedge_proposal()
    fingerprint = safety.action_fingerprint(proposal)
    state = ctx.scenario_state(slug)
    ledger = ctx.scenario_ledger(slug)
    decisions_dir = ctx.decisions_dir / slug
    budget = state.retry_budget
    cooldown = state.cooldown_seconds

    attempt_outcomes = []
    for _attempt in range(budget):
        res = _run_pass(
            ctx,
            propose=lambda b, r: [proposal],
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_fail(), itil=None
            ),
            execution_state=state,
            ledger=ledger,
            decisions_dir=decisions_dir,
        )
        attempt_outcomes.append(res["outcomes"][0] if res["outcomes"] else {})
        ctx.backdate_last_attempt(state, fingerprint, cooldown + 1)

    calls_before = len(ctx.calls)
    final = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
        ),
        execution_state=state,
        ledger=ledger,
        decisions_dir=decisions_dir,
    )
    final_outcome = final["outcomes"][0] if final["outcomes"] else {}

    all_were_genuine_attempts = all(
        "actuation failed" in o.get("outcome", "") for o in attempt_outcomes
    )
    none_were_ledger_dead_ends = not any(
        "invalid action transition" in o.get("outcome", "") for o in attempt_outcomes
    )
    distinct_intents = len({o.get("intent_id") for o in attempt_outcomes}) == budget
    circuit_open = "execution suppressed: circuit-open" in final_outcome.get("outcome", "")
    passed = (
        all_were_genuine_attempts
        and none_were_ledger_dead_ends
        and distinct_intents
        and circuit_open
        and len(ctx.calls) == calls_before  # the circuit-open 4th never reached the actuator
    )
    state_entry = json.loads(state.path.read_text())["actions"].get(fingerprint, {})
    return ScenarioResult(
        name=f"15. retry_budget={budget} is genuinely reachable on --honor WITH a ledger "
        "attached",
        passed=passed,
        evidence={
            "attempt_outcomes": attempt_outcomes,
            "distinct_intent_ids": [o.get("intent_id") for o in attempt_outcomes],
            "final_outcome_after_cooldown_elapsed": final_outcome,
            "execution_state_entry": state_entry,
        },
        note="3 real actuation failures via the actual honor path, ledger attached, no "
        "rollback plan -- pre-fix, attempt 2 would have died with an 'invalid action "
        "transition' ledger error instead of a real attempt; here all 3 are genuine, each "
        "on its own occurrence/intent, and the circuit only opens on the 4th (safety.py's "
        "retry_budget, not the ledger, is the limiting mechanism)",
    )


def scenario_mid_run_freeze_race(ctx: DrillContext) -> ScenarioResult:
    """Freeze lands AFTER the loop's top-of-pass check but BEFORE actuation.

    Gate 5 weak spot 2: the refusal was always correct, but the pass result
    still said frozen=False and the refusal burned retry budget like a real
    fault. Now the loop re-reads the freeze at each decision point, so the pass
    reports the freeze, refuses without calling the actuator, and leaves the
    circuit breaker alone.
    """
    slug = "s16-freeze-race"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    state = ctx.scenario_state(slug)
    fingerprint = safety.action_fingerprint(proposal)
    state_before = (
        dict(json.loads(state.path.read_text())["actions"].get(fingerprint, {}))
        if state.path.exists()
        else {}
    )

    def propose_and_freeze(brief_dict, route):
        # The top-of-pass is_frozen() check has already passed by the time
        # propose() runs (loop.py checks freeze BEFORE observe/propose).
        _freeze(ctx.paths, reason="drill: simulated human kill-switch mid-pass")
        return [proposal]

    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
    )
    try:
        res = _run_pass(
            ctx,
            propose=propose_and_freeze,
            apply_fn=apply_fn,
            execution_state=state,
            ledger=ctx.scenario_ledger(slug),
            decisions_dir=ctx.decisions_dir / slug,
        )
    finally:
        _unfreeze(ctx.paths)  # drill root only; never touches production

    outcome = res["outcomes"][0] if res["outcomes"] else {}
    state_after = (
        json.loads(state.path.read_text())["actions"].get(fingerprint, {})
        if state.path.exists()
        else {}
    )
    consumed_retry_budget = int(state_after.get("consecutive_failures", 0)) > int(
        state_before.get("consecutive_failures", 0)
    )
    reports_freeze = (
        res.get("frozen") is True
        and res.get("frozen_mid_pass") is True
        and res.get("freeze_state") == "frozen"
        and outcome.get("freeze_state") == "frozen"
    )
    # runner_boom would have raised (and the loop recorded "failed: ...") had the
    # actuator been reached, so a "refused: ..." outcome proves it never was.
    refused_at_decision = str(outcome.get("outcome", "")).startswith("refused: ") and (
        "frozen" in str(outcome.get("outcome", ""))
    )
    return ScenarioResult(
        name="12. mid-run freeze race",
        passed=reports_freeze and refused_at_decision and not consumed_retry_budget,
        evidence={
            "pass_reported_frozen": res.get("frozen"),
            "pass_reported_frozen_mid_pass": res.get("frozen_mid_pass"),
            "pass_freeze_state": res.get("freeze_state"),
            "outcome_freeze_state": outcome.get("freeze_state"),
            "actuation_outcome": outcome,
            "retry_budget_consumed": consumed_retry_budget,
            "execution_state_before": state_before,
            "execution_state_after": state_after,
        },
        note="freeze is set inside propose(), i.e. AFTER the loop's own top-of-pass check "
        "already passed. The loop re-reads the freeze at the decision point, refuses before "
        "the actuator is called, reports frozen/frozen_mid_pass/freeze_state on the pass and "
        "the outcome, and does not count the refusal against the retry budget (gate 5 weak "
        "spot 2, was a documented gap)",
    )


def scenario_unverified_outcome_is_not_applied(ctx: DrillContext) -> ScenarioResult:
    """An action nothing re-observed reads "performed, unverified", never "applied".

    Gate 5 weak spot 3, the other half: with NO lifecycle ledger and no
    ``require_verified_actions`` the loop does not run the post-action
    verification at all, so the outcome must not claim more than "performed".
    """
    slug = "s22-unverified-outcome"
    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
    )
    res = _run_pass(
        ctx,
        propose=lambda b, r: [proposal],
        apply_fn=apply_fn,
        execution_state=ctx.scenario_state(slug),
        ledger=None,
        decisions_dir=ctx.decisions_dir / slug,
    )
    outcome = res["outcomes"][0] if res["outcomes"] else {}
    ctx.heal_wedge()
    word = str(outcome.get("outcome", ""))
    return ScenarioResult(
        name="22. unverified outcome is never reported as applied",
        passed=word == "performed, unverified",
        evidence={"outcome": outcome},
        note="no ledger and no require_verified_actions: the post-action re-observation never "
        "ran, so the outcome word is 'performed, unverified' (gate 5 weak spot 3)",
    )


def scenario_scheduler_overlap(ctx: DrillContext) -> ScenarioResult:
    """Two concurrent operator passes: the second is refused immediately by
    the non-blocking single-flight lock, never runs concurrently with the first."""
    slug = "s17-scheduler-overlap"
    proposal = ctx.wedge_proposal()
    state = ctx.scenario_state(slug)
    holding = threading.Event()
    release = threading.Event()

    def slow_propose(brief_dict, route):
        holding.set()
        release.wait(timeout=5.0)
        return []

    def first():
        return loop.run_once(
            ctx.paths,
            now_iso=_now_iso(),
            propose=slow_propose,
            explain=act_dispatch.merged_explain(),
            execute=False,
            emit=lambda _m: None,
            execution_state=state,
        )

    def second():
        holding.wait(timeout=5.0)
        try:
            return loop.run_once(
                ctx.paths,
                now_iso=_now_iso(),
                propose=lambda b, r: [proposal],
                explain=act_dispatch.merged_explain(),
                execute=False,
                emit=lambda _m: None,
                execution_state=state,
            )
        except RuntimeError as exc:
            return exc
        finally:
            release.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(first)
        fut2 = pool.submit(second)
        second_result = fut2.result(timeout=10.0)
        first_result = fut1.result(timeout=10.0)

    overlap_refused = isinstance(second_result, RuntimeError) and "another Atlas" in str(
        second_result
    )
    first_completed = isinstance(first_result, dict) and first_result.get("frozen") is False
    passed = overlap_refused and first_completed
    return ScenarioResult(
        name="13. scheduler overlap (single_flight non-blocking lock)",
        passed=passed,
        evidence={
            "second_pass_result": str(second_result),
            "first_pass_completed": first_completed,
        },
        note="second run_once() is attempted while the first still holds the lock inside "
        "its propose() callback; it is refused immediately rather than queued or racing",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_decisions(decisions_dir: Path) -> list[dict]:
    from . import decisions

    return decisions.list_pending(str(decisions_dir))


def _redact_event(event: action_ledger.ActionEvent) -> dict:
    payload = event.model_dump(mode="json")
    if payload.get("signature"):
        sig = payload["signature"]
        payload["signature"] = (
            sig[:24] + "...<redacted>..." + sig[-16:] if len(sig) > 48 else "<redacted>"
        )
    return payload


def _current_catalog_generation(ctx: DrillContext, app: str = "skchat") -> str:
    """The app's live OperatorApp generation in the drill root (earlier scenarios may have
    bumped it), so an intent written here is not stale for an unrelated reason."""
    record = store.read_spec(ctx.paths, "operatorapp", app)
    return str(record["generation"]) if record and record.get("generation") else "drill"


def scenario_evidence_age_uses_source_time_on_the_real_clock(ctx: DrillContext) -> ScenarioResult:
    """Review round 2, finding 1: freshness is the SOURCE data's age on the REAL clock.

    Round 1 stamped evidence from the pass clock and compared it with that same pass
    clock, so its age was always zero. This scenario drives the whole chain (real
    adapter, envelope, brief, proposer, loop) three ways: a real probe with a LYING
    pass clock still acts (the probe really is fresh); the same adapter with its
    source time stripped and the same lying pass clock is refused (no evidence); and
    a decision taken long after the observation is refused as stale.
    """
    slug = "s23-source-time"
    explain = act_dispatch.merged_explain()
    reply = json.dumps(
        [
            {
                "app": "skchat",
                "condition": "BridgeAlive",
                "action": "restart-telegram-bridge",
                "object": "telegram-bridge",
                "change_class": "normal",
                "rationale": "bridge poll heartbeat stale while the daemon is up",
            }
        ]
    )

    def unstamped(paths=None, now_iso=None):
        payload = skchat_adapter.skchat_observe()
        payload.pop("source_observed_at", None)
        return payload

    def chain(observe, runner, *, now_iso, case, clock=None):
        case_slug = f"{slug}-{case}"
        saved = loop.ADAPTERS
        loop.ADAPTERS = {"skchat": observe}
        try:
            return loop.run_once(
                ctx.paths,
                now_iso=now_iso,
                propose=lambda b, r: proposer.propose(b, explain, chat=lambda _p: reply),
                explain=explain,
                decisions_dir=str(ctx.decisions_dir / case_slug),
                apply_fn=act_dispatch.build_apply_fn(
                    ctx.paths, _now_iso(), runner=runner, itil=None
                ),
                execute=True,
                emit=lambda _m: None,
                execution_state=ctx.scenario_state(case_slug),
                lifecycle_ledger=ctx.scenario_ledger(case_slug),
                ledger_actor="atlas-p32-drill",
                clock=clock,
            )
        finally:
            loop.ADAPTERS = saved

    liar = "2019-01-01T00:00:00Z"
    ctx.inject_wedge()
    real = chain(skchat_adapter.observe, ctx.runner_success_and_heal(), now_iso=liar, case="real")
    real_out = real["outcomes"][0] if real["outcomes"] else {}
    real_ok = real_out.get("outcome") == "verified"

    ctx.inject_wedge()
    calls_before = len(ctx.calls)
    blind = chain(unstamped, ctx.runner_boom(), now_iso=liar, case="unstamped")
    blind_out = blind["outcomes"][0] if blind["outcomes"] else {}
    blind_ok = str(blind_out.get("outcome", "")).startswith("refused: evidence missing") and (
        len(ctx.calls) == calls_before
    )

    late = datetime.now(timezone.utc) + timedelta(seconds=safety_age_probe_seconds())
    slow = chain(
        skchat_adapter.observe,
        ctx.runner_boom(),
        now_iso=_now_iso(),
        case="slow",
        clock=lambda: late,
    )
    slow_out = slow["outcomes"][0] if slow["outcomes"] else {}
    slow_ok = "evidence stale" in str(slow_out.get("outcome", ""))
    ctx.heal_wedge()
    return ScenarioResult(
        name="23. evidence age is the source's age on the real clock",
        passed=real_ok and blind_ok and slow_ok,
        evidence={
            "real_probe_with_a_lying_pass_clock": real_out.get("outcome"),
            "source_time_stripped_with_a_lying_pass_clock": blind_out.get("outcome"),
            "decision_taken_an_hour_after_the_probe": slow_out.get("outcome"),
        },
        note="the pass clock can no longer launder evidence: a real probe is fresh whatever "
        "the pass clock says, an observation with no source time is refused, and age is "
        "measured at the decision point on the real clock (review round 2, finding 1)",
    )


def safety_age_probe_seconds() -> float:
    """A decision this long after the probe is well past any allowed evidence age."""
    from . import evidence

    return evidence.MAX_ALLOWED_AGE_SECONDS + 600.0


def _leftover_intent_scenario(ctx: DrillContext) -> ScenarioResult:
    """Review round 2, finding 2: a PROPOSED intent cannot be actuated later, unattended.

    Two intents are left PROPOSED the way the loop used to leave them: an
    auto-approved STANDARD change whose stored evidence is a day old, and one whose
    evidence is fresh but whose condition has since cleared. ``run_dispatch_pass``
    (what ``skoperator honor-pending`` runs) must refuse both without calling the
    actuator, and void them so they cannot return. A human-approved intent for a
    condition that is still firing still actuates, because the dispatcher re-observes
    rather than distrusting the human.
    """
    slug = "s24-leftover-intent"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    state = ctx.scenario_state(slug)
    created = datetime.now(timezone.utc)

    def standard_change():
        return itil.propose_change(
            title="restart telegram bridge",
            change_type="standard",
            risk="low",
            rollback_plan="restart again",
            created_by="atlas-p32-drill",
            tags=[
                "operator",
                dispatch.change_binding_tag(
                    "skchat", "restart-telegram-bridge", "telegram-bridge"
                ),
            ],
        )

    def leftover(change_id, target, evidence_stamp):
        intent = action_ledger.ActionIntent(
            condition_fingerprint=f"fp-{slug}-{change_id}",
            application="skchat",
            target_kind="CI",
            target_id=target,
            action="restart-telegram-bridge",
            catalog_generation=_current_catalog_generation(ctx),
            created_at=created,
            itil_change_id=change_id,
            condition_type="BridgeAlive",
        )
        ledger.create(intent, actor="atlas-p32-drill")
        for nxt, detail in (
            (action_ledger.ActionState.DIAGNOSED, {}),
            (
                action_ledger.ActionState.PROPOSED,
                {"disposition": "auto", "evidence": evidence_stamp},
            ),
        ):
            ledger.append(
                intent.intent_id, nxt, occurred_at=created, actor="atlas-p32-drill", detail=detail
            )
        return intent.intent_id

    day_old = fresh_evidence((created - timedelta(days=1)).strftime(_TS_FMT))
    ctx.inject_wedge()
    stale_id = leftover(standard_change().id, "telegram-bridge", day_old)
    calls_before = len(ctx.calls)

    def sweep():
        return dispatch.run_dispatch_pass(
            ctx.paths,
            ledger,
            itil,
            adapters={"skchat": skchat_adapter.observe},
            problem_types=loop.PROBLEM_WHEN_TRUE,
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=itil
            ),
            execution_state=state,
            now_iso=_now_iso(),
            actor="atlas-p32-drill",
            emit=lambda _m: None,
        )

    first = sweep()
    stale_refused = (
        bool(first)
        and first[0].outcome.startswith("refused: evidence stale")
        and ledger.current_state(stale_id) is action_ledger.ActionState.ESCALATED
    )

    ctx.heal_wedge()  # the condition cleared while the intent waited
    cleared_id = leftover(standard_change().id, "telegram-bridge", fresh_evidence())
    second = sweep()
    cleared_refused = (
        bool(second)
        and "no longer firing" in second[0].outcome
        and ledger.current_state(cleared_id) is action_ledger.ActionState.ESCALATED
    )
    third = sweep()  # nothing is left to dispatch, so nothing can act on a later pass
    nothing_left = third == []
    return ScenarioResult(
        name="24. a leftover PROPOSED intent is not actuated later",
        passed=stale_refused
        and cleared_refused
        and nothing_left
        and len(ctx.calls) == calls_before,
        evidence={
            "day_old_standard_intent": first[0].outcome if first else None,
            "cleared_condition_standard_intent": second[0].outcome if second else None,
            "third_sweep_outcomes": [o.outcome for o in third],
            "physical_calls": len(ctx.calls) - calls_before,
        },
        note="the dispatcher demands fresh stored evidence for an auto-approved change and "
        "re-observes the condition before acting; what it refuses is voided so it cannot "
        "return on a later pass (review round 2, finding 2)",
    )


def scenario_leftover_intent_is_not_actuated_later(ctx: DrillContext) -> ScenarioResult:
    """Run the leftover-intent scenario with the bridge restart ratified as a standard
    action, so the intents are the auto-approved kind this scenario is about (without
    it the dispatcher would stop at "classification hardened" before the checks)."""
    saved_catalog = policy.RATIFIED_STANDARD_CATALOG
    policy.RATIFIED_STANDARD_CATALOG = saved_catalog | {"restart-telegram-bridge"}
    try:
        return _leftover_intent_scenario(ctx)
    finally:
        policy.RATIFIED_STANDARD_CATALOG = saved_catalog


def scenario_escalated_proposal_needs_a_human_and_keeps_its_decision(
    ctx: DrillContext,
) -> ScenarioResult:
    """Review round 3: an ESCALATED proposal never gets an auto-approving change.

    The old loop typed the ITIL change from the proposal's class and ignored its
    disposition, so a standard-class proposal the planner escalated (here: its target
    does not resolve) got a ``standard`` change that auto-approves at the fold, and a
    later ``run_dispatch_pass`` ran it with no human vote. Now it gets a CAB-gated
    change, the sweep leaves it pending (and un-voided, so the parked decision still
    works), and only a real CAB vote lets it run, after a fresh re-observation.
    """
    slug = "s25-escalated-needs-human"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    state = ctx.scenario_state(slug)
    explain = act_dispatch.merged_explain()
    for entry in explain["actions"]:
        if entry["name"] == "restart-telegram-bridge":
            entry["standard"] = True
    saved_catalog = policy.RATIFIED_STANDARD_CATALOG
    policy.RATIFIED_STANDARD_CATALOG = saved_catalog | {"restart-telegram-bridge"}
    try:
        ctx.inject_wedge()
        proposal = ctx.wedge_proposal()
        res = loop.run_once(
            ctx.paths,
            now_iso=_now_iso(),
            propose=lambda b, r: [proposal],
            explain=explain,
            decisions_dir=str(ctx.decisions_dir / slug),
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=itil
            ),
            execute=True,
            emit=lambda _m: None,
            execution_state=state,
            lifecycle_ledger=ledger,
            ledger_actor="atlas-p32-drill",
            itil=itil,
            target_known=lambda p: False,
            catalog_generation=_current_catalog_generation(ctx),
        )
        outcome = res["outcomes"][0]
        intent_id = outcome["intent_id"]
        change_id = ledger.read_intent(intent_id).itil_change_id
        change = next(c for c in itil.list_changes() if c.id == change_id)

        def sweep(runner):
            return dispatch.run_dispatch_pass(
                ctx.paths,
                ledger,
                itil,
                adapters={"skchat": skchat_adapter.observe},
                problem_types=loop.PROBLEM_WHEN_TRUE,
                apply_fn=act_dispatch.build_apply_fn(
                    ctx.paths, _now_iso(), runner=runner, itil=itil
                ),
                execution_state=state,
                now_iso=_now_iso(),
                actor="atlas-p32-drill",
                emit=lambda _m: None,
            )

        calls_before = len(ctx.calls)
        first = sweep(ctx.runner_boom())
        pending = (
            outcome["outcome"] == "escalated (parked for approval)"
            and change.change_type.value == "normal"
            and change.status.value == "proposed"
            and bool(first)
            and first[0].outcome.startswith("pending")
            and ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
            and len(ctx.calls) == calls_before
        )
        itil.submit_cab_vote(
            change_id,
            agent="capauth:drill-human@scratch",
            decision="approved",
            subject="capauth:drill-human@scratch",
            subject_role="approver",
        )
        second = sweep(ctx.runner_success_and_heal())
        acted_after_human = (
            bool(second)
            and second[0].outcome == "verified"
            and len(ctx.calls) == calls_before + 1
            and ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED
        )
    finally:
        policy.RATIFIED_STANDARD_CATALOG = saved_catalog
        ctx.heal_wedge()
    return ScenarioResult(
        name="25. an escalated proposal needs a human and keeps its decision",
        passed=pending and acted_after_human,
        evidence={
            "escalated_outcome": outcome["outcome"],
            "change_type": change.change_type.value,
            "change_status_before_vote": change.status.value,
            "sweep_before_the_human": first[0].outcome if first else None,
            "sweep_after_the_human": second[0].outcome if second else None,
        },
        note="the escalate lane gets a CAB-gated change, so nothing runs without a human and "
        "the pending intent is never voided; after the vote it runs once, after a fresh "
        "re-observation (review round 3, findings A and B)",
    )


def scenario_approval_authorizes_one_thing_once(ctx: DrillContext) -> ScenarioResult:
    """Review round 4: a human approval is read from the VOTES, authorizes ONE intent, once.

    Three holes, closed together. (a) "Human-approved" was inferred from a MISSING
    ``auto-approved`` marker, but the fold only writes that marker while a change is
    still PROPOSED, so a standard change that went approved, scheduled, unscheduled
    read as human: here an ESCALATED intent behind such a change must stay pending.
    (b) The change was never bound to the intent: a change a human approved for one
    action also ran an intent for ANOTHER action. (c) The approval was never spent: a
    second intent on the same change ran again. Now only a qualifying human vote counts,
    the change is bound to (app, action, target), and a verified run moves it to
    ``deployed``.
    """
    slug = "s26-one-approval-once"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    state = ctx.scenario_state(slug)
    created = datetime.now(timezone.utc)
    bind = dispatch.change_binding_tag("skchat", "restart-telegram-bridge", "telegram-bridge")
    voter = "capauth:drill-human@scratch"

    def draft(change_type: str):
        return itil.propose_change(
            title="restart telegram bridge",
            change_type=change_type,
            risk="low",
            rollback_plan="restart again",
            created_by="atlas-p32-drill",
            tags=["operator", bind],
        )

    def intent(change_id: str, action: str, lane: str, tag: str) -> str:
        core = action_ledger.ActionIntent(
            condition_fingerprint=f"fp-{slug}-{tag}",
            application="skchat",
            target_kind="CI",
            target_id="telegram-bridge",
            action=action,
            catalog_generation=_current_catalog_generation(ctx),
            created_at=created,
            itil_change_id=change_id,
            condition_type="BridgeAlive",
        )
        ledger.create(core, actor="atlas-p32-drill")
        for nxt, detail in (
            (action_ledger.ActionState.DIAGNOSED, {}),
            (
                action_ledger.ActionState.PROPOSED,
                {"disposition": lane, "evidence": fresh_evidence()},
            ),
        ):
            ledger.append(
                core.intent_id, nxt, occurred_at=created, actor="atlas-p32-drill", detail=detail
            )
        return core.intent_id

    def sweep(runner):
        return dispatch.run_dispatch_pass(
            ctx.paths,
            ledger,
            itil,
            adapters={"skchat": skchat_adapter.observe},
            problem_types=loop.PROBLEM_WHEN_TRUE,
            apply_fn=act_dispatch.build_apply_fn(ctx.paths, _now_iso(), runner=runner, itil=itil),
            execution_state=state,
            now_iso=_now_iso(),
            actor="atlas-p32-drill",
            emit=lambda _m: None,
        )

    def outcome_of(outcomes, intent_id):
        return next((o.outcome for o in outcomes if o.intent_id == intent_id), None)

    ctx.inject_wedge()
    # (a) a standard change approved, scheduled, unscheduled: no marker, still not human.
    standard = draft("standard")
    when = _now_iso()
    for kind, extra in (
        ("schedule", {"window_start": when, "window_end": when, "asap": True}),
        ("unschedule", {"note": "rescheduled"}),
    ):
        itil._append_event(itil.changes_dir, standard.id, voter, kind, **extra)
    escalated_id = intent(standard.id, "restart-telegram-bridge", "escalate", "esc")
    # (b) one human approval, bound to the bridge restart; another action rides it.
    approved = draft("normal")
    itil.submit_cab_vote(
        approved.id, agent=voter, decision="approved", subject=voter, subject_role="approver"
    )
    other_id = intent(approved.id, "restart-daemon", "escalate", "other")
    calls_before = len(ctx.calls)
    first = sweep(ctx.runner_boom())
    escalated_pending = (outcome_of(first, escalated_id) or "").startswith("pending") and (
        ledger.current_state(escalated_id) is action_ledger.ActionState.PROPOSED
    )
    other_refused = "not bound" in (outcome_of(first, other_id) or "")
    # (c) the bound intent runs once; the approval is then spent.
    good_id = intent(approved.id, "restart-telegram-bridge", "escalate", "good")
    second = sweep(ctx.runner_success_and_heal())
    ran_once = outcome_of(second, good_id) == "verified" and len(ctx.calls) == calls_before + 1
    spent = next(c for c in itil.list_changes() if c.id == approved.id).status.value == "deployed"
    ctx.inject_wedge()
    again_id = intent(approved.id, "restart-telegram-bridge", "escalate", "again")
    third = sweep(ctx.runner_boom())
    reuse_refused = (outcome_of(third, again_id) or "").startswith("pending: ITIL change status=")
    no_extra_call = len(ctx.calls) == calls_before + 1
    ctx.heal_wedge()
    return ScenarioResult(
        name="26. an approval authorizes one thing, once, and only a vote is a human",
        passed=escalated_pending
        and other_refused
        and ran_once
        and spent
        and reuse_refused
        and no_extra_call,
        evidence={
            "rescheduled_standard_change_escalated_intent": outcome_of(first, escalated_id),
            "other_action_on_the_same_change": outcome_of(first, other_id),
            "bound_intent": outcome_of(second, good_id),
            "change_status_after_the_run": "deployed" if spent else "not deployed",
            "second_intent_on_the_spent_change": outcome_of(third, again_id),
            "physical_calls": len(ctx.calls) - calls_before,
        },
        note="human approval is derived from votes (not a missing marker), the change is "
        "bound to its (app, action, target), and a verified run spends it (round 4, "
        "findings 1 and 2)",
    )


class _SlowChangeITIL:
    """An ITILManager whose ``update_change`` is slow, to make two dispatchers overlap."""

    def __init__(self, inner: ITILManager, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def update_change(self, *args: Any, **kwargs: Any) -> Any:
        time.sleep(self._delay)
        return self._inner.update_change(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def scenario_one_approval_one_actuation_per_episode(ctx: DrillContext) -> ScenarioResult:
    """Review round 5: the spend is a compare-and-swap, an approval covers one episode, a
    crash never causes a re-run.

    (a) Two dispatchers, two intents, ONE approved change, overlapped on purpose: exactly one
    may actuate and the ITIL record must end ``deployed``, agreeing with the ledger (it used
    to end ``failed`` while the ledger said verified, with the actuator run twice). (b) When
    the condition clears before dispatch the approved intent is closed and its change
    retired, so a recurrence needs a new vote. (c) An intent a crash stranded in EXECUTING is
    escalated with an alert and never re-run.
    """
    slug = "s27-one-approval-per-episode"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    created = datetime.now(timezone.utc)
    bind = dispatch.change_binding_tag("skchat", "restart-telegram-bridge", "telegram-bridge")
    voter = "capauth:drill-human@scratch"

    def approved_change():
        change = itil.propose_change(
            title="restart telegram bridge",
            change_type="normal",
            risk="low",
            rollback_plan="restart again",
            created_by="atlas-p32-drill",
            tags=["operator", bind],
        )
        itil.submit_cab_vote(
            change.id, agent=voter, decision="approved", subject=voter, subject_role="approver"
        )
        return change

    def intent(change_id: str, tag: str) -> str:
        core = action_ledger.ActionIntent(
            condition_fingerprint=f"fp-{slug}-{tag}",
            application="skchat",
            target_kind="CI",
            target_id="telegram-bridge",
            action="restart-telegram-bridge",
            catalog_generation=_current_catalog_generation(ctx),
            created_at=created,
            itil_change_id=change_id,
            condition_type="BridgeAlive",
        )
        ledger.create(core, actor="atlas-p32-drill")
        for nxt, detail in (
            (action_ledger.ActionState.DIAGNOSED, {}),
            (action_ledger.ActionState.PROPOSED, {"disposition": "escalate"}),
        ):
            ledger.append(
                core.intent_id, nxt, occurred_at=created, actor="atlas-p32-drill", detail=detail
            )
        return core.intent_id

    def dispatch_one(manager, intent_id, apply_fn):
        return dispatch.dispatch_intent(
            ctx.paths,
            ledger,
            manager,
            intent_id,
            adapters={"skchat": skchat_adapter.observe},
            problem_types=loop.PROBLEM_WHEN_TRUE,
            apply_fn=apply_fn,
            now_iso=_now_iso(),
            actor="atlas-p32-drill",
            emit=lambda _m: None,
        )

    # (a) the race
    ctx.inject_wedge()
    shared = approved_change()
    first, second = intent(shared.id, "a"), intent(shared.id, "b")
    slow = _SlowChangeITIL(itil, delay=0.3)
    real_apply = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=itil
    )
    actuations: list[str] = []
    guard = threading.Lock()

    def counting_apply(prop: dict, classification: dict) -> Any:
        with guard:
            actuations.append(prop["action"])
        return real_apply(prop, classification)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result(timeout=60)
            for f in [pool.submit(dispatch_one, slow, i, counting_apply) for i in (first, second)]
        ]
    race_status = next(c for c in itil.list_changes() if c.id == shared.id).status.value
    race_ok = len(actuations) == 1 and race_status == "deployed"

    # (b) the episode clears before dispatch
    ctx.heal_wedge()
    cleared_change = approved_change()
    cleared_id = intent(cleared_change.id, "cleared")
    cleared = dispatch_one(itil, cleared_id, lambda p, c: (_ for _ in ()).throw(AssertionError))
    retired = next(c for c in itil.list_changes() if c.id == cleared_change.id).status.value
    cleared_ok = (
        "condition cleared before dispatch" in cleared.outcome
        and ledger.current_state(cleared_id) is action_ledger.ActionState.ESCALATED
        and retired in ("rejected", "closed")
    )

    # (c) a crash stranded an intent in EXECUTING
    ctx.inject_wedge()
    stranded_change = approved_change()
    stranded_id = intent(stranded_change.id, "stranded")
    long_ago = created - timedelta(minutes=30)
    for state in (action_ledger.ActionState.AUTHORIZED, action_ledger.ActionState.EXECUTING):
        ledger.append(stranded_id, state, occurred_at=long_ago, actor="crashed")
    itil.update_change(stranded_change.id, "crashed", new_status="implementing", note="x")
    calls_before = len(ctx.calls)
    alerts: list[str] = []
    swept = dispatch.escalate_stranded(
        ledger, itil, now_iso=_now_iso(), actor="atlas-p32-drill", emit=alerts.append
    )
    stranded_ok = (
        any(o.intent_id == stranded_id for o in swept)
        and ledger.current_state(stranded_id) is action_ledger.ActionState.ESCALATED
        and any("ALERT" in a and stranded_id in a for a in alerts)
        and len(ctx.calls) == calls_before
    )
    ctx.heal_wedge()
    return ScenarioResult(
        name="27. one approval, one actuation, per episode",
        passed=race_ok and cleared_ok and stranded_ok,
        evidence={
            "race_actuations": len(actuations),
            "race_outcomes": sorted(r.outcome.split(":")[0] for r in results),
            "itil_status_after_the_race": race_status,
            "cleared_episode": cleared.outcome,
            "retired_change_status": retired,
            "stranded_outcome": next(
                (o.outcome for o in swept if o.intent_id == stranded_id), None
            ),
        },
        note="the spend is a compare-and-swap under a per-change lock, a human approval is "
        "closed with its episode, and a crash-stranded intent is escalated, never re-run "
        "(round 5, findings 1, 2 and 5)",
    )


def scenario_single_actuator_live_dispatch_and_future_votes(ctx: DrillContext) -> ScenarioResult:
    """Review round 6: one actuator node, node-local locks, live dispatches left alone, and a
    future-dated vote cannot buy time.

    (a) The REAL single-actuator-node rule: with no node designated (the default), or another
    node designated, the loop and ``run_dispatch_pass`` refuse to actuate; designating this node
    opens it. (b) The per-change lock lives in the node-local state directory, never beside the
    (synced) ledger. (c) A dispatch whose actuation outlasts the stranded timeout is NOT
    escalated by a sweeper (its lifecycle events are stamped from the clock at write time and
    it holds its change lock), and its fix is not rolled back. (d) A vote dated five days ahead
    closes the intent and retires the change instead of waiting to be honored.
    """
    slug = "s28-actuator-node-live-dispatch"
    itil = ITILManager(ctx.itil_home)
    ledger = ctx.scenario_ledger(slug)
    created = datetime.now(timezone.utc)
    bind = dispatch.change_binding_tag("skchat", "restart-telegram-bridge", "telegram-bridge")
    voter = "capauth:drill-human@scratch"

    def approved(decided_at: str | None = None):
        change = itil.propose_change(
            title="restart telegram bridge",
            change_type="normal",
            risk="low",
            rollback_plan="restart again",
            created_by="atlas-p32-drill",
            tags=["operator", bind],
        )
        itil.submit_cab_vote(
            change.id, agent=voter, decision="approved", subject=voter, subject_role="approver"
        )
        if decided_at:
            path = itil.cab_dir / f"{change.id}-{voter}.json"
            data = json.loads(path.read_text())
            data["decided_at"] = decided_at
            path.write_text(json.dumps(data))
        return change

    def intent(change_id: str, tag: str) -> str:
        core = action_ledger.ActionIntent(
            condition_fingerprint=f"fp-{slug}-{tag}",
            application="skchat",
            target_kind="CI",
            target_id="telegram-bridge",
            action="restart-telegram-bridge",
            catalog_generation=_current_catalog_generation(ctx),
            created_at=created,
            itil_change_id=change_id,
            condition_type="BridgeAlive",
        )
        ledger.create(core, actor="atlas-p32-drill")
        for nxt, detail in (
            (action_ledger.ActionState.DIAGNOSED, {}),
            (action_ledger.ActionState.PROPOSED, {"disposition": "escalate"}),
        ):
            ledger.append(
                core.intent_id, nxt, occurred_at=created, actor="atlas-p32-drill", detail=detail
            )
        return core.intent_id

    def sweep(runner, **kwargs):
        return dispatch.run_dispatch_pass(
            ctx.paths,
            ledger,
            itil,
            adapters={"skchat": skchat_adapter.observe},
            problem_types=loop.PROBLEM_WHEN_TRUE,
            apply_fn=act_dispatch.build_apply_fn(ctx.paths, _now_iso(), runner=runner, itil=itil),
            now_iso=_now_iso(),
            actor="atlas-p32-drill",
            emit=lambda _m: None,
            **kwargs,
        )

    # (a) the real single-actuator-node rule
    saved_env = {k: os.environ.get(k) for k in (node_local.ENV_ACTUATOR, "XDG_CONFIG_HOME")}
    os.environ.pop(node_local.ENV_ACTUATOR, None)
    os.environ["XDG_CONFIG_HOME"] = str(ctx.root / "empty-config")
    node_local.actuator_refusal = node_local.real_actuator_refusal
    ctx.inject_wedge()
    waiting = intent(approved().id, "waiting")
    calls_before = len(ctx.calls)
    try:
        undesignated = node_local.actuator_refusal()
        proposal = ctx.wedge_proposal()
        refused_loop = _run_pass(
            ctx,
            propose=lambda b, r: [proposal],
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
            ),
            execution_state=ctx.scenario_state(slug),
            ledger=ctx.scenario_ledger(slug + "-loop"),
            decisions_dir=ctx.decisions_dir / slug,
        )["outcomes"][0]["outcome"]
        refused_pass = sweep(ctx.runner_boom())
        os.environ[node_local.ENV_ACTUATOR] = "some-other-node"
        other_node = node_local.actuator_refusal()
        os.environ[node_local.ENV_ACTUATOR] = self_node_name()
        designated_here = node_local.actuator_refusal()
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        node_local.actuator_refusal = lambda: None
    rule_ok = (
        undesignated is not None
        and "no actuator node" in undesignated
        and refused_loop.startswith("refused: no actuator node")
        and refused_pass == []
        and other_node is not None
        and designated_here is None
        and len(ctx.calls) == calls_before
        and ledger.current_state(waiting) is action_ledger.ActionState.PROPOSED
    )

    # (c) a live dispatch that outlasts the stranded timeout is left alone
    live_change = approved()
    live_id = intent(live_change.id, "live")
    real_apply = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=itil
    )
    swept: list[list[Any]] = []
    rollbacks: list[dict] = []

    def long_apply(prop: dict, classification: dict) -> Any:
        swept.append(
            dispatch.escalate_stranded(
                ledger,
                itil,
                now_iso=_now_iso(),
                clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=11),
            )
        )
        return real_apply(prop, classification)

    live = dispatch.dispatch_intent(
        ctx.paths,
        ledger,
        itil,
        live_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=long_apply,
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
        now_iso=_now_iso(),
        actor="atlas-p32-drill",
        emit=lambda _m: None,
    )
    live_ok = (
        live.outcome == "verified"
        and swept == [[]]
        and rollbacks == []
        and ledger.current_state(live_id) is action_ledger.ActionState.VERIFIED
    )

    # (b) the lock lives in node-local state, not beside the ledger
    lock_file = node_local.state_dir() / "atlas" / "change-locks" / f"{live_change.id}.lock"
    locks_ok = lock_file.is_file() and not (ledger.root / "change-locks").exists()

    # (d) a vote dated five days ahead closes the intent instead of waiting
    ctx.inject_wedge()
    future_change = approved(decided_at=(created + timedelta(days=5)).isoformat())
    future_id = intent(future_change.id, "future")
    future = dispatch.dispatch_intent(
        ctx.paths,
        ledger,
        itil,
        future_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=act_dispatch.build_apply_fn(
            ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=itil
        ),
        now_iso=_now_iso(),
        actor="atlas-p32-drill",
        emit=lambda _m: None,
        max_approval_age_s=dispatch.MAX_ALLOWED_APPROVAL_AGE_SECONDS,
    )
    retired = next(c for c in itil.list_changes() if c.id == future_change.id).status.value
    future_ok = (
        "dated in the future" in future.outcome
        and ledger.current_state(future_id) is action_ledger.ActionState.ESCALATED
        and retired in ("rejected", "closed")
    )
    ctx.heal_wedge()
    return ScenarioResult(
        name="28. one actuator node, node-local locks, live dispatches left alone",
        passed=rule_ok and live_ok and locks_ok and future_ok,
        evidence={
            "no_designation": undesignated,
            "loop_without_designation": refused_loop,
            "other_node_designated": other_node,
            "this_node_designated": designated_here,
            "live_dispatch": live.outcome,
            "sweeper_during_the_live_dispatch": swept,
            "rollbacks": len(rollbacks),
            "lock_file_node_local": str(lock_file),
            "future_dated_vote": future.outcome,
        },
        note="actuation needs a designated actuator node (default none), the change lock is "
        "node-local, a live dispatch is never swept as stranded or rolled back, and a "
        "future-dated vote is closed (round 6, findings 1, 2 and 4)",
    )


def scenario_actuator_rule_decided_once_and_decisions_not_shadowed(
    ctx: DrillContext,
) -> ScenarioResult:
    """Review round 8: the actuator rule is decided ONCE per pass, and a non-actuator's
    unbound decision cannot shadow the actuator's bound one.

    (a) The rule refuses on its first call and answers "designated" on every later one (a
    designation file written mid-pass, or a failed read). The loop drops its ledger wiring
    for the refused pass; a second evaluation used to flip it back and let the proposal fall
    into the inline lane (a systemctl restart with no ledger intent). Now the pass refuses
    and runs nothing. (b) The decisions store is replicated and its ids are content-based: a
    non-actuator parks the decision with no ITIL change, and the actuator's bound park used
    to be skipped, leaving a decision that could never be approved. A pending unbound record
    is now upgraded to the bound option (never the other way, never a resolved record).
    """
    slug = "s29-one-evaluation-no-shadowing"
    calls = {"n": 0}

    def flapping() -> str | None:
        calls["n"] += 1
        return "this node is not the designated actuator" if calls["n"] == 1 else None

    ctx.inject_wedge()
    proposal = ctx.wedge_proposal()
    ledger = ctx.scenario_ledger(slug)
    calls_before = len(ctx.calls)
    node_local.actuator_refusal = flapping
    try:
        res = _run_pass(
            ctx,
            propose=lambda b, r: [proposal],
            apply_fn=act_dispatch.build_apply_fn(
                ctx.paths, _now_iso(), runner=ctx.runner_boom(), itil=None
            ),
            execution_state=ctx.scenario_state(slug),
            ledger=ledger,
            decisions_dir=ctx.decisions_dir / slug,
        )
    finally:
        node_local.actuator_refusal = lambda: None
    outcome = res["outcomes"][0]["outcome"]
    once_ok = (
        outcome.startswith("refused: ")
        and "not the designated actuator" in outcome
        and calls["n"] == 1
        and len(ctx.calls) == calls_before
        and ledger.list_intents() == []
    )

    store_dir = ctx.decisions_dir / (slug + "-store")
    unbound = {"action": "restart-telegram-bridge", "object": "telegram-bridge"}
    bound = {**unbound, "itil_change_id": "chg-drill"}
    decisions.park(store_dir, [unbound], decision_id="d-shadow", created_iso=_now_iso())
    upgraded = decisions.park(store_dir, [bound], decision_id="d-shadow", created_iso=_now_iso())
    downgraded = decisions.park(
        store_dir, [unbound], decision_id="d-shadow", created_iso=_now_iso()
    )
    shadow_ok = upgraded["options"] == [bound] and downgraded["options"] == [bound]
    ctx.heal_wedge()
    return ScenarioResult(
        name="29. the actuator rule is decided once; decisions are not shadowed",
        passed=once_ok and shadow_ok,
        evidence={
            "outcome_of_the_flapping_pass": outcome,
            "rule_evaluations_in_the_pass": calls["n"],
            "systemctl_calls": len(ctx.calls) - calls_before,
            "decision_after_the_bound_park": upgraded["options"],
            "decision_after_a_later_unbound_park": downgraded["options"],
        },
        note="one evaluation per pass so a refusal cannot flip into an inline actuation, and "
        "a pending unbound decision is upgraded to the actuator's bound option (round 8, "
        "findings 1 and 2)",
    )


SCENARIOS: list[Callable[[DrillContext], ScenarioResult]] = [
    scenario_baseline_self_heal,
    scenario_performed_false_actdispatch_gate,
    scenario_performed_false_loop_gate,
    scenario_performed_proof_omitted,
    scenario_cooldown_blocks_retry,
    scenario_circuit_breaker_opens,
    scenario_postverification_blocks,
    scenario_rollback_executes,
    scenario_escalation_no_rollback,
    scenario_escalation_rollback_fails,
    scenario_signed_ledger_and_itil,
    scenario_duplicate_observations,
    scenario_stale_evidence,
    scenario_auth_expiry_fails_closed,
    scenario_ledger_terminal_dead_end,
    scenario_duplicate_observation_survives_terminal_mid_pass,
    scenario_retry_budget_reachable_with_ledger,
    scenario_mid_run_freeze_race,
    scenario_scheduler_overlap,
    scenario_dispatcher_full_arc,
    scenario_dispatcher_unapproved_never_actuates,
    scenario_dispatcher_unlinked_decision_never_actuates,
    scenario_dispatcher_stale_catalog_generation_escalates,
    scenario_unverified_outcome_is_not_applied,
    scenario_evidence_age_uses_source_time_on_the_real_clock,
    scenario_leftover_intent_is_not_actuated_later,
    scenario_escalated_proposal_needs_a_human_and_keeps_its_decision,
    scenario_approval_authorizes_one_thing_once,
    scenario_one_approval_one_actuation_per_episode,
    scenario_single_actuator_live_dispatch_and_future_votes,
    scenario_actuator_rule_decided_once_and_decisions_not_shadowed,
]


def run_all(root: Path | str | None = None, *, keep_root: bool = True) -> DrillReport:
    """Run the full P3.2 fault-injection drill against a freshly-claimed,
    guarded, throwaway fleet root and return every scenario's verdict.

    Args:
        root: Candidate drill root (defaults to :func:`default_candidate_root`,
            i.e. ``SKFLEET_ROOT`` or ``/tmp/atlas-drill-fleet``). Always run
            through :func:`guard_drill_root` before anything is written.
        keep_root: When True (the default) the drill tree is left on disk so
            its ledger/execution-state can be inspected afterward. The CLI
            script controls this; tests may pass False to self-clean.

    Raises:
        skcapstone.fleet.drill.UnsafeDrillRootError: the resolved root is (or
            is inside) the live sovereign tree, or exists without this
            harness's own marker.
    """
    candidate = root if root is not None else default_candidate_root()
    guarded = guard_drill_root(candidate)
    resolved = fleet_drill.claim_root(guarded)
    started = _now_iso()
    ctx = build_context(resolved)
    ctx.patch_daemon_probe(ready=True, auth=True, calling=True)
    ctx.patch_env(
        SKCOMMS_OUTBOX_DIR=str(ctx.outbox_dir),
        SKCHAT_BRIDGE_HEARTBEAT=str(ctx.heartbeat_path),
        # Node-local state (the per-change locks) stays inside the guarded drill root.
        SKCAPSTONE_LOCAL_STATE_DIR=str(ctx.root / "node-local-state"),
    )
    # The drill runs against its own throwaway fleet, so the single-actuator-node rule is
    # opened for it (scenario 28 restores the real rule to prove it refuses).
    previous_gate = node_local.actuator_refusal  # captured at call time, never at import
    node_local.actuator_refusal = lambda: None
    results: list[ScenarioResult] = []
    try:
        for scenario in SCENARIOS:
            try:
                results.append(scenario(ctx))
            except Exception as exc:  # noqa: BLE001 - one scenario's bug must not hide the rest
                results.append(
                    ScenarioResult(
                        name=scenario.__name__,
                        passed=False,
                        evidence={"exception": repr(exc)},
                        note="HARNESS ERROR: scenario raised unexpectedly "
                        "(not a documented refusal)",
                    )
                )
        # Safety net: never leave the drill tree frozen or with a corrupted
        # config, even if a scenario above threw before reaching its own cleanup.
        if store.is_frozen(ctx.paths):
            _unfreeze(ctx.paths)
    finally:
        node_local.actuator_refusal = previous_gate
        ctx.restore_daemon_probe()
        ctx.restore_env()
    finished = _now_iso()
    if not keep_root:
        fleet_drill.require_owned_root(resolved)
        import shutil

        shutil.rmtree(resolved)
    return DrillReport(root=resolved, results=results, started_at=started, finished_at=finished)
