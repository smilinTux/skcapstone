"""The skfleet CLI: fleet inventory, cordon, freeze, explain, sknoded.

Available standalone as `skfleet` and as `skcapstone fleet ...`.
"""

from __future__ import annotations

import json as jsonlib
from datetime import datetime, timezone

import click

from . import (
    admission,
    agent_controller,
    alerts,
    config_controller,
    cron_controller,
    install_backends,
    installer,
    modelserver_controller,
    node_controller,
    service_controller,
    store,
)
from . import profiles as profiles_mod
from . import services as services_mod
from . import sknoded as sknoded_mod
from .explain import explain as explain_kind
from .paths import default_paths, self_node_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _operator() -> store.Writer:
    return store.Writer(role="operator", node=self_node_name(), identity=store.writer_identity())


@click.group(name="fleet")
def fleet() -> None:
    """SKWorld fleet control plane (skfleet)."""


@fleet.command("nodes")
def nodes_cmd() -> None:
    """List all fleet nodes with phase, labels, and capacity."""
    for v in node_controller.node_views(default_paths()):
        labels = ",".join(f"{k}={val}" for k, val in sorted(v.labels.items()))
        cordoned = " CORDONED" if v.cordoned else ""
        age = "never" if v.heartbeat_age_s is None else f"{int(v.heartbeat_age_s)}s"
        click.echo(
            f"{v.name}\t{v.phase}{cordoned}\trole={v.role or '-'}\t[{labels}]\t"
            f"cores={v.capacity.get('cores', '?')} "
            f"ram={v.capacity.get('ram_gb', '?')}GB "
            f"disk={v.capacity.get('disk_gb', '?')}GB\tbeat={age}"
        )


@fleet.command("describe")
@click.argument("kind")
@click.argument("name")
def describe_cmd(kind: str, name: str) -> None:
    """Show the merged object (spec + placement + statuses) as JSON."""
    payload = store.merged(default_paths(), kind, name)
    if payload is None:
        raise click.ClickException(f"no such object: {kind}/{name}")
    click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))


@fleet.command("placements")
@click.option("--kind", "kind", default=None, help="Filter by kind (e.g. job, service).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def placements_cmd(kind: str | None, as_json: bool) -> None:
    """Show current placements with the scheduler's reason for each decision."""
    records = store.list_placements(default_paths(), kind)
    if as_json:
        click.echo(jsonlib.dumps(records, indent=2, sort_keys=True))
        return
    if not records:
        click.echo("no placements")
        return
    for r in records:
        click.echo(
            f"{r['kind'].lower()}/{r['name']}\t-> {r['node']}\t"
            f"gen={r['placementGeneration']}\t{r['reason']}"
        )


@fleet.command("cordon")
@click.argument("name")
def cordon_cmd(name: str) -> None:
    """Mark a node unschedulable."""
    node_controller.cordon(default_paths(), name, True, writer=_operator())
    click.echo(f"{name} cordoned")


@fleet.command("uncordon")
@click.argument("name")
def uncordon_cmd(name: str) -> None:
    """Mark a node schedulable again."""
    node_controller.cordon(default_paths(), name, False, writer=_operator())
    click.echo(f"{name} uncordoned")


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
        f"move them manually (v1 drains never auto-move)",
        level="warn",
    )
    click.echo(f"{name} cordoned (drain)")
    for r in residents:
        click.echo(f"  resident: {r['name']}\tvia={r['via']}\tstate={r['state']}")
    click.echo("manual move required in v1: re-place or migrate each resident, then uncordon")


@fleet.command("explain")
@click.argument("kind", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def explain_cmd(kind: str | None, as_json: bool) -> None:
    """Describe the fleet object model (kinds, fields, conditions, actions)."""
    try:
        payload = explain_kind(kind)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))


