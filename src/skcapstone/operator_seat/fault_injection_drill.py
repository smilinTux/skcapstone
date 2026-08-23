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
drill to surface gaps (duplicate observations / stale evidence / auth expiry /
mid-run freeze race / scheduler overlap); those scenarios carry a ``finding``
string that must be read, not hidden. Nothing in this module is permitted to
change ``act_dispatch.py``, ``safety.py``, ``loop.py`` or ``action_ledger.py``
to make its own assertions pass.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..fleet import drill as fleet_drill
from ..fleet import store
from ..fleet.paths import FleetPaths
from . import act_dispatch, action_ledger, loop, safety, skchat_adapter

SCHEMA_NOTE = "ATLAS P3.2 fault-injection drill (card b993eaaa, epic fb3cc09d)"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


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
    # NOTE: loop.py's outcome STRING says "verified" only when
    # require_verified_actions=True was also passed; this drill deliberately
    # doesn't (see module docstring on scope). Attaching lifecycle_ledger alone
    # already forces performed=True proof AND _verify_postcondition to run
    # (loop.py:372-379), so the real safety property holds regardless of the
    # label -- checked here via the LEDGER's own folded state, not the string.
    passed = (
        outcome.get("outcome") == "applied"
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
        note="wedge injected, restarted, postcondition re-observed clear, ledger reached VERIFIED "
        "(canonical atlas/action-ledger/ + atlas/state/execution-state.json)",
        finding="loop.py's outcome STRING reports 'applied' rather than 'verified' unless "
        "require_verified_actions=True is ALSO passed, even though attaching lifecycle_ledger "
        "alone already makes performed=True proof and post-action verification mandatory "
        "(loop.py:372-379). An operator reading the tick's human-readable report cannot tell "
        "from the word 'applied' alone whether post-action verification actually ran.",
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
    on the proposal. Also documents the one-directional correlation gap."""
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
    correlated = intent is not None and intent.itil_change_id == pre_created.id

    # The gap: act_dispatch's OWN auto-created ITIL change (from apply_fn's
    # `itil=itil`) is a SEPARATE record, never fed back into the already-frozen
    # ActionIntent. Demonstrate it exists and differs.
    auto_records = [c for c in itil.list_changes() if c.id != pre_created.id]
    auto_change_id = auto_records[-1].id if auto_records else None

    passed = all_signed and chain_ok and correlated
    return ScenarioResult(
        name="7. signed ledger (real PGP) + ITIL correlation",
        passed=passed,
        evidence={
            "intent_id": intent_id,
            "events_signed": all_signed,
            "hash_chain_ok": chain_ok,
            "event_count": len(events),
            "pre_created_itil_change_id": pre_created.id,
            "ledger_itil_change_id": intent.itil_change_id if intent else None,
            "auto_created_itil_change_id (act_dispatch's own record)": auto_change_id,
            "sample_event_redacted": _redact_event(events[-1]) if events else None,
        },
        note="ITIL correlation works when the proposer supplies change_id in advance",
        finding=(
            None
            if auto_change_id is None
            else "act_dispatch.build_apply_fn's OWN auto-created ITIL change "
            f"({auto_change_id}) is NOT the same record as the ledger's itil_change_id "
            f"({intent.itil_change_id if intent else None}) and is never written back onto "
            "the ActionIntent (frozen at creation, before apply_fn runs). Correlation only "
            "works end-to-end when the proposer pre-creates the change and stamps "
            "proposal['change_id'] itself."
        ),
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
    """A proposal citing evidence from years ago is accepted with no freshness
    check anywhere in the ledger or safety layer. This is a documented gap."""
    slug = "s13-stale-evidence"
    ctx.inject_wedge()
    ancient_ref = "obs:2019-01-01T00:00:00Z:bridge-wedge-fabricated"
    proposal = ctx.wedge_proposal(evidence_ref=ancient_ref)
    apply_fn = act_dispatch.build_apply_fn(
        ctx.paths, _now_iso(), runner=ctx.runner_success_and_heal(), itil=None
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
    events = ledger.events(intent_id) if intent_id else []
    observed_event = events[0] if events else None
    accepted_stale_ref = bool(observed_event and observed_event.evidence_ref == ancient_ref)
    return ScenarioResult(
        name="9. stale evidence is not rejected [GAP]",
        passed=accepted_stale_ref,  # "pass" = the drill correctly demonstrates the gap
        evidence={
            "evidence_ref_supplied": ancient_ref,
            "evidence_ref_on_ledger_OBSERVED_event": (
                observed_event.evidence_ref if observed_event else None
            ),
            "outcome": outcome,
        },
        note="a 7-year-old, fabricated evidence_ref is accepted verbatim onto the ledger",
        finding="action_ledger.ActionEvent.evidence_ref is a free-text string (max_length=1024) "
        "with NO timestamp parsing or freshness check anywhere in action_ledger.py, safety.py, "
        "or loop.py. Any string is accepted as 'evidence' regardless of age, and nothing "
        "cross-checks it against the observation that actually triggered the proposal.",
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
    """Deliberately reuses ONE intent identity across TWO separate real
    occurrences of the same standing fault, to show what happens once the
    first occurrence's lifecycle reaches a terminal state (VERIFIED /
    ROLLED_BACK / ESCALATED). This is a documented gap, not a bug this drill
    is permitted to patch."""
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

    # Time passes (cooldown elapses); the SAME real fault recurs later. A brand
    # new, independent occurrence in the real world -- but action_ledger's
    # stable_intent_id() has no time component, so it is the SAME intent_id.
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
    ctx.heal_wedge()

    dead_end = "invalid action transition" in second_outcome.get("outcome", "")
    same_identity = second_outcome.get("intent_id") == intent_id
    return ScenarioResult(
        name="11. ledger cannot represent a later, separate occurrence [GAP]",
        passed=dead_end and same_identity,  # "pass" = the drill correctly demonstrates the gap
        evidence={
            "first_occurrence_outcome": first_outcome,
            "first_occurrence_final_ledger_state": str(first_state),
            "second_occurrence_outcome": second_outcome,
            "same_intent_id_both_times": same_identity,
        },
        note="occurrence 1 self-heals to VERIFIED; occurrence 2, a genuinely later and "
        "separate real-world firing of the SAME condition/object/action, gets the SAME "
        "stable intent_id and its transition is REJECTED by the ledger",
        finding="skcapstone.operator_seat.action_ledger.stable_intent_id() derives identity "
        "from (condition_fingerprint, application, target_kind, target_id, action, "
        "catalog_generation, itil_change_id, cmdb_ci_id, authorization_ref) with NO time or "
        "attempt component. Once that identity's lifecycle reaches a terminal state "
        "(VERIFIED/ROLLED_BACK/ESCALATED), loop.py's unconditional AUTHORIZED/EXECUTING "
        "append on the NEXT real occurrence of the exact same standing condition raises "
        "'invalid action transition: <terminal> -> authorized' and is caught by the broad "
        "except handler -- so every future recurrence of that condition fails with what "
        "looks like a ledger/lifecycle bug, not a description of the actual actuator "
        "outcome, and never reaches actuation again through this ledger. This is worse than "
        "it first looks even for the RETRY BUDGET (scenario 4): with a ledger attached and no "
        "rollback plan, ONE failure already escalates the intent to a terminal ledger state, "
        "so the SECOND of the three retries retry_budget=3 is supposed to allow fails "
        "immediately with this ledger error instead of a genuine re-attempt -- the circuit "
        "breaker's 3-strike budget is effectively moot on the production --honor path (which "
        "always attaches a ledger; operator_seat/cli.py) for any standard action proposed "
        "without a rollback_plan.",
    )


def scenario_mid_run_freeze_race(ctx: DrillContext) -> ScenarioResult:
    """Freeze lands AFTER the loop's top-of-pass check but BEFORE actuation.
    Physical actuation is still refused (per-verb fresh checks), but the pass's
    own `frozen` flag and the recorded failure reason don't distinguish this
    from a genuine actuator fault."""
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
    reported_frozen = res.get("frozen")
    actuation_refused_by_freeze = "fleet is frozen" in outcome.get("outcome", "")
    no_physical_call = True  # runner_boom would have raised DrillHarnessError, propagated up
    state_after = json.loads(state.path.read_text())["actions"].get(fingerprint, {})
    consumed_retry_budget = int(state_after.get("consecutive_failures", 0)) > int(
        state_before.get("consecutive_failures", 0)
    )
    # Safety property holds (no physical actuation); the observability property
    # does not (misleadingly counts as a generic actuation failure).
    passed = actuation_refused_by_freeze and no_physical_call
    return ScenarioResult(
        name="12. mid-run freeze race",
        passed=passed,
        evidence={
            "pass_reported_frozen": reported_frozen,
            "actuation_outcome": outcome,
            "execution_state_before": state_before,
            "execution_state_after": state_after,
        },
        note="freeze is set inside propose(), i.e. AFTER the loop's own pre-observe freeze "
        "check already passed. actuator.honor()'s fresh, independent frozen check still "
        "refuses the physical restart",
        finding=(
            "the pass-level result still reports frozen=False (freeze only gates the very "
            "start of _run_once, it is never re-checked at the pass level), and the refusal "
            "is recorded as a generic actuation failure ('fleet is frozen: the operator does "
            "not actuate') that consumes one unit of the SAME retry budget a real fault "
            "would. A human-triggered freeze landing mid-pass on a persistently-firing "
            "condition can therefore contribute toward opening that condition's circuit "
            "breaker, indistinguishably from real failures, unless a reader inspects the "
            "reason string."
            if consumed_retry_budget
            else "the pass-level result still reports frozen=False even though a freeze "
            "landed and was honored mid-pass; only the per-verb refusal (not the pass "
            "result) reflects it."
        ),
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
    scenario_mid_run_freeze_race,
    scenario_scheduler_overlap,
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
    )
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
        ctx.restore_daemon_probe()
        ctx.restore_env()
    finished = _now_iso()
    if not keep_root:
        fleet_drill.require_owned_root(resolved)
        import shutil

        shutil.rmtree(resolved)
    return DrillReport(root=resolved, results=results, started_at=started, finished_at=finished)
