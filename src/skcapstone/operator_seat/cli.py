"""The skoperator CLI: Atlas's control surface.

Available as `skoperator`. Commands:
  run       one operator pass (report-only by default; reasons via the hybrid brain)
  pending   list parked decisions awaiting a human
  decide    approve or reject a parked decision (human only), write-through to a CAB vote
  honor-pending   dispatch every PROPOSED ledger intent whose linked ITIL change now approves
  status    freeze state (frozen / active / unprovisioned)
  freeze / unfreeze   toggle the kill switch (human only)
  provision   write the freeze store in its off position for the first time (human only)

Report-only by default. With --execute, auto-normal proposals are applied via
the fleet act verb (signed spec annotations); majors still park for approval and
freeze always wins.
"""

from __future__ import annotations

import functools
import os
from datetime import datetime, timezone

import click

from ..fleet import signing, store
from ..fleet.operatorapp_controller import operatorapp_rows
from ..fleet.paths import default_paths
from . import (
    act_dispatch,
    action_ledger,
    bootstrap,
    brief_publish,
    decisions,
    discovery,
    dispatch,
    fleet_adapter,
    kedb_seeds,
    loop,
    notify,
    proposer,
    registration,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gateway() -> str:
    return os.environ.get("SKOPERATOR_GATEWAY", "http://localhost:18780/v1")


def _decisions_dir(paths) -> str:
    return str(paths.root / "decisions")


def _human_writer() -> store.Writer:
    # A CLI invocation is a human at a terminal, never the autonomous seat.
    return store.Writer(
        role="operator", node="cli", identity=store.writer_identity() or "human", agent_seat=False
    )


def _seat_writer() -> store.Writer:
    # The autonomous operator seat: it may register/refresh app objects, but the
    # store's human-only guard blocks it from writing ratifiedStandardActions.
    return store.Writer(
        role="operator",
        node=fleet_adapter.self_node_name(),
        identity=store.resolved_writer_identity(),
        agent_seat=True,
    )


@click.group(name="operator")
def operator() -> None:
    """Atlas, the SKWorld operator seat."""


@operator.command("run")
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help="Enable actuation: apply auto-normal fixes via the fleet act verb (majors still park).",
)
@click.option(
    "--honor",
    "honor_flag",
    is_flag=True,
    default=False,
    envvar="SKOPERATOR_HONOR",
    help=(
        "CR-9.1 autonomy step 1: physically actuate auto STANDARD-catalog fixes on "
        "the fleet + skchat adapters (else --execute only writes signed annotations). "
        "OFF by default; the one-line Chef flip. Ignored unless --execute is set."
    ),
)
@click.option(
    "--notify",
    "notify_flag",
    is_flag=True,
    default=False,
    help="Send the report + parked escalations to Telegram (silent when all quiet).",
)
@click.option(
    "--publish-dir",
    default=None,
    help="Where to write the static brief artifact (default: <fleet root>/atlas/brief).",
)
@click.option(
    "--no-publish",
    is_flag=True,
    default=False,
    help="Skip writing the static brief artifact this tick.",
)
@click.option(
    "--no-bootstrap",
    is_flag=True,
    default=False,
    help="Skip the startup bootstrap (register app adapters + seed the KEDB) this tick.",
)
def run_cmd(
    execute: bool,
    honor_flag: bool,
    notify_flag: bool,
    publish_dir: str | None,
    no_publish: bool,
    no_bootstrap: bool,
) -> None:
    """One operator pass: observe, reason, report. Report-only by default."""
    paths = default_paths()

    now = _now_iso()

    # Honoring only matters when execution is on: it is what turns an applied
    # proposal from a signed annotation (fleet_act) into a real STANDARD-catalog
    # actuation on the fleet + skchat adapters. OFF by default (the report/annotate
    # deployment running today), so this pass is byte-identical unless Chef flips it.
    honor = bool(honor_flag) and bool(execute)

    # Idempotent startup bootstrap: keep the Operatorapp set and the KEDB current
    # before the first pass, so registrations and known-error entries are never
    # stale or missing without a manual command. Writes only registration objects
    # + missing KEDB entries (both human-safe: the store guard blocks the seat
    # from writing ratifiedStandardActions, and KEDB seeding is create-or-skip);
    # it never actuates. Opt out with --no-bootstrap.
    if not no_bootstrap:
        from .. import SHARED_ROOT

        boot = bootstrap.bootstrap_operator(paths, writer=_seat_writer(), home=SHARED_ROOT)
        seeded = boot["seeded"]
        kedb_note = f"kedb seeded: {', '.join(seeded)}" if seeded else "kedb current"
        click.echo(f"bootstrap: {len(boot['registered'])} app(s) registered, {kedb_note}")
        if boot.get("discovered"):
            click.echo(f"discovered: {', '.join(boot['discovered'])}")

    # The action catalog the brain reasons over and the planner classifies against.
    # Honoring widens it to fleet + skchat so skchat self-heals can be proposed and
    # planned; without honoring it stays the fleet-only catalog (unchanged today).
    explain = act_dispatch.merged_explain() if honor else fleet_adapter.fleet_explain()

    def _propose(brief, route):
        if brief.get("quiet"):
            return []
        model = "ornith-1.0-35b" if route == "ornith" else "sk-default"
        chat = functools.partial(proposer.default_chat, base_url=_gateway(), model=model)
        return proposer.propose(brief, explain, chat=chat)

    apply_fn = None
    rollback_fn = None
    itil_manager = None
    if execute and honor:
        # CR-9.1: physically actuate auto STANDARD-catalog fixes (fleet + skchat),
        # each recorded as an ITIL change first. Freeze is enforced by the act verbs.
        # The SAME ITILManager instance is also handed to loop.run_once below (the
        # `itil=` param): that is what lets the loop draft-and-bind a change onto
        # each proposal's ledger intent BEFORE it exists, and re-point the auto
        # lane through dispatch.dispatch_intent (AUTONOMY_ARCHITECTURE.md
        # section 3.2) instead of authorizing itself inline.
        from .. import SHARED_ROOT
        from ..itil import ITILManager

        itil_manager = ITILManager(SHARED_ROOT)
        apply_fn = act_dispatch.build_apply_fn(paths, now, itil=itil_manager, emit=click.echo)
        rollback_fn = act_dispatch.build_rollback_fn(paths)
    elif execute:

        def apply_fn(prop, cls):  # noqa: E731 - annotation-only act verb (no --honor)
            return fleet_adapter.fleet_act(paths, prop, cls, now_iso=now)

    # Manifest-driven observe adapters (OPS0.3): empty when discovery is gated off
    # (SKOPERATOR_MANIFEST_DISCOVERY unset), so the pass is byte-identical to today.
    # Each observe closure receives now_iso from the loop when it is called.
    extra_observers = discovery.discover_observers()

    ledger = None
    if honor:
        signer = signing.capauth_signer()
        verifier = signing.capauth_verifier()
        if signer is None or verifier is None:
            raise click.ClickException(
                "HONOR requires a usable CapAuth signer and trusted verifier; failing closed"
            )
        ledger = action_ledger.ActionLedger(
            paths.root / "atlas" / "action-ledger",
            signer=signer,
            verifier=verifier,
            require_signatures=True,
        )

    res = loop.run_once(
        paths,
        now_iso=now,
        propose=_propose,
        explain=explain,
        decisions_dir=_decisions_dir(paths),
        apply_fn=apply_fn,
        rollback_fn=rollback_fn,
        execute=execute,
        emit=click.echo,
        extra_observers=extra_observers,
        # Validate the proposal's target against the live fleet before anything
        # is auto-applied. The proposer works from a brief keyed on app label,
        # so it can name an app ('skgateway') where the real objects are
        # 'upstreams' and 'connection-pool'. Unresolvable targets escalate.
        target_known=lambda p: fleet_adapter.fleet_target_known(paths, p),
        execution_state=loop.safety.ExecutionState(paths.root / "atlas" / "state"),
        # Physical execution is allowed only for a proposal bound to its owning
        # app + condition, human-ratified there, and verified by re-observation.
        require_verified_actions=honor,
        require_signed_catalog=honor,
        lifecycle_ledger=ledger,
        itil=itil_manager,
    )
    if honor:
        click.echo("honor: ON (CR-9.1 step-1 physical actuation: fleet + skchat)")
    if res.get("outcomes"):
        click.echo(f"({len(res['outcomes'])} proposal(s); parked escalations await approval)")

    # Publish the static brief artifact per tick (the atlas host serves it).
    if not no_publish:
        pub_dir = publish_dir or str(paths.root / "atlas" / "brief")
        written = brief_publish.publish_brief(res, now, pub_dir)
        click.echo(f"brief published: {written['html']}")

    # Telegram: message the human only when something happened (silent when quiet).
    if notify_flag and res.get("outcomes"):
        notify.notify_report(res["report"])
        escalated = {o["action"] for o in res["outcomes"] if o["disposition"] == "escalate"}
        if escalated:
            for d in decisions.list_pending(_decisions_dir(paths)):
                if any(o.get("action") in escalated for o in d.get("options", [])):
                    notify.notify_escalation(d)


