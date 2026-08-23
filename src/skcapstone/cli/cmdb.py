"""CMDB / asset management CLI commands.

The CMDB had a dashboard surface and no CLI, so the only way to populate or
inspect assets was the web UI. That also left
``cronjob-skbrain-cmdb-reconcile.json`` in the skbrain pack calling
``skcapstone cmdb reconcile``, a verb that did not exist.

``scan`` and ``reconcile`` are read-only unless ``--apply`` is passed. A scan
that writes by default is a scan nobody can safely run twice.
"""

from __future__ import annotations

import importlib
import json as _json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ._common import SHARED_ROOT, console

_STATUS_COLOR = {
    "operational": "green",
    "degraded": "yellow",
    "down": "red",
    "retired": "dim",
}


def _manager():
    from skcoord.cmdb import CMDBManager

    return CMDBManager(Path(SHARED_ROOT).expanduser())


def _discovery():
    """Import the SKCapstone discovery facade or report an old dependency.

    The base collectors ship in skcoord while SKCapstone adds governed ingress
    identity and address-set support. Without skcoord the failure should name
    the package an operator must upgrade.
    """
    try:
        importlib.import_module("skcoord.discovery")
        import skcapstone.cmdb_discovery as mod
    except ImportError as exc:  # pragma: no cover - depends on installed version
        raise click.ClickException(
            "skcoord.discovery is missing: this skcoord is too old for "
            "`cmdb scan/reconcile/drift`. Upgrade skcoord, then retry. "
            f"({exc})"
        ) from exc
    return mod


def _orchestration():
    """Import the bounded fleet orchestration shipped by newer skcoord."""
    try:
        import skcoord.cmdb_reconcile as mod
    except ImportError as exc:  # pragma: no cover - installed-version dependent
        raise click.ClickException(
            "skcoord.cmdb_reconcile is missing; upgrade skcoord for --network support"
        ) from exc
    discovery = _discovery()
    mod.DECLARED_COLLECTORS = discovery.DECLARED_COLLECTORS
    mod.OBSERVED_COLLECTORS = discovery.OBSERVED_COLLECTORS
    return mod


def _vault_transport():
    """Return the installed skvault SSH metadata adapter.

    Kept as a small seam so distributions can provide the adapter without the
    CLI ever accepting key paths or secret material directly.
    """
    try:
        from skvault import resolve_ssh
    except (ImportError, AttributeError) as exc:  # pragma: no cover - install dependent
        raise click.ClickException(
            "skvault does not expose resolve_ssh; install the CMDB SSH adapter"
        ) from exc

    class Transport:
        def resolve_ssh(self, reference):
            return resolve_ssh(reference)

    return Transport()


def _credential_references(values: tuple[str, ...]) -> dict[str, str]:
    """Parse explicit HOST=skvault://... mappings without resolving secrets."""
    references = {}
    for value in values:
        host, separator, reference = value.partition("=")
        if not separator or not host.strip() or not reference.startswith("skvault://"):
            raise click.ClickException(
                "--credential must be HOST=skvault://REFERENCE (repeat per target)"
            )
        if host.strip() in references:
            raise click.ClickException(f"duplicate --credential mapping for {host.strip()}")
        references[host.strip()] = reference
    return references


def _secure_runner_factory(targets, values: tuple[str, ...]):
    """Build a target runner factory backed only by explicit skvault refs."""
    references = _credential_references(values)
    missing = sorted(target.host for target in targets if target.host not in references)
    extra = sorted(set(references) - {target.host for target in targets})
    if missing:
        raise click.ClickException(
            "missing --credential mapping for network target(s): " + ", ".join(missing)
        )
    if extra:
        raise click.ClickException(
            "credential mapping is outside fleet scope: " + ", ".join(extra)
        )
    from skcoord.infrastructure_discovery import (
        SecureSSHRunner,
        SKVaultCredentialResolver,
    )

    resolver = SKVaultCredentialResolver(_vault_transport())
    credentials = {host: resolver.resolve(reference) for host, reference in references.items()}
    return lambda host: SecureSSHRunner(host, credentials[host])