@fleet.command("freeze")
@click.option("--reason", default="", help="Why the fleet is frozen.")
def freeze_cmd(reason: str) -> None:
    """Halt ALL fleet actuation (services keep running). Kill-switch on."""
    store.set_frozen(default_paths(), True, writer=_operator(), reason=reason)
    click.echo("fleet FROZEN: actuation halted, services untouched")


@fleet.command("unfreeze")
def unfreeze_cmd() -> None:
    """Kill-switch off: actuation resumes."""
    store.set_frozen(default_paths(), False, writer=_operator())
    click.echo("fleet unfrozen")


@fleet.command("sknoded")
@click.option("--once", is_flag=True, help="One self-report + converge pass, then exit.")
@click.option("--interval", default=sknoded_mod.HEARTBEAT_INTERVAL_S, show_default=True)
@click.option(
    "--actuation-interval",
    "actuation_interval",
    default=None,
    type=int,
    help="Seconds between converge passes (default 30).",
)
def sknoded_cmd(once: bool, interval: int, actuation_interval: int | None) -> None:
    """Run the node agent loop (self-report + Phase 3 converge)."""
    sknoded_mod.main_loop(
        default_paths(),
        self_node_name(),
        interval=interval,
        once=once,
        actuation_interval=actuation_interval,
    )


@fleet.command("apply")
@click.option(
    "-f", "--file", "file_path", required=True, type=click.Path(exists=True, dir_okay=False)
)
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
    if kind == "profile":
        try:
            profiles_mod.normalize_profile_spec(spec)
        except profiles_mod.ProfileSpecError as exc:
            raise click.ClickException(f"invalid profile spec: {exc}") from exc
    try:
        payload = store.write_spec(
            default_paths(), kind, name, spec, writer=_operator(), labels=doc.get("labels")
        )
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
        click.echo(
            f"{r.name}\t-> {r.node or 'unplaced'}\t" f"state={r.state}\tready={r.ready}{flags}"
        )


def _nodes_by_role(paths_) -> dict[str, list[str]]:
    """Node names grouped by their bound spec.role.

    spec.role is owned by card 8258517f; this only READS it, and a node
    object that predates it simply contributes no binding rather than
    erroring, so `get profiles` works before and after that card lands.
    """
    bound: dict[str, list[str]] = {}
    for payload in store.list_specs(paths_, "node"):
        role = (payload.get("spec") or {}).get("role")
        if isinstance(role, str) and role:
            bound.setdefault(role, []).append(payload["name"])
    return {role: sorted(names) for role, names in bound.items()}


def _profile_rows(paths_) -> list[dict]:
    """One display row per Profile object, sorted by name.

    A malformed profile is shown with its error rather than skipped: a
    profile nobody can read is exactly the thing an operator needs to see.
    """
    bound = _nodes_by_role(paths_)
    rows = []
    for payload in store.list_specs(paths_, "profile"):
        name = payload["name"]
        try:
            spec = profiles_mod.normalize_profile_spec(payload.get("spec", {}))
        except profiles_mod.ProfileSpecError as exc:
            rows.append(
                {
                    "name": name,
                    "stateTier": "INVALID",
                    "capauthIdentityClass": str(exc)[:40],
                    "required": "-",
                    "mustNot": "-",
                    "nodes": ",".join(bound.get(name, [])),
                }
            )
            continue
        rows.append(
            {
                "name": name,
                "stateTier": spec["stateTier"],
                "capauthIdentityClass": spec["capauthIdentityClass"],
                "required": len(spec["units"]["required"]),
                "mustNot": len(spec["units"]["mustNot"]),
                "nodes": ",".join(bound.get(name, [])),
            }
        )
    return sorted(rows, key=lambda r: r["name"])


