"""The skoperator CLI: Atlas's control surface.

Available as `skoperator`. Commands:
  run       one operator pass (report-only by default; reasons via the hybrid brain)
  pending   list parked decisions awaiting a human
  decide    approve or reject a parked decision (human only)
  status    freeze state
  freeze / unfreeze   toggle the kill switch (human only)

Report-only by default. With --execute, auto-normal proposals are applied via
the fleet act verb (signed spec annotations); majors still park for approval and
freeze always wins.
"""

from __future__ import annotations

import functools
import os
from datetime import datetime, timezone

import click

from ..fleet import store
from ..fleet.operatorapp_controller import operatorapp_rows
from ..fleet.paths import default_paths
from . import (
    bootstrap,
    brief_publish,
    decisions,
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
        identity="operator",
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
    notify_flag: bool,
    publish_dir: str | None,
    no_publish: bool,
    no_bootstrap: bool,
) -> None:
    """One operator pass: observe, reason, report. Report-only by default."""
    paths = default_paths()

    now = _now_iso()

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

    def _propose(brief, route):
        if brief.get("quiet"):
            return []
        model = "ornith-1.0-35b" if route == "ornith" else "sk-default"
        chat = functools.partial(proposer.default_chat, base_url=_gateway(), model=model)
        return proposer.propose(brief, fleet_adapter.fleet_explain(), chat=chat)

    apply_fn = None
    if execute:

        def apply_fn(prop, cls):  # noqa: E731 - the fleet act verb, only when --execute
            return fleet_adapter.fleet_act(paths, prop, cls, now_iso=now)

    res = loop.run_once(
        paths,
        now_iso=now,
        propose=_propose,
        decisions_dir=_decisions_dir(paths),
        apply_fn=apply_fn,
        execute=execute,
        emit=click.echo,
    )
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
    """Approve or reject a parked decision (human only)."""
    out = decisions.resolve(
        _decisions_dir(default_paths()),
        decision_id,
        approve=approve,
        choice=choice,
        by="human",
        resolved_iso=_now_iso(),
    )
    click.echo(f"{decision_id} -> {out['status']}")


@operator.command("status")
def status_cmd() -> None:
    """Show the freeze state."""
    frozen = store.is_frozen(default_paths())
    click.echo("FROZEN (Atlas stands down)" if frozen else "active (freeze off)")


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