@operator.command("pending")
def pending_cmd() -> None:
    """List decisions parked for a human."""
    rows = decisions.list_pending(_decisions_dir(default_paths()))
    if not rows:
        click.echo("no pending decisions")
        return
    for d in rows:
        opts = "; ".join(f"[{i}] {o.get('action')}" for i, o in enumerate(d.get("options", [])))
        click.echo(f"{d['id']}  {opts}")


@operator.command("decide")
@click.argument("decision_id")
@click.option("--approve/--reject", required=True)
@click.option("--choice", type=int, default=None, help="Option index when several are offered.")
def decide_cmd(decision_id: str, approve: bool, choice: int | None) -> None:
    """Approve or reject a parked decision (human only).

    Resolution is write-through (AUTONOMY_ARCHITECTURE.md section 3.1): the
    decisions store is a projection, so approving here submits a
    provenance-bound CAB vote on the option's linked ITIL change, and it is
    THAT vote, folded by ITIL, that authorizes -- never the free-text literal
    ``by="human"`` this command used to write (PROVENANCE_AND_MUTATION_STANDARD
    bans exactly that). A decision parked with no linked change authorizes
    nothing: this command refuses and names the reason instead of printing
    success.
    """
    from .. import SHARED_ROOT
    from ..itil import ITILManager

    subject = store.resolved_writer_identity()
    if subject in ("", "unattributed"):
        raise click.ClickException(
            "cannot resolve an authenticated CapAuth identity for this approval; "
            "failing closed rather than recording an unattributed decision"
        )
    try:
        out = dispatch.resolve_decision(
            _decisions_dir(default_paths()),
            ITILManager(SHARED_ROOT),
            decision_id,
            approve=approve,
            choice=choice,
            subject=subject,
            resolved_iso=_now_iso(),
        )
    except dispatch.UnauthorizedDecisionError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{decision_id} -> {out['status']}")