@fleet.command("get")
@click.argument("resource")
def get_cmd(resource: str) -> None:
    """List objects of one kind (currently: cronjobs, modelservers, agents, configs)."""
    if resource == "cronjobs":
        rows = cron_controller.cron_rows(default_paths(), _now_iso())
        if not rows:
            click.echo("no cronjobs")
            return
        click.echo("NAME\tNODE\tSCHEDULE\tENABLED\tLAST\tNEXT\tMISSED")
        for r in rows:
            click.echo(
                f"{r.name}\t{r.node or 'unplaced'}\t{r.schedule}\t{r.enabled}\t"
                f"{r.last_run or 'never'}\t{r.next_run}\t{r.missed}"
            )
        return
    if resource == "modelservers":
        rows = modelserver_controller.modelserver_rows(default_paths(), _now_iso())
        if not rows:
            click.echo("no modelservers")
            return
        click.echo("NAME\tNODE\tPORTS\tSERVING\tVRAM")
        for r in rows:
            ports = ",".join(str(p) for p in r.ports)
            click.echo(f"{r.name}\t{r.node or 'unplaced'}\t{ports}\t{r.serving}\t{r.vram}")
        return
    if resource == "agents":
        rows = agent_controller.agent_rows(default_paths(), _now_iso())
        if not rows:
            click.echo("no agents")
            return
        click.echo("NAME\tNODE\tSOUL\tMODEL\tREADY")
        for r in rows:
            click.echo(
                f"{r.name}\t{r.node or 'unplaced'}\t{r.soul or '-'}\t{r.model or '-'}\t{r.ready}"
            )
        return
    if resource == "configs":
        rows = config_controller.config_rows(default_paths(), _now_iso())
        if not rows:
            click.echo("no configs")
            return
        click.echo("NAME\tNODE\tSECRETS\tDRIFT\tROTATION")
        for r in rows:
            click.echo(
                f"{r.name}\t{r.node or 'unplaced'}\t{r.secrets_present}\t"
                f"{r.drift}\t{r.rotation_overdue}"
            )
        return
    if resource == "profiles":
        rows = _profile_rows(default_paths())
        if not rows:
            click.echo("no profiles")
            return
        click.echo("NAME\tSTATE-TIER\tIDENTITY-CLASS\tREQUIRED\tMUSTNOT\tNODES")
        for r in rows:
            click.echo(
                f"{r['name']}\t{r['stateTier']}\t{r['capauthIdentityClass']}\t"
                f"{r['required']}\t{r['mustNot']}\t{r['nodes'] or '-'}"
            )
        return
    raise click.ClickException(
        f"unknown resource: {resource!r} "
        "(known: cronjobs, modelservers, agents, configs, profiles)"
    )


@fleet.command("reconcile")
def reconcile_cmd() -> None:
    """One ServiceController pass (place-once + failover watch)."""
    out = service_controller.reconcile_once(default_paths(), node=self_node_name())
    click.echo(
        f"placed={len(out['placed'])} kept={len(out['kept'])} "
        f"failovers={len(out['failovers'])} alerted={len(out['alerted'])} "
        f"skipped={len(out['skipped'])}"
    )


@fleet.command("actuation")
@click.argument("name")
@click.option(
    "--enable/--disable",
    "enabled",
    required=True,
    help="Opt this node in or out of actuation (default is report-only).",
)
def actuation_cmd(name: str, enabled: bool) -> None:
    """Toggle sknoded actuation for one node (report-only by default)."""
    try:
        node_controller.set_actuation(default_paths(), name, enabled, writer=_operator())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name} actuation {'ENABLED' if enabled else 'disabled (report-only)'}")


@fleet.command("admit")
@click.argument("name")
@click.option("--label", "labels", multiple=True, help="k=v, repeatable.")
@click.option("--role", "role", default=None, help="Install profile to bind (e.g. worker-gpu).")
@click.option("--preset", is_flag=True, help="Use the known-node preset labels/taints/role.")
@click.option("--bootstrap", is_flag=True, help="First node: admit without a join request.")
def admit_cmd(
    name: str, labels: tuple[str, ...], role: str | None, preset: bool, bootstrap: bool
) -> None:
    """Admit a joining node, minting its node object."""
    label_map = dict(part.split("=", 1) for part in labels) if labels else None
    try:
        spec = admission.admit(
            default_paths(),
            name,
            writer=_operator(),
            labels=label_map,
            role=role,
            preset=preset,
            bootstrap=bootstrap,
        )
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    bound = spec.get("spec", {}).get("role") or "-"
    click.echo(f"admitted {name} (generation {spec['generation']}, role={bound})")