def _build_runners(hosts: tuple[str, ...], local: bool):
    """Turn --host/--local into runners. No host means no observation."""
    if not local and not hosts:
        # Nothing to observe, so do not demand a dependency we will not use.
        return []
    disc = _discovery()

    runners = []
    if local:
        runners.append(disc.LocalRunner())
    for spec in hosts:
        name, _, target = spec.partition("=")
        runners.append(disc.SSHRunner(host=name, target=target or name))
    return runners


def register_cmdb_commands(main: click.Group) -> None:
    """Register the cmdb command group."""

    @main.group()
    def cmdb():
        """CMDB - configuration items, discovery, and drift."""

    @cmdb.group("operator")
    def cmdb_operator():
        """ATLAS operator facet: explain, observe, and governed actuation."""

    @cmdb_operator.command("explain")
    def cmdb_operator_explain():
        """Describe CMDB conditions and actions to ATLAS."""
        from skcapstone.operator_seat.cmdb_adapter import cmdb_explain

        click.echo(_json.dumps(cmdb_explain(), indent=2))

    @cmdb_operator.command("observe")
    def cmdb_operator_observe():
        """Read CMDB health without changing the store or timers."""
        from skcapstone.operator_seat.cmdb_adapter import observe

        click.echo(_json.dumps(observe(), indent=2))

    @cmdb_operator.command("act")
    @click.argument("action", type=click.Choice(["run-cmdb-shadow", "apply-cmdb-reconcile"]))
    @click.option("--change-id", help="Approved ITIL change binding (required for apply).")
    def cmdb_operator_act(action, change_id):
        """Start a governed CMDB oneshot (freeze-aware)."""
        from skcapstone.fleet.paths import default_paths
        from skcapstone.operator_seat.cmdb_adapter import cmdb_act

        result = cmdb_act(default_paths(), action, change_id=change_id)
        click.echo(_json.dumps(result, indent=2))
        if not result["performed"]:
            raise click.ClickException(result.get("reason", "CMDB action failed"))

    # ── cmdb list ─────────────────────────────────────────────────────

    @cmdb.command("list")
    @click.option("--type", "ci_type", default=None, help="Filter by CI type.")
    @click.option("--tag", default=None, help="Filter by tag.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the CI list as JSON.")
    def cmdb_list(ci_type, tag, as_json):
        """List configuration items."""
        cis = _manager().list_cis(ci_type)
        if tag:
            cis = [c for c in cis if tag in (c.tags or [])]

        if as_json:
            click.echo(_json.dumps([c.model_dump() for c in cis], indent=2, default=str))
            return

        if not cis:
            console.print("[dim]No configuration items.[/dim]")
            return
        console.print(f"\n[bold]Configuration Items[/bold] ({len(cis)})")
        for ci in cis:
            color = _STATUS_COLOR.get(ci.status, "white")
            node = f" @ {ci.node}" if ci.node else ""
            console.print(f"  [{color}]{ci.status:<12}[/{color}] {ci.ci_type:<10} {ci.name}{node}")

    # ── cmdb show ─────────────────────────────────────────────────────

    @cmdb.command("show")
    @click.argument("ci_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit the CI as JSON.")
    def cmdb_show(ci_id, as_json):
        """Show one configuration item, folded from its event log."""
        ci = _manager().get_ci(ci_id)
        if ci is None:
            raise click.ClickException(f"CI not found: {ci_id}")

        if as_json:
            click.echo(_json.dumps(ci.model_dump(), indent=2, default=str))
            return

        console.print(f"\n[bold]{ci.name}[/bold]  [dim]{ci.id}[/dim]")
        console.print(f"  type:    {ci.ci_type}")
        console.print(f"  status:  {ci.status}")
        if ci.node:
            console.print(f"  node:    {ci.node}")
        if ci.description:
            console.print(f"  desc:    {ci.description}")
        if ci.tags:
            console.print(f"  tags:    {', '.join(ci.tags)}")
        for key, value in sorted(ci.attributes.items()):
            console.print(f"  [dim]{key}[/dim]: {value}")
        for rel in ci.relationships:
            console.print(f"  [cyan]{rel.rel_type}[/cyan] -> {rel.target}")

    # ── cmdb migrate-schema ──────────────────────────────────────────

    @cmdb.command("migrate-schema")
    @click.option("--apply", is_flag=True, help="Perform the atomic cutover. Off by default.")
    @click.option(
        "--backup-path",
        type=click.Path(path_type=Path),
        help="Retained backup path beside cmdb/ (apply only).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit the migration report as JSON.")
    def cmdb_migrate_schema(apply, backup_path, as_json):
        """Plan or apply the physical CMDB v1-to-v2 migration.

        The default is a write-free dry run. Applying stages and validates a
        complete copy before an atomic directory cutover, retaining the old
        store as a rollback backup.
        """
        if backup_path is not None and not apply:
            raise click.ClickException("--backup-path is only meaningful with --apply")
        try:
            result = _manager().migrate_schema(apply=apply, backup_path=backup_path)
        except (ValueError, OSError) as exc:
            raise click.ClickException(f"schema migration refused: {exc}") from exc

        if as_json:
            click.echo(_json.dumps(result, indent=2, default=str))
            return
        mode = "[green]applied[/green]" if result["applied"] else "[yellow]dry run[/yellow]"
        console.print(f"\n[bold]CMDB schema migration[/bold] ({mode})")
        console.print(f"  records: {result['records']}")
        console.print(f"  v1 cores: {result['cores']}")
        console.print(f"  v1 events: {result['events']}")
        if result.get("backup"):
            console.print(f"  backup: {result['backup']}")
        elif result["records_to_migrate"] == 0:
            console.print("[green]Already at schema v2; nothing to do.[/green]")
        elif not apply:
            console.print("\n[dim]Nothing was written. Re-run with --apply after review.[/dim]")

    # ── cmdb scan ─────────────────────────────────────────────────────

    @cmdb.command("scan")
    @click.option(
        "--host",
        multiple=True,
        help="Observe a remote node over ssh. NAME or NAME=ssh-target. Repeatable.",
    )
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option(
        "--declared/--no-declared", default=True, help="Read fleet objects, registry, agents."
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit discovered CIs as JSON.")
    def cmdb_scan(host, local, declared, as_json):
        """Scan declared and observed state. Read-only: never writes.

        Use `cmdb reconcile --apply` to persist what a scan finds.
        """
        run_scan = _discovery().scan

        runners = _build_runners(host, local)
        found = run_scan(
            Path(SHARED_ROOT).expanduser(), runners=runners, include_declared=declared
        )

        if as_json:
            click.echo(
                _json.dumps(
                    [
                        {
                            "ci_id": d.ci_id,
                            "ci_type": d.ci_type,
                            "name": d.name,
                            "source": d.source,
                            "observed": d.observed,
                            "node": d.node,
                            "attributes": d.attributes,
                            "tags": list(d.tags),
                            "relationships": [
                                {"rel_type": r, "target": t} for r, t in d.relationships
                            ],
                        }
                        for d in found
                    ],
                    indent=2,
                    default=str,
                )
            )
            return

        observed = sum(1 for d in found if d.observed)
        console.print(
            f"\n[bold]Discovered[/bold] {len(found)} CIs "
            f"([green]{observed} observed[/green], {len(found) - observed} declared only)"
        )
        if not runners:
            console.print("[yellow]  No runners: nothing was observed, only specs read.[/yellow]")
        for d in found:
            mark = "[green]*[/green]" if d.observed else " "
            console.print(f"  {mark} {d.ci_type:<10} {d.name:<40} [dim]{d.source}[/dim]")

    # ── cmdb reconcile ────────────────────────────────────────────────

    @cmdb.command("reconcile")
    @click.option("--host", multiple=True, help="Observe a remote node over ssh. Repeatable.")
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option(
        "--network",
        is_flag=True,
        help="Scan the authoritative fleet target set with bounded concurrency.",
    )
    @click.option(
        "--credential",
        "credentials",
        multiple=True,
        metavar="HOST=SKVAULT_REF",
        help="Explicit skvault SSH credential reference. Repeat for every network target.",
    )
    @click.option("--apply", is_flag=True, help="Write the changes. Off by default.")
    @click.option(
        "--record-run",
        is_flag=True,
        help="Persist the checksummed run artifact even in dry-run/shadow mode.",
    )
    @click.option("--agent", default="cmdb-discovery", help="Writer name for the event log.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
    def cmdb_reconcile(host, local, network, credentials, apply, record_run, agent, as_json):
        """Converge the CMDB on discovered state. Additive: never deletes."""
        disc = _discovery()
        run_scan, run_reconcile = disc.scan, disc.reconcile

        mgr = _manager()
        home = Path(SHARED_ROOT).expanduser()
        artifact = None
        if network:
            if host:
                raise click.ClickException("--network cannot be combined with --host")
            orch = _orchestration()
            targets = orch.resolve_targets(home)
            if not targets:
                raise click.ClickException("--network resolved no authoritative fleet targets")
            runner_factory = _secure_runner_factory(targets, credentials)
            scan_result = orch.scan_network(
                home,
                targets,
                runner_factory,
            )
            if apply and not scan_result.complete:
                raise click.ClickException(
                    "refusing --apply: network scan is incomplete; inspect collector health"
                )
            scope_fingerprint = scan_result.scope_fingerprint()
            discovered_ids = [item.ci_id for item in scan_result.discovered]
            owned_ids = [
                ci.id
                for ci in mgr.list_cis()
                if "discovered" in (ci.tags or [])
                and str(ci.attributes.get("source_authority", "")).startswith("network:")
                and ci.attributes.get("lifecycle_scope") == scope_fingerprint
            ]
            lifecycle_actions = orch.apply_retirement_lifecycle(
                mgr,
                "network:fleet",
                scope_fingerprint,
                discovered_ids,
                owned_ids,
                scan_result.complete,
                apply=False,
                agent=agent,
            )
            artifact, _events = orch.run_reconcile(
                mgr,
                scan_result,
                apply=apply,
                code_version="skcapstone",
                lifecycle_actions=lifecycle_actions,
                agent=agent,
            )
            validation_failures = artifact.get("plan", {}).get("validation_failures", [])
            if apply and validation_failures:
                raise click.ClickException(
                    "refusing --apply: discovery evidence failed validation; inspect the plan"
                )
            if apply:
                orch.apply_retirement_lifecycle(
                    mgr,
                    "network:fleet",
                    scope_fingerprint,
                    discovered_ids,
                    owned_ids,
                    scan_result.complete,
                    apply=True,
                    agent=agent,
                )
            if apply or record_run:
                artifact_path, checksum = orch.write_run_artifact(home, artifact)
                artifact["artifact"] = {
                    "path": str(artifact_path),
                    "sha256": checksum,
                }
            report_data = artifact["reconcile"]
        else:
            found = run_scan(home, runners=_build_runners(host, local))
            report = run_reconcile(mgr, found, agent=agent, apply=apply)
            report_data = report.as_dict()
            if apply and report.validation_failures:
                raise click.ClickException(
                    "refusing --apply: discovery evidence failed validation; inspect `cmdb plan`"
                )

        if as_json:
            click.echo(_json.dumps(artifact or report_data, indent=2, default=str))
            return

        mode = "[green]applied[/green]" if apply else "[yellow]dry run[/yellow]"
        console.print(f"\n[bold]CMDB reconcile[/bold] ({mode})")
        console.print(f"  created:   {len(report_data['created'])}")
        console.print(f"  updated:   {len(report_data['updated'])}")
        console.print(f"  unchanged: {len(report_data['unchanged'])}")
        console.print(f"  orphans:   {len(report_data['orphans'])}")
        for ci_id in report_data["created"][:20]:
            console.print(f"    [green]+[/green] {ci_id}")
        for ci_id, keys in list(report_data["updated"].items())[:20]:
            console.print(f"    [yellow]~[/yellow] {ci_id}: {', '.join(keys)}")
        for ci_id in report_data["orphans"][:20]:
            console.print(f"    [dim]?[/dim] {ci_id} (not seen; left in place)")
        if not apply:
            console.print("\n[dim]Nothing was written. Re-run with --apply.[/dim]")

    # Explicit supported verbs. ``reconcile`` stays as a compatibility command
    # for existing timers, while plan/apply make write intent unambiguous.

    @cmdb.command("plan")
    @click.option("--host", multiple=True, help="Observe a remote node over ssh. Repeatable.")
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option("--network", is_flag=True, help="Scan the authoritative fleet target set.")
    @click.option(
        "--credential",
        "credentials",
        multiple=True,
        metavar="HOST=SKVAULT_REF",
        help="Explicit skvault SSH credential reference. Repeat for every network target.",
    )
    @click.option("--record-run", is_flag=True, help="Persist the checksummed shadow artifact.")
    @click.option("--agent", default="cmdb-discovery", help="Writer name for the event log.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the complete plan as JSON.")
    @click.pass_context
    def cmdb_plan(ctx, host, local, network, credentials, record_run, agent, as_json):
        """Plan reconciliation without modifying the CMDB."""
        return ctx.invoke(
            cmdb_reconcile,
            host=host,
            local=local,
            network=network,
            credentials=credentials,
            apply=False,
            record_run=record_run,
            agent=agent,
            as_json=as_json,
        )

    @cmdb.command("apply")
    @click.option("--host", multiple=True, help="Observe a remote node over ssh. Repeatable.")
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option("--network", is_flag=True, help="Scan the authoritative fleet target set.")
    @click.option(
        "--credential",
        "credentials",
        multiple=True,
        metavar="HOST=SKVAULT_REF",
        help="Explicit skvault SSH credential reference. Repeat for every network target.",
    )
    @click.option("--agent", default="cmdb-discovery", help="Writer name for the event log.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the applied report as JSON.")
    @click.pass_context
    def cmdb_apply(ctx, host, local, network, credentials, agent, as_json):
        """Apply a validated reconciliation plan and append audit events."""
        return ctx.invoke(
            cmdb_reconcile,
            host=host,
            local=local,
            network=network,
            credentials=credentials,
            apply=True,
            record_run=network,
            agent=agent,
            as_json=as_json,
        )

    @cmdb.command("status")
    @click.option("--json", "as_json", is_flag=True, help="Emit status as JSON.")
    def cmdb_status(as_json):
        """Show inventory, audit, and checksum-verified discovery freshness."""
        orch = _orchestration()
        mgr = _manager()
        artifacts = orch.read_verified_run_artifacts(Path(SHARED_ROOT).expanduser())
        result = orch.operator_summary(
            artifacts,
            datetime.now(timezone.utc),
            timedelta(hours=4),
        )
        cis = mgr.list_cis()
        result["inventory"] = {
            "total": len(cis),
            "discovered": sum("discovered" in (ci.tags or []) for ci in cis),
            "retired": sum(ci.status == "retired" for ci in cis),
        }
        findings = mgr.audit_relationships()
        result["relationship_audit"] = {
            "clean": not findings,
            "findings": findings,
        }
        if as_json:
            click.echo(_json.dumps(result, indent=2, default=str))
            return
        console.print("\n[bold]CMDB status[/bold]")
        console.print(f"  inventory: {result['inventory']['total']} CIs")
        console.print(f"  latest scan: {result['latest_scan_id'] or 'none'}")
        console.print(f"  latest complete: {result['latest_complete']}")
        console.print(f"  relationship audit clean: {result['relationship_audit']['clean']}")

    # ── cmdb drift ────────────────────────────────────────────────────

    @cmdb.command("drift")
    @click.option("--host", multiple=True, help="Observe a remote node over ssh. Repeatable.")
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option("--json", "as_json", is_flag=True, help="Emit findings as JSON.")
    def cmdb_drift(host, local, as_json):
        """Where the specs and the machines disagree."""
        disc = _discovery()
        run_scan, run_drift = disc.scan, disc.drift

        runners = _build_runners(host, local)
        mgr = _manager()
        found = run_scan(Path(SHARED_ROOT).expanduser(), runners=runners)
        findings = run_drift(found, mgr)

        if as_json:
            click.echo(_json.dumps([f.as_dict() for f in findings], indent=2, default=str))
            return

        if not runners:
            console.print(
                "[yellow]No runners: without observation, drift cannot be measured.[/yellow]"
            )
        if not findings:
            console.print("[green]No drift.[/green]")
            return
        console.print(f"\n[bold]Drift[/bold] ({len(findings)} findings)")
        for finding in findings:
            color = "red" if finding.kind == "declared_not_observed" else "yellow"
            console.print(f"  [{color}]{finding.kind}[/{color}] {finding.ci_id}")
            console.print(f"    [dim]{finding.detail}[/dim]")

    # ── cmdb retire ───────────────────────────────────────────────

    @cmdb.command("retire")
    @click.argument("ci_ids", nargs=-1)
    @click.option(
        "--orphans",
        is_flag=True,
        help=(
            "Retire the scan's orphan CIs (discovered, not seen this pass) "
            "instead of the given IDs."
        ),
    )
    @click.option(
        "--confirm-single-pass",
        is_flag=True,
        help="Explicitly acknowledge that --orphans bypasses the N-pass network lifecycle.",
    )
    @click.option("--host", multiple=True, help="Observe a remote node over ssh. Repeatable.")
    @click.option("--local/--no-local", default=True, help="Observe this machine.")
    @click.option("--note", default="", help="Note recorded on each retire event.")
    @click.option("--agent", default="cmdb-retire", help="Writer name for the event log.")
    @click.option("--json", "as_json", is_flag=True, help="Emit the result as JSON.")
    def cmdb_retire(ci_ids, orphans, confirm_single_pass, host, local, note, agent, as_json):
        """Retire CIs: status -> retired. Nothing is ever deleted (CMDB-8).

        The store is append-only, so a CI nobody observes stops being trusted
        instead of disappearing: `retired` keeps the record (attributes,
        relationships, history) and reconcile never un-retires a CI, so the
        next scan cannot resurrect it. Orphans from the last scan can be
        retired in one pass with --orphans.
        """
        mgr = _manager()
        source = "given"

        if orphans:
            if not confirm_single_pass:
                raise click.ClickException(
                    "--orphans is a single-pass compatibility path; add "
                    "--confirm-single-pass or use `cmdb reconcile --network --apply` "
                    "for scope-bound N-pass retirement"
                )
            disc = _discovery()
            found = disc.scan(Path(SHARED_ROOT).expanduser(), runners=_build_runners(host, local))
            report = disc.reconcile(mgr, found, agent=agent, apply=False)
            targets = list(report.orphans)
            source = "orphans"
            if not targets:
                result = {"retired": [], "already_retired": [], "not_found": []}
                if as_json:
                    click.echo(_json.dumps(result, indent=2))
                    return
                console.print("[green]No orphan CIs to retire.[/green]")
                return
        else:
            if not ci_ids:
                raise click.ClickException("Give CI IDs to retire, or use --orphans.")
            targets = list(ci_ids)

        default_note = (
            "orphan: not seen in scan (retire-not-delete)"
            if source == "orphans"
            else "retired by operator (retire-not-delete)"
        )
        retired, already, missing = [], [], []
        for cid in targets:
            ci = mgr.get_ci(cid)
            if ci is None:
                missing.append(cid)
                continue
            if ci.status == "retired":
                already.append(cid)
                continue
            mgr.set_status(cid, agent, "retired", note=note or default_note)
            retired.append(cid)

        if missing and not retired and not already:
            # Nothing happened: fail loudly, but still emit the payload so an
            # agent parsing --json sees which IDs were not found.
            if as_json:
                click.echo(
                    _json.dumps(
                        {"retired": [], "already_retired": [], "not_found": missing},
                        indent=2,
                    )
                )
            raise click.ClickException(f"No CIs retired; {len(missing)} not found.")

        result = {"retired": retired, "already_retired": already, "not_found": missing}
        if as_json:
            click.echo(_json.dumps(result, indent=2))
            return

        for cid in retired:
            console.print(f"  [dim]retired[/dim]   {cid}")
        for cid in already:
            console.print(f"  [green]kept[/green]    {cid} (already retired)")
        for cid in missing:
            console.print(f"  [yellow]missing[/yellow] {cid}")
        if retired or already:
            console.print(
                "\n[dim]Retirement is a status event, not a deletion: the records stay "
                "and reconcile will never un-retire them.[/dim]"
            )

    # ── cmdb impact ───────────────────────────────────────────────────

    @cmdb.command("impact")
    @click.argument("ci_id")
    @click.option("--transitive", is_flag=True, help="Traverse all bounded dependents.")
    @click.option("--max-depth", default=8, show_default=True, type=click.IntRange(min=0))
    @click.option("--max-nodes", default=1000, show_default=True, type=click.IntRange(min=1))
    @click.option("--json", "as_json", is_flag=True, help="Emit the analysis as JSON.")
    def cmdb_impact(ci_id, transitive, max_depth, max_nodes, as_json):
        """What breaks if this CI does, plus its open incidents."""
        mgr = _manager()
        result = (
            mgr.impact_graph(ci_id, max_depth=max_depth, max_nodes=max_nodes)
            if transitive
            else mgr.impact_analysis(ci_id)
        )
        if result.get("error"):
            raise click.ClickException(f"{result['error']}: {ci_id}")

        if as_json:
            click.echo(_json.dumps(result, indent=2, default=str))
            return

        if transitive:
            console.print(f"\n[bold]Transitive impact: {ci_id}[/bold]")
            console.print(f"  dependents: {len(result['dependents'])}")
            console.print(f"  cycles: {len(result['cycles'])}")
            console.print(f"  truncated: {result['truncated']}")
            for dep in result["dependents"]:
                console.print(
                    f"    d={dep['depth']} [cyan]{dep['rel']}[/cyan] "
                    f"{dep['ci_type']} {dep['name']}"
                )
            return

        ci = result["ci"]
        console.print(f"\n[bold]Impact: {ci['name']}[/bold]  [dim]{ci['id']}[/dim]")
        dependents = result["dependents"]
        console.print(f"  dependents: {len(dependents)}")
        for dep in dependents:
            console.print(f"    [cyan]{dep['rel']}[/cyan] {dep['ci_type']} {dep['name']}")
        incidents = result["open_incidents"]
        console.print(f"  open incidents: {len(incidents)}")
        for inc in incidents:
            console.print(f"    [red]{inc['severity']}[/red] {inc['id']} {inc['title']}")

    @cmdb.command("audit")
    @click.option("--json", "as_json", is_flag=True, help="Emit findings as JSON.")
    def cmdb_audit(as_json):
        """Audit relationship integrity without changing the CMDB."""
        findings = _manager().audit_relationships()
        if as_json:
            click.echo(_json.dumps(findings, indent=2, default=str))
            return
        if not findings:
            console.print("[green]Relationship graph is internally consistent.[/green]")
            return
        console.print(f"[yellow]Relationship findings: {len(findings)}[/yellow]")
        for finding in findings:
            console.print(
                f"  {finding['kind']} {finding['source']} "
                f"--{finding['relationship']}--> {finding['target']}"
            )