@operator.command("honor-pending")
def honor_pending_cmd() -> None:
    """Dispatch every PROPOSED ledger intent whose linked ITIL change now folds
    approved: the slow half of the dispatcher's "one code path, two speeds"
    (AUTONOMY_ARCHITECTURE.md section 3.2). Run this after `skoperator decide
    --approve` so a human's CAB vote actually causes the actuation it
    authorized -- before this command existed, nothing downstream ever
    re-read a resolved decision, and every escalated proposal was a dead end.

    Refuses outright (same as any actuation surface) unless the estate is
    actuation-ready and not frozen. An intent whose change has not yet folded
    approved is left pending for a later run, not treated as an error.
    """
    paths = default_paths()
    now = _now_iso()
    from .. import SHARED_ROOT
    from ..itil import ITILManager

    signer = signing.capauth_signer()
    verifier = signing.capauth_verifier()
    if signer is None or verifier is None:
        raise click.ClickException(
            "honor-pending requires a usable CapAuth signer and trusted verifier; "
            "failing closed"
        )
    ledger = action_ledger.ActionLedger(
        paths.root / "atlas" / "action-ledger",
        signer=signer,
        verifier=verifier,
        require_signatures=True,
    )
    itil_manager = ITILManager(SHARED_ROOT)
    apply_fn = act_dispatch.build_apply_fn(paths, now, itil=itil_manager, emit=click.echo)
    rollback_fn = act_dispatch.build_rollback_fn(paths)
    extra_observers = discovery.discover_observers()
    adapters = {**extra_observers, **loop.ADAPTERS}
    outcomes = dispatch.run_dispatch_pass(
        paths,
        ledger,
        itil_manager,
        adapters=adapters,
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        rollback_fn=rollback_fn,
        execution_state=loop.safety.ExecutionState(paths.root / "atlas" / "state"),
        decisions_dir=_decisions_dir(paths),
        now_iso=now,
        actor="dispatcher",
        emit=click.echo,
    )
    if not outcomes:
        click.echo("honor-pending: nothing to dispatch")
        return
    for o in outcomes:
        click.echo(f"{o.intent_id}: {o.outcome}")


@operator.command("status")
def status_cmd() -> None:
    """Show the freeze state: FROZEN, active, or UNPROVISIONED.

    Tri-state, deliberately (AUTONOMY_ARCHITECTURE.md section 3.6): a
    missing freeze store and a freeze store deliberately switched off used
    to render identically as "active (freeze off)", which made "provisioned
    and switched off" indistinguishable from "nobody ever set this up".
    UNPROVISIONED now names that second state instead of collapsing into
    active. Run `skoperator provision` to fix it.
    """
    gate = store.check_actuation_gate(default_paths())
    if gate.reason == store.REASON_FROZEN:
        click.echo("FROZEN (Atlas stands down)")
    elif gate.reason == store.REASON_UNPROVISIONED:
        click.echo(
            "UNPROVISIONED (no freeze store written by a human yet; "
            "Atlas cannot actuate; run `skoperator provision`)"
        )
    else:
        click.echo("active (freeze off)")