@fleet.command("set-role")
@click.argument("name")
@click.argument("role")
def set_role_cmd(name: str, role: str) -> None:
    """Bind a node to an install profile by name."""
    try:
        spec = node_controller.set_role(default_paths(), name, role, writer=_operator())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name} role={role} (generation {spec['generation']})")


def _resolve_role(paths_, role: str | None) -> str:
    """The role to install for: --role verbatim, or this node's spec.role.

    Mirrors the resolution `_doctor_one` uses for `skfleet node doctor`
    (card 76dad234): a node with no node object, or no bound role, cannot
    be installed for and must say so plainly rather than pass a role of
    None through to `installer.run_install`.
    """
    if role:
        return role
    target = self_node_name()
    views = {v.name: v for v in node_controller.node_views(paths_)}
    view = views.get(target)
    if view is None:
        raise click.ClickException(f"{target}: no such node object")
    if not view.role:
        raise click.ClickException(
            f"{target}: no spec.role set (skfleet set-role {target} <profile>)"
        )
    return view.role


@fleet.command("install")
@click.option(
    "--role", "role", default=None, help="Install profile role (default: this node's spec.role)."
)
@click.option("--check", "check_flag", is_flag=True, help="Report drift only (default mode).")
@click.option(
    "--apply", "apply_flag", is_flag=True, help="Actuate: install missing_required items."
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Apply mode: report what would run, run nothing."
)
@click.option("--enable", is_flag=True, help="Enable installed units.")
@click.option("--start", is_flag=True, help="Start installed units.")
@click.option("--only", "only", multiple=True, help="Limit to this item name (repeatable).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def install_cmd(
    role: str | None,
    check_flag: bool,
    apply_flag: bool,
    dry_run: bool,
    enable: bool,
    start: bool,
    only: tuple[str, ...],
    as_json: bool,
) -> None:
    """Diff (--check, default) or actuate (--apply) this node's install profile.

    Resolves --role from the node's bound spec.role when omitted. `check`
    always just reports (never gated); `apply` is gated by the fleet-wide
    freeze and this node's actuation opt-in (see `skfleet freeze` /
    `skfleet actuation`), and is refused cleanly rather than actuating
    partway.
    """
    if check_flag and apply_flag:
        raise click.ClickException("--check and --apply are mutually exclusive")
    mode = "apply" if apply_flag else "check"

    paths_ = default_paths()
    resolved_role = _resolve_role(paths_, role)

    try:
        summary = installer.run_install(
            paths_,
            resolved_role,
            node=self_node_name(),
            mode=mode,
            dry_run=dry_run,
            enable=enable,
            start=start,
            only=list(only) or None,
            backends=install_backends.default_backends(),
        )
    except installer.ProfileNotApplied as exc:
        raise click.ClickException(
            f"no applied profile named {exc}: `skfleet apply -f <profile.json>` first"
        ) from exc
    except installer.Frozen as exc:
        raise click.ClickException(
            "fleet is FROZEN: actuation halted (skfleet unfreeze to resume)"
        ) from exc
    except installer.ActuationNotAllowed as exc:
        raise click.ClickException(
            f"actuation not enabled for {exc}: `skfleet actuation <name> --enable` first"
        ) from exc

    if as_json:
        click.echo(jsonlib.dumps(summary, indent=2, sort_keys=True))
    else:
        click.echo(f"role={summary['role']}\tmode={summary['mode']}")
        for r in summary["results"]:
            if mode == "check":
                click.echo(f"  {r['grade']:5} {r['category']:28} {r['name']}")
            else:
                click.echo(f"  {r['status']:12} {r['kind']:8} {r['name']}\t{r['detail']}")
        click.echo(f"ok={summary['ok']}")

    if not summary["ok"]:
        raise SystemExit(1)


@fleet.group("node")
def node_group() -> None:
    """Per-node checks (report only)."""


def _profile_for(paths_, role: str):
    """Normalized profile spec for a role, or None when absent/invalid."""
    payload = store.read_spec(paths_, "profile", role)
    if payload is None:
        return None
    try:
        return profiles_mod.normalize_profile_spec(payload.get("spec", {}))
    except profiles_mod.ProfileSpecError:
        return None


def _doctor_one(paths_, name: str, inventory: dict | None) -> tuple[dict | None, str]:
    """(report dict, note). A skip returns (None, reason).

    Args:
        inventory: Observed inventory, or None when the node has published
            none. None and {} are DIFFERENT answers and must not be
            conflated: an empty inventory is a real observation ("nothing is
            enabled here"), while an absent one means the node has not
            reported yet. Passing an absent inventory to the diff would grade
            a healthy node as missing everything, which is the one verdict
            nodeinventory exists to never produce.

    The checks are ordered by how actionable they are. A node with no role
    cannot be graded no matter what it published, so that note wins over the
    inventory note.
    """
    from . import profile_doctor

    views = {v.name: v for v in node_controller.node_views(paths_)}
    view = views.get(name)
    if view is None:
        return None, f"{name}: no such node object"
    if not view.role:
        return None, f"{name}: no spec.role set (skfleet set-role {name} <profile>)"
    profile = _profile_for(paths_, view.role)
    if profile is None:
        return None, f"{name}: no valid profile object named {view.role!r}"
    if inventory is None:
        return None, (
            f"{name}: has published no inventory yet "
            "(needs a sknoded pass on a build carrying card 1f5397f0)"
        )
    report = profile_doctor.diff(inventory, profile)
    payload = report.as_dict()
    payload["node"] = name
    payload["role"] = view.role
    payload["findings"] = [{"grade": g, "category": c, "name": n} for g, c, n in report.findings()]
    return payload, ""


@node_group.command("doctor")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--all", "all_nodes", is_flag=True, help="Every node, from published inventories.")
@click.option("--strict", is_flag=True, help="Exit 1 on error-grade findings.")
def node_doctor_cmd(name: str | None, as_json: bool, all_nodes: bool, strict: bool) -> None:
    """Report install-profile drift for a node. REPORT ONLY: changes nothing.

    With no NAME, collects this node's live inventory locally. With --all,
    reads each node's published inventory from its status file instead, so
    no ssh is needed. A node with no role or no matching profile is skipped
    with a note on stderr, never a whole-run failure: an unbound node is a
    legitimate state, not an error.
    """
    from . import nodeinventory

    paths_ = default_paths()
    reports: list[dict] = []
    notes: list[str] = []

    if all_nodes:
        for view in node_controller.node_views(paths_):
            status = (store.read_node_file(paths_, view.name, "node.json") or {}).get("status", {})
            # `.get("inventory")` would collapse absent into empty; see the
            # note in _doctor_one for why that grades healthy nodes as broken.
            published = status["inventory"] if "inventory" in status else None
            report, note = _doctor_one(paths_, view.name, published)
            (reports.append(report) if report else notes.append(note))
    else:
        target = name or self_node_name()
        report, note = _doctor_one(paths_, target, nodeinventory.collect())
        (reports.append(report) if report else notes.append(note))

    for note in notes:
        click.echo(f"skipped {note}", err=True)

    if as_json:
        click.echo(jsonlib.dumps(reports, indent=2, sort_keys=True))
    elif not reports:
        click.echo("no nodes to report on")
    else:
        for payload in reports:
            click.echo(
                f"\n{payload['node']}\trole={payload['role']}\t{payload['severity'].upper()}"
            )
            if not payload["findings"]:
                click.echo("  (clean)")
            for finding in payload["findings"]:
                click.echo(f"  {finding['grade']:5} {finding['category']:28} {finding['name']}")
        worst = [p["severity"] for p in reports]
        click.echo(
            f"\n{len(reports)} node(s), "
            f"{sum(1 for s in worst if s == 'error')} error, "
            f"{sum(1 for s in worst if s == 'warn')} warn, "
            f"{sum(1 for s in worst if s == 'ok')} clean"
        )

    # Report-only by default: drift is information, not a failure. --strict
    # is the opt-in that makes error-grade findings gate something.
    if strict and any(p["severity"] == "error" for p in reports):
        raise SystemExit(1)