@operator.command("schedule-doctor")
def schedule_doctor_cmd() -> None:
    """Read-only comparison of ATLAS units with effective user-systemd config."""
    from pathlib import Path

    from skcapstone import systemd_drift

    source = Path(__file__).resolve().parents[3] / "data" / "systemd"
    results = systemd_drift.audit(source, ("skoperator.service", "skoperator.timer"))
    dirty = False
    for result in results:
        if result.clean:
            click.echo(f"clean: {result.unit}")
            continue
        dirty = True
        if result.unavailable:
            click.echo(f"unavailable: {result.unit}: {result.unavailable}")
        for field in result.missing:
            click.echo(f"missing: {result.unit} {field}")
        for field in result.changed:
            click.echo(f"changed: {result.unit} {field}")
        for field in result.extra:
            click.echo(f"extra: {result.unit} {field}")
    if dirty:
        raise click.ClickException("effective systemd configuration has drift")


@operator.command("freeze")
@click.option("--reason", default="", help="Why the fleet is being frozen.")
def freeze_cmd(reason: str) -> None:
    """Freeze the fleet: Atlas halts all actuation (human only)."""
    store.set_frozen(default_paths(), True, writer=_human_writer(), reason=reason)
    click.echo("frozen: Atlas will stand down until unfrozen")


@operator.command("unfreeze")
def unfreeze_cmd() -> None:
    """Lift the freeze (human only)."""
    store.set_frozen(default_paths(), False, writer=_human_writer())
    click.echo("unfrozen: Atlas resumes")


@operator.command("provision")
def provision_cmd() -> None:
    """Provision the freeze store in its off position (human only).

    The one-time ceremony AUTONOMY_ARCHITECTURE.md section 3.6 requires
    before any actuation surface may run: a human writes `_freeze.json`
    through the same human-only path `freeze`/`unfreeze` use
    (`store.set_frozen`), so the kill switch is proven to exist before
    anything it governs can act. `skoperator unfreeze` performs the exact
    same write and remains the command to lift an existing freeze; this
    command exists so first-time setup has a name that says what it means
    instead of reading as "unfreeze something that was never frozen".

    Safe to re-run: a state that is already provisioned is left alone.
    """
    paths = default_paths()
    if store.actuation_ready(paths):
        click.echo("already provisioned: freeze store exists and was human-written")
        return
    store.set_frozen(paths, False, writer=_human_writer(), reason="initial provisioning")
    click.echo("provisioned: freeze store written (off); actuation surfaces may now run")


@operator.command("kedb-seed")
def kedb_seed_cmd() -> None:
    """Seed the ITIL KEDB with the known errors the app adapters reference.

    Create-or-skip: entries that already exist are left as-is, so this is safe to
    run repeatedly (and alongside `apps register`). Makes every adapter kedb_ref
    resolve to a real runbook entry instead of a dangling id.
    """
    from .. import SHARED_ROOT

    created = kedb_seeds.seed_operator_kedb(SHARED_ROOT)
    if created:
        click.echo("seeded: " + ", ".join(created))
    else:
        click.echo("kedb already seeded (nothing to do)")


@operator.group("apps")
def apps() -> None:
    """The subapps Atlas operates, registered as fleet Operatorapp objects."""


@apps.command("list")
def apps_list_cmd() -> None:
    """List registered subapps and their ratification state."""
    rows = operatorapp_rows(default_paths(), _now_iso())
    if not rows:
        click.echo("no registered apps (run: skoperator apps register)")
        return
    for r in rows:
        mark = "ok" if r.proposals_ratified else f"{r.ratified_count}/{r.proposed_count} ratified"
        cli = r.cli or "-"
        click.echo(f"{r.name:12} {cli:22} proposed={r.proposed_count} [{mark}]")


@apps.command("register")
def apps_register_cmd() -> None:
    """Register or refresh an Operatorapp object per app adapter (seat writer).

    Also seeds the ITIL KEDB (create-or-skip) so every adapter kedb_ref resolves
    to a real runbook entry the moment the apps are registered.
    """
    from .. import SHARED_ROOT

    boot = bootstrap.bootstrap_operator(default_paths(), writer=_seat_writer(), home=SHARED_ROOT)
    click.echo("registered: " + ", ".join(boot["registered"]))
    if boot["seeded"]:
        click.echo("kedb seeded: " + ", ".join(boot["seeded"]))


@apps.command("ratify")
@click.argument("app")
@click.argument("action")
def apps_ratify_cmd(app: str, action: str) -> None:
    """Ratify one proposed standard action for an app (human only)."""
    try:
        registration.ratify(default_paths(), app, action, writer=_human_writer())
    except ValueError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"ratified: {app} may now run {action} auto-standard")


def main() -> None:
    operator()


if __name__ == "__main__":
    main()