def _parse_taint(spec: str) -> tuple[str, str, str]:
    """Split a KEY=VALUE:EFFECT taint argument, e.g. travel=true:NoSchedule."""
    key, sep, rest = spec.partition("=")
    value, sep2, effect = rest.partition(":")
    if not (sep and sep2) or not key:
        raise click.ClickException(
            f"malformed taint {spec!r}: want KEY=VALUE:EFFECT, e.g. travel=true:NoSchedule"
        )
    return key, value, effect


@fleet.command("taint")
@click.argument("name")
@click.argument("taint")
def taint_cmd(name: str, taint: str) -> None:
    """Add or replace one taint on a node: KEY=VALUE:EFFECT.

    Re-tainting a key replaces that entry, it never appends a duplicate.
    """
    key, value, effect = _parse_taint(taint)
    try:
        spec = node_controller.set_taint(
            default_paths(), name, key, value, effect, writer=_operator()
        )
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{name} tainted {key}={value}:{effect} (generation {spec['generation']})")


@fleet.command("untaint")
@click.argument("name")
@click.argument("key")
def untaint_cmd(name: str, key: str) -> None:
    """Remove the taint with this KEY from a node (a no-op when absent)."""
    paths_ = default_paths()
    before = store.read_spec(paths_, "node", name)
    try:
        spec = node_controller.clear_taint(paths_, name, key, writer=_operator())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    if before is not None and spec["generation"] == before["generation"]:
        click.echo(f"{name} has no {key} taint (nothing to do)")
        return
    click.echo(f"{name} untainted {key} (generation {spec['generation']})")


@fleet.group("control-bus")
def control_bus_group() -> None:
    """The scoped skfleet-control folder: its scope contract and its budget."""


@control_bus_group.command("audit")
@click.option("--budget", "budget", default=None, help="Byte budget, e.g. 10MB, 512KB or 4096.")
@click.option("--top", default=10, show_default=True, help="How many largest files to name.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--stignore", is_flag=True, help="Print a recommended .stignore body and exit.")
def control_bus_audit_cmd(budget: str | None, top: int, as_json: bool, stignore: bool) -> None:
    """Measure the fleet tree against the control-bus budget and scope.

    READ ONLY: writes nothing, so it is safe on any node including the one
    it is judging. Exits 1 when the tree is over budget or when any path
    outside the five known classes appears.
    """
    from . import control_bus_audit as audit_mod

    if stignore:
        click.echo(audit_mod.stignore_body(), nl=False)
        return

    try:
        limit = audit_mod.parse_size(budget) if budget else audit_mod.DEFAULT_BUDGET_BYTES
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--budget") from exc

    report = audit_mod.audit(default_paths(), budget=limit, top=top)
    if as_json:
        click.echo(jsonlib.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        click.echo(audit_mod.render(report))
    if not report.ok:
        raise SystemExit(1)


def register_fleet_commands(main: click.Group) -> None:
    """Register the fleet group on the skcapstone CLI."""
    main.add_command(fleet)


def main() -> None:
    """Console script entry point (skfleet)."""
    fleet()
