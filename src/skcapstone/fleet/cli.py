"""The skfleet CLI: fleet inventory, cordon, freeze, explain, sknoded.

Available standalone as `skfleet` and as `skcapstone fleet ...`.
"""

from __future__ import annotations

import json as jsonlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

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
    seat_audit,
    service_controller,
    staged_rollout,
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
        click.echo(f"{r.name}\t-> {r.node or 'unplaced'}\tstate={r.state}\tready={r.ready}{flags}")


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
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

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


def _published_inventory(paths_, name: str) -> dict | None:
    """What a node last published, or None when it has published nothing.

    None and {} are different answers: an empty inventory is a real
    observation, an absent one means the node has not reported yet.
    """
    status = (store.read_node_file(paths_, name, "node.json") or {}).get("status", {})
    return status["inventory"] if "inventory" in status else None


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
            report, note = _doctor_one(paths_, view.name, _published_inventory(paths_, view.name))
            (reports.append(report) if report else notes.append(note))
    else:
        target = name or self_node_name()
        # Only THIS node can be inventoried live. Naming another node and
        # grading the local units against that node's profile produces a
        # confident wrong answer, which is worse than no answer: it reads
        # exactly like a real report. For any other node, use what that node
        # published, the same source --all uses.
        inventory = (
            nodeinventory.collect()
            if target == self_node_name()
            else _published_inventory(paths_, target)
        )
        report, note = _doctor_one(paths_, target, inventory)
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


def _stignore_rulesets(paths_):
    """Every known sync-folder ruleset, folder objects merged over built-ins.

    Keyed by FOLDER ID on purpose. Role is the wrong key: two roles can join
    one folder, and a per-role ruleset would let them disagree about what
    must never leave a node, which makes the no-secrets invariant per-node.
    """
    from . import stignore_doctor

    folder_ids = set(stignore_doctor.DEFAULT_RULESETS)
    folder_ids.update(
        payload["name"]
        for payload in store.list_specs(paths_, "syncfolder")
        if payload.get("name")
    )
    out = []
    for folder_id in sorted(folder_ids):
        payload = store.read_spec(paths_, "syncfolder", folder_id) or {}
        out.append(stignore_doctor.ruleset_from_spec(folder_id, payload.get("spec")))
    return out


@node_group.command("stignore")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--strict", is_flag=True, help="Exit 1 on error-grade findings.")
def node_stignore_cmd(as_json: bool, strict: bool) -> None:
    """Report Syncthing ignore-rule drift on this node. REPORT ONLY.

    Checks each sovereign sync folder this node actually holds for the rules
    that keep private key material from being announced to peers. A folder
    whose root is not on this host is skipped: a folder a node does not hold
    cannot leak through it.

    Deliberately a SIBLING of `node doctor` rather than part of it. `doctor`
    diffs a ROLE profile against a published inventory and skips any node
    with no role bound; this invariant is keyed by folder and applies to a
    role-less node exactly as much as to a control node.
    """
    from . import stignore_doctor

    paths_ = default_paths()
    reports: list[dict] = []
    notes: list[str] = []
    for ruleset in _stignore_rulesets(paths_):
        report = stignore_doctor.check_folder(ruleset)
        if report is None:
            notes.append(f"{ruleset.folder_id}: not held on this node")
        else:
            reports.append(report.as_dict())

    for note in notes:
        click.echo(f"skipped {note}", err=True)

    if as_json:
        click.echo(jsonlib.dumps(reports, indent=2, sort_keys=True))
    elif not reports:
        click.echo("no sovereign sync folders on this node")
    else:
        for payload in reports:
            click.echo(f"\n{payload['folder']}\t{payload['root']}\t{payload['severity'].upper()}")
            if not payload["present"]:
                click.echo("  error no_stignore                 (folder has no ignore rules)")
            for name in payload["missing_required"]:
                click.echo(f"  error missing_required_ignore     {name}")
            for name in payload["missing_recommended"]:
                click.echo(f"  warn  missing_recommended_ignore  {name}")
            if payload["severity"] == "ok":
                click.echo("  (clean)")

    if strict and any(p["severity"] == "error" for p in reports):
        raise SystemExit(1)


@node_group.command("endpoint-audit")
@click.option(
    "--status-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read a captured tailscale status JSON file instead of the local CLI.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--strict", is_flag=True, help="Exit 1 when endpoint routing is unsafe.")
def node_endpoint_audit_cmd(status_file, as_json: bool, strict: bool) -> None:
    """Reconcile fleet node endpoints with Tailscale peers. REPORT ONLY."""
    from . import endpoint_audit

    try:
        report = endpoint_audit.audit(default_paths(), endpoint_audit.read_status(status_file))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(jsonlib.dumps(report, indent=2, sort_keys=True))
    elif not report["reports"]:
        click.echo("no Tailscale peers matched fleet node identities")
    else:
        for node in report["reports"]:
            click.echo(
                f"{node['node']}\t{node['severity'].upper()}\t"
                f"safe_to_route={str(node['safe_to_route']).lower()}"
            )
            for finding in node["findings"]:
                click.echo(f"  {finding['severity']:5} {finding['kind']}")
            for peer_id in node["retirement_candidates"]:
                click.echo(f"  plan  retirement_candidate        {peer_id}")
    if strict and report["summary"]["unsafe"]:
        raise SystemExit(1)


#: Artifacts whose ``missing`` finding is never a role difference.
#:
#: ``git_sha`` is the pre-rename name of ``package:git_sha``; a controller
#: running this code may be gating a node that still reports the old name
#: over ``fleet node drift --json``, and a rename must not quietly turn an
#: unambiguous finding into a suppressed one mid-rollout.
#:
#: ``checkout:git_sha`` missing means git could not read the checkout at
#: all, and ``package:git_sha`` missing means the installed distribution
#: could not be read at all. Neither is a per-unit question about this
#: host's role, so neither is ever ambiguous.
_UNAMBIGUOUS_MISSING_ARTIFACTS = frozenset({"git_sha", "package:git_sha", "checkout:git_sha"})


def _drift_is_role_ambiguous(drift) -> bool:
    """True when ``drift`` cannot be told from a legitimate role difference.

    See ``.superpowers/sdd/2026-09-17-rollout-observability/task-5-brief.md``
    Part 1, and the incident it names: run against a real workstation or
    against chiap01/chiap03, most of the ``missing`` findings on a shipped
    unit or the dispatcher script are units that host's ROLE never installs
    on purpose, not a rollout gap. This estate has no per-host role
    manifest, so nothing here can tell those two apart -- and Task 3/4's own
    documented rule is that ``unit_in_scope`` returning False already
    suppresses the cases it CAN judge; what reaches ``detect_drift`` as
    ``missing`` is exactly the residue that is genuinely undecidable.

    ``changed``, ``enablement_mismatch``, and ``failed`` are never
    ambiguous: the artifact is demonstrably installed, so a content or
    activation difference is a fact about this host, not a guess about its
    role. A ``missing`` finding on ``git_sha`` is not ambiguous either: it
    means the installed distribution itself could not be read at all,
    which is not a per-unit role question.

    Neither is a ``missing`` finding on a ``script:``-prefixed artifact
    (a ``pyproject.toml`` ``script-files`` entry, Fix 4 of the same
    review). Unlike a shipped unit, which a host's role may legitimately
    never install, pip installs every ``script-files`` entry into
    ``~/.skenv/bin`` unconditionally on every host that has the package
    installed at all -- there is no role for which a script-files entry is
    supposed to be absent. Measured live: ``skfleet_readiness.py`` is
    genuinely missing from chiap01's ``~/.skenv/bin``, which is exactly
    the kind of finding this default output exists to surface, not bury.
    """
    return (
        drift.kind == "missing"
        and drift.artifact not in _UNAMBIGUOUS_MISSING_ARTIFACTS
        and not drift.artifact.startswith("script:")
    )


def _default_repo_root() -> Path:
    """The checkout `node drift` reads EXPECTED content from.

    ``SKCAPSTONE_REPO_ROOT`` when set, otherwise ``~/work/skcapstone``: the
    shared-checkout convention documented in
    ``docs/runbooks/chatgpt-codex-sk-client.md`` and confirmed, read-only,
    on the live fleet (chiap01/02/03/04/08) -- every host that has a
    checkout to diagnose itself against keeps it there. Deliberately not
    ``install_backends._repos_root()`` (``~/clawd/skcapstone-repos``): that is
    a different, developer-workstation convention, and no rotate host in
    the live fleet has anything under it.
    """
    env = os.environ.get("SKCAPSTONE_REPO_ROOT")
    return Path(env).expanduser() if env else Path.home() / "work" / "skcapstone"


@node_group.command("drift")
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="Checkout with expected content (default: $SKCAPSTONE_REPO_ROOT or ~/work/skcapstone)",
)
@click.option(
    "--home",
    "home",
    type=click.Path(path_type=Path),
    default=None,
    help="Estate home to inspect for installed artifacts (default: $HOME).",
)
@click.option(
    "--expect-git-sha",
    "expect_git_sha",
    default=None,
    help=(
        "Pin the commit this node is supposed to be on. Without it, the manifest is "
        "built from --repo-root and the checkout can only ever agree with itself."
    ),
)
@click.option(
    "--fleet",
    "include_fleet",
    is_flag=True,
    help=(
        "Also report nodes that disagree with each OTHER, read from the rollout "
        "history every node already publishes to the shared fleet tree."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--strict", is_flag=True, help="Exit 1 when any drift is found.")
def node_drift_cmd(
    repo_root: Path | None,
    home: Path | None,
    expect_git_sha: str | None,
    include_fleet: bool,
    as_json: bool,
    strict: bool,
) -> None:
    """Report rollout drift for this node against a fresh manifest. REPORT ONLY.

    Builds a deployment manifest (Task 1's ``build_manifest``) from
    --repo-root and compares it against what this node actually has
    installed (Task 3's ``detect_drift``): content digests for unit files
    and the dispatcher script, the installed distribution's embedded
    git_sha, and enabled-versus-active state, never a version label alone.
    See ``fleet/rollout_drift.py`` for why: a version check reported every
    host healthy during all four real drift incidents this exists to catch.

    Local only, like ``node doctor``: both the manifest and the installed
    state it is compared against are facts about THIS machine. Grading a
    remote node from a local repo checkout would be a confident wrong
    answer dressed up as a report, not a report.

    Cheap enough to run from a systemd timer independently of the rest of
    ``skcapstone doctor``: every check here is a file read, a glob, or a
    handful of ``systemctl --user show`` calls, the same read-only cost
    Task 2's readiness gate already pays every 15 minutes.

    Default text output shows every ``changed`` and ``enablement_mismatch``
    finding, plus any ``git_sha`` finding, by name: those are unambiguous
    drift regardless of what this host's role is. A ``missing`` unit or
    dispatcher script is summarised as a count instead of printed by name,
    because this estate has no per-host role manifest, so "this role never
    installs it" cannot be told apart from "a rollout should have installed
    it and did not" -- see ``_drift_is_role_ambiguous``. ``--json`` and
    ``--strict`` are unaffected by this split: both see and act on every
    finding, missing included.
    """
    from . import deployment_manifest, rollout_drift

    resolved_repo_root = repo_root or _default_repo_root()
    resolved_home = home or Path.home()

    try:
        manifest = deployment_manifest.build_manifest(resolved_repo_root, resolved_home)
        if expect_git_sha:
            # Replace the self-derived sha with the caller's pin and mark it
            # as pinned, which is what unlocks the checkout:git_sha surface
            # in detect_drift. Without this the manifest's git_sha came from
            # this very checkout, so comparing the checkout against it is a
            # tautology that can never fail -- the exact blind spot that let
            # five uniformly-stale hosts report clean.
            manifest = dict(manifest)
            manifest["git_sha"] = expect_git_sha
            manifest["checkout_git_sha_pinned"] = True
        drifts = rollout_drift.detect_drift(manifest, resolved_home, resolved_repo_root)
        if include_fleet:
            # Deliberately opt-in, and deliberately NOT inside detect_drift:
            # the staged rollout gates each node with detect_drift, and during
            # a staged rollout the nodes are SUPPOSED to disagree (node 1
            # deployed, node 5 not yet). Folding this in unconditionally would
            # make every staged rollout fail at its second node. It is still
            # the same command and the same Drift records, so --json and
            # --strict handle it with no new reporting path.
            drifts = drifts + rollout_drift.detect_fleet_incoherence(resolved_home)
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(
            f"could not compute drift from repo root {resolved_repo_root}: {exc}"
        ) from exc

    node = self_node_name()
    if as_json:
        click.echo(
            jsonlib.dumps(
                {
                    "node": node,
                    "git_sha": manifest["git_sha"],
                    "drifts": [
                        {
                            "artifact": d.artifact,
                            "kind": d.kind,
                            "expected": d.expected,
                            "found": d.found,
                            "host": d.host,
                        }
                        for d in drifts
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif not drifts:
        click.echo(f"{node}: no drift (matches {manifest['git_sha']})")
    else:
        ambiguous = [d for d in drifts if _drift_is_role_ambiguous(d)]
        unambiguous = [d for d in drifts if not _drift_is_role_ambiguous(d)]
        click.echo(
            f"{node}\t{len(drifts)} drift(s) against manifest git_sha={manifest['git_sha']}"
        )
        if not unambiguous:
            click.echo("  (no unambiguous drift: no changed content, enablement, or git_sha)")
        for d in unambiguous:
            # fleet: findings are about a PEER, so they name the host they
            # are about; every other finding is about this node.
            where = f" on {d.host}" if d.artifact.startswith("fleet:") else ""
            click.echo(
                f"  {d.kind:20} {d.artifact:40} expected={d.expected!r} "
                f"found={d.found!r}{where}"
            )
        if ambiguous:
            click.echo(
                f"  {len(ambiguous)} unit(s)/dispatcher script reported missing, not listed: "
                "this estate has no per-host role manifest, so 'this host's role never "
                "installs it' cannot be told apart from 'a rollout should have installed "
                "it and did not'. Re-run with --json to see each by name."
            )

    if strict and drifts:
        raise SystemExit(1)


@node_group.command("manifest")
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="Checkout to build the manifest from (default: $SKCAPSTONE_REPO_ROOT or "
    "~/work/skcapstone)",
)
@click.option(
    "--home",
    "home",
    type=click.Path(path_type=Path),
    default=None,
    help="Estate home to publish under (default: $HOME).",
)
@click.option("--json", "as_json", is_flag=True, help="Print the published manifest as JSON.")
def node_manifest_cmd(repo_root: Path | None, home: Path | None, as_json: bool) -> None:
    """Build this node's deployment manifest and publish it to disk. WRITES.

    ``deployment_manifest.write_manifest`` had no non-test caller before
    this command: the manifest pipeline (Task 1) built a fresh manifest in
    memory every time it was needed, but nothing ever pinned one to a
    well-known path (``docs/fleet/rollout-drift.md`` said this outright).
    This command is that caller: it builds the same manifest ``node drift``
    builds (Task 1's ``build_manifest``) and publishes it to the FLEET tree
    (``default_paths()``, i.e. the ``$SKFLEET_ROOT`` override or its
    documented default -- see ``fleet/paths.py``) at
    ``status/node-<node>/manifest/manifest.json``, the same
    ``status/<node>/<kind>/<name>.json`` shape every other status write in
    this package already uses (``fleet/paths.py``). Publishing through
    ``default_paths()`` rather than a path built from ``--home`` here is
    deliberate: this is fleet STATE, and ``paths.py`` is the one module
    allowed to name where the fleet tree lives (see
    ``tests/fleet/test_root_relocation.py``), so a relocated
    ``SKFLEET_ROOT`` must relocate this too.

    Unlike ``node drift`` / ``node doctor``, this command WRITES on
    purpose -- publishing is its entire job, so it carries no
    ``--strict``/report-only contract to violate. ``node drift`` itself
    still compares against a freshly built manifest, not this pinned one
    (deliberately unchanged here); publishing a comparison artifact a
    future check could read against is exactly what closes the "no
    caller" gap without also changing what ``node drift`` means today.
    """
    from . import deployment_manifest

    resolved_repo_root = repo_root or _default_repo_root()
    resolved_home = home or Path.home()

    try:
        manifest = deployment_manifest.build_manifest(resolved_repo_root, resolved_home)
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(
            f"could not build a manifest from repo root {resolved_repo_root}: {exc}"
        ) from exc

    node = self_node_name()
    manifest_path = default_paths().status_path(node, "manifest", "manifest")
    deployment_manifest.write_manifest(manifest_path, manifest)

    if as_json:
        click.echo(jsonlib.dumps(manifest, indent=2, sort_keys=True))
    else:
        click.echo(f"{node}: published manifest git_sha={manifest['git_sha']} to {manifest_path}")


def _staged_result_as_dict(result) -> dict:
    """A ``RolloutResult`` or ``RollbackResult`` (same shape) as a JSON-safe
    dict. Shared because both carry identical fields
    (``dry_run``/``completed``/``halted_at``/``reason``/``remaining``).
    """
    return {
        "dry_run": result.dry_run,
        "completed": [
            {
                "node": r.node,
                "dry_run": r.dry_run,
                "deployed": r.deployed,
                "ready": r.ready,
                "drift": [
                    {
                        "artifact": d.artifact,
                        "kind": d.kind,
                        "expected": d.expected,
                        "found": d.found,
                    }
                    for d in r.drift
                ],
                "detail": r.detail,
            }
            for r in result.completed
        ],
        "halted_at": result.halted_at,
        "reason": result.reason,
        "remaining": list(result.remaining),
    }


def _render_staged_result(result, as_json: bool, *, noun: str) -> None:
    """Render a rollout/rollback result: every node in plan order, numbered,
    so a human can read the whole plan (or the whole outcome) top to bottom
    before deciding whether to authorise ``--apply``.

    ``noun`` is "rollout" or "rollback", used only in the summary lines.
    """
    if as_json:
        click.echo(jsonlib.dumps(_staged_result_as_dict(result), indent=2, sort_keys=True))
        return

    total = len(result.completed) + (1 if result.halted_at else 0) + len(result.remaining)

    if result.dry_run:
        click.echo(f"DRY RUN: previewing {noun} across {total} node(s); nothing was executed.")
        click.echo("Pass --apply to run this for real.")
    elif result.halted_at is None:
        click.echo(f"{noun} complete: all {total} node(s) deployed and gate passed.")
    else:
        click.echo(f"{noun} HALTED after {len(result.completed)}/{total} node(s).")

    index = 0
    for r in result.completed:
        index += 1
        click.echo(f"  [{index}/{total}] {r.node}\t{r.detail}")
    if result.halted_at is not None:
        index += 1
        click.echo(f"  [{index}/{total}] {result.halted_at}\tHALTED: {result.reason}")
        for node in result.remaining:
            index += 1
            click.echo(
                f"  [{index}/{total}] {node}\tnot attempted; {noun} halted before reaching it"
            )


@fleet.command("rollout")
@click.option(
    "--node",
    "nodes",
    multiple=True,
    required=True,
    help="A node to roll out to, in the order given. Repeat for each node, "
    "e.g. --node chiap01 --node chiap02 --node chiap03.",
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="Checkout to build the manifest from, and to compare THIS machine "
    "against when it is one of the target nodes (default: "
    "$SKCAPSTONE_REPO_ROOT or ~/work/skcapstone).",
)
@click.option(
    "--home",
    "home",
    type=click.Path(path_type=Path),
    default=None,
    help="Estate home to read/write node-scoped state under (default: $HOME).",
)
@click.option(
    "--remote-repo-root",
    "remote_repo_root",
    default=staged_rollout.DEFAULT_REMOTE_REPO_ROOT,
    help="Checkout path on each REMOTE node's own filesystem (default: "
    f"{staged_rollout.DEFAULT_REMOTE_REPO_ROOT}).",
)
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    help="Execute for real. Without this flag the rollout is a DRY RUN: no "
    "deploy, gate, or record call is made for any node.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--strict",
    is_flag=True,
    help="Exit 1 when the rollout halts before every node completes.",
)
def rollout_cmd(
    nodes: tuple[str, ...],
    repo_root: Path | None,
    home: Path | None,
    remote_repo_root: str,
    apply_flag: bool,
    as_json: bool,
    strict: bool,
) -> None:
    """Roll the manifest built from --repo-root out to NODES, one at a time.

    DRY RUN BY DEFAULT: this command previews the plan and executes nothing
    unless --apply is given. This is a human-invoked mechanism, not an
    autonomous actuator; nothing schedules it, and it stops itself at the
    first node that fails rather than continuing past a problem.

    Visits --node arguments in the exact order given (repeat --node once per
    node). For each node in turn: record the manifest now in force (so a
    later rollback has something to return to), then deploy (git pull, pip
    install, copy the dispatcher script, converge) and gate (the existing
    readiness verdict plus `fleet node drift`'s own no-unambiguous-drift
    rule -- no second notion of "healthy" is invented here). The first node
    that fails to deploy or fails its gate HALTS the rollout: every later
    node is left untouched, never even attempted, and the command reports
    which node stopped it and why.

    --apply is the only way anything here writes to a host. Without it, no
    network call and no filesystem write happens for any node: this previews
    what deploying and gating would do, it does not run a real health check
    dressed up as a preview.

    --strict sets a non-zero exit code when the rollout halts before every
    node completes; it does not change the output (matching `node drift` and
    `node doctor`). Re-run with --json for the full machine-readable result,
    including every node's readiness and drift detail.
    """
    from . import deployment_manifest

    resolved_repo_root = repo_root or _default_repo_root()
    resolved_home = home or Path.home()

    try:
        manifest = deployment_manifest.build_manifest(resolved_repo_root, resolved_home)
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(
            f"could not build a manifest from repo root {resolved_repo_root}: {exc}"
        ) from exc

    try:
        plan = staged_rollout.plan_rollout(nodes, manifest)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    result = staged_rollout.execute_rollout(
        plan,
        dry_run=not apply_flag,
        home=resolved_home,
        remote_repo_root=remote_repo_root,
        local_repo_root=resolved_repo_root,
    )

    _render_staged_result(result, as_json, noun="rollout")

    if strict and result.halted_at is not None:
        raise SystemExit(1)


@fleet.command("rollback")
@click.option(
    "--node",
    "nodes",
    multiple=True,
    required=True,
    help="A node to roll back, in the order given. Repeat for each node, "
    "e.g. --node chiap01 --node chiap02 --node chiap03.",
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(path_type=Path),
    default=None,
    help="Checkout to compare THIS machine against when it is one of the "
    "target nodes (default: $SKCAPSTONE_REPO_ROOT or ~/work/skcapstone).",
)
@click.option(
    "--home",
    "home",
    type=click.Path(path_type=Path),
    default=None,
    help="Estate home to read/write node-scoped state under (default: $HOME).",
)
@click.option(
    "--remote-repo-root",
    "remote_repo_root",
    default=staged_rollout.DEFAULT_REMOTE_REPO_ROOT,
    help="Checkout path on each REMOTE node's own filesystem (default: "
    f"{staged_rollout.DEFAULT_REMOTE_REPO_ROOT}).",
)
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    help="Execute for real. Without this flag the rollback is a DRY RUN: no "
    "lookup, deploy, gate, or record call is made for any node.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option(
    "--strict",
    is_flag=True,
    help="Exit 1 when the rollback halts before every node completes.",
)
def rollback_cmd(
    nodes: tuple[str, ...],
    repo_root: Path | None,
    home: Path | None,
    remote_repo_root: str,
    apply_flag: bool,
    as_json: bool,
    strict: bool,
) -> None:
    """Return NODES to whatever manifest each was running before its last
    recorded rollout, one at a time.

    DRY RUN BY DEFAULT: --apply is required to execute for real, the same
    contract `rollout` uses.

    There is no --manifest option: rollback's target is never chosen on the
    command line. Each node's "previous manifest" is looked up per node, at
    rollback time, from what `rollout` actually recorded for THAT node via
    `record_deployment` -- never guessed, never reconstructed from "current
    minus one commit". A NODE THAT HAS NO RECORDED PREVIOUS MANIFEST CAUSES
    ROLLBACK TO REFUSE OUTRIGHT for that node: it halts there, with a reason
    naming the node, exactly like any other halting failure, and every later
    node is left untouched. This is deliberate: this estate's only existing
    rollback practice before this command was hand-written `card_events`
    evidence with no code behind it, and a refusal is a fact where a guess
    would have been a liability.

    Rollback re-runs the same gate the forward `rollout` path uses, after
    every node, including after a successful rollback: a rollback is
    remediation run under pressure on a fleet already known to be unhealthy,
    which makes verifying each node MORE important, not a place to cut. When
    the gate fails after a rollback, the node's rollback has already
    happened; nothing here undoes it (there is no rollback-of-a-rollback).
    What halts is only the ADVANCE to later nodes, reported with a reason
    that says "after rollback" so it is never mistaken for a forward-deploy
    failure.

    --strict sets a non-zero exit code when the rollback halts before every
    node completes; it does not change the output. Re-run with --json for
    the full machine-readable result.
    """
    try:
        plan = staged_rollout.plan_rollback(nodes)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved_repo_root = repo_root or _default_repo_root()
    resolved_home = home or Path.home()

    result = staged_rollout.execute_rollback(
        plan,
        dry_run=not apply_flag,
        home=resolved_home,
        remote_repo_root=remote_repo_root,
        local_repo_root=resolved_repo_root,
    )

    _render_staged_result(result, as_json, noun="rollback")

    if strict and result.halted_at is not None:
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


@fleet.command("seat-audit")
@click.option("--strict", is_flag=True, help="Exit 1 when more than one operator seat is found.")
def seat_audit_cmd(strict: bool) -> None:
    """Report how many operator seats have written to this store.

    Catches the two-seat case the Syncthing conflict detector misses. Measured
    in the promotion drill: two seats writing with a sync between them produced
    10 writes and ZERO conflict files, so a quiet conflict directory is not
    evidence of a single writer.

    CURRENT-STATE ONLY. write_spec emits no event, so a second seat that wrote
    and was later overwritten leaves no trace anywhere. A clean result means
    "no second seat is represented in the objects as they stand", not "no
    second seat has been writing".
    """
    audit = seat_audit.audit_seats(default_paths())
    click.echo(audit.summary())
    for node in audit.seats:
        refs = audit.by_node[node]
        click.echo(f"  {node}: {len(refs)} object(s)")
        for ref in refs[:10]:
            click.echo(f"    {ref}")
        if len(refs) > 10:
            click.echo(f"    ... and {len(refs) - 10} more")
    if audit.unattributed:
        click.echo(f"  unattributed (no writer block): {len(audit.unattributed)}")
    if strict and not audit.ok:
        raise SystemExit(1)


@fleet.command("label")
@click.argument("name")
@click.argument("labels", nargs=-1)
@click.option("--remove", "remove", multiple=True, help="Label KEY to drop (repeatable).")
def label_cmd(name: str, labels: tuple[str, ...], remove: tuple[str, ...]) -> None:
    """Add, change or drop labels on a node: KEY=VALUE ... [--remove KEY].

    Merges. Every other field of the spec is preserved, which `skfleet apply`
    does NOT do: apply replaces the whole spec from the document you hand it,
    so a label-only apply drops taints, cordoned and address and exits 0.

    Labels decide placement. `scheduler.feasible` filters on them and never
    reads `spec.role`, so changing a role does not change what can be
    scheduled on a node; changing its labels does.
    """
    if not labels and not remove:
        raise click.ClickException("nothing to do: pass KEY=VALUE and/or --remove KEY")
    add: dict[str, str] = {}
    for item in labels:
        if "=" not in item:
            raise click.ClickException(f"malformed label {item!r}: want KEY=VALUE, e.g. gpu=true")
        key, _, value = item.partition("=")
        add[key] = value
    paths_ = default_paths()
    before = store.read_spec(paths_, "node", name)
    try:
        spec = node_controller.set_labels(
            paths_, name, add=add, remove=tuple(remove), writer=_operator()
        )
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if before is not None and spec["labels"] == before.get("labels", {}):
        click.echo(f"{name} labels already as requested (nothing to do)")
        return
    shown = ",".join(f"{k}={v}" for k, v in sorted(spec["labels"].items())) or "(none)"
    click.echo(f"{name} labels [{shown}] (generation {spec['generation']})")


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


@fleet.group("drill")
def drill_group() -> None:
    """Scratch-fleet promotion drill. NEVER touches the live fleet tree.

    Every subcommand requires an explicit --root. There is deliberately no
    default and SKFLEET_ROOT is never read as the drill target: on a control
    node that variable points at production.
    """


#: --root is required on every drill subcommand for exactly one reason: an
#: omitted root must be an error, never a fallback to the live tree.
_drill_root_opt = click.option(
    "--root",
    required=True,
    help="Scratch fleet root. Must be outside the sovereign home and created by this harness.",
)


@drill_group.command("create")
@_drill_root_opt
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def drill_create_cmd(root: str, as_json: bool) -> None:
    """Build a populated throwaway fleet tree at --root."""
    from . import drill as drill_mod

    try:
        fleet_handle = drill_mod.create(root)
    except (drill_mod.UnsafeDrillRootError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    payload = drill_mod.summary(fleet_handle)
    if as_json:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"drill tree ready at {payload['root']}")
    click.echo(f"point the CLI at it with: export SKFLEET_ROOT={payload['root']}")
    for node, phase in sorted(payload["phases"].items()):
        click.echo(f"  {node}\t{phase}\trole={payload['roles'].get(node) or '-'}")


@drill_group.command("status")
@_drill_root_opt
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def drill_status_cmd(root: str, as_json: bool) -> None:
    """Show phases and bound roles inside a drill tree."""
    from . import drill as drill_mod

    try:
        payload = drill_mod.summary(drill_mod.attach(root))
    except drill_mod.UnsafeDrillRootError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
        return
    for node, phase in sorted(payload["phases"].items()):
        click.echo(f"{node}\t{phase}\trole={payload['roles'].get(node) or '-'}")


@drill_group.command("kill-control")
@_drill_root_opt
def drill_kill_control_cmd(root: str) -> None:
    """Age the control seat's heartbeat until its phase derives as Dead."""
    from . import drill as drill_mod

    try:
        step = drill_mod.attach(root).kill_control()
    except drill_mod.UnsafeDrillRootError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{step.action}: {step.detail}")
    click.echo(f"  revert: {step.revert}")


@drill_group.command("promote")
@_drill_root_opt
@click.option("--force", is_flag=True, help="Promote even while the seat is still alive.")
@click.option("--revert", is_flag=True, help="Undo a previous promote instead.")
def drill_promote_cmd(root: str, force: bool, revert: bool) -> None:
    """Run (or revert) the promotion runbook inside the drill tree."""
    from . import drill as drill_mod

    try:
        handle = drill_mod.attach(root)
        steps = handle.revert_promotion() if revert else handle.promote(force=force)
    except (drill_mod.UnsafeDrillRootError, drill_mod.DrillPreconditionError) as exc:
        raise click.ClickException(str(exc)) from exc
    for step in steps:
        click.echo(f"{step.action}: {step.detail}")
        click.echo(f"  revert: {step.revert}")


@drill_group.command("teardown")
@_drill_root_opt
def drill_teardown_cmd(root: str) -> None:
    """Delete the drill tree. Refuses anything this harness did not create."""
    from . import drill as drill_mod

    try:
        removed = drill_mod.attach(root).teardown()
    except drill_mod.UnsafeDrillRootError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"removed drill tree {removed}")


def register_fleet_commands(main: click.Group) -> None:
    """Register the fleet group on the skcapstone CLI."""
    main.add_command(fleet)


def main() -> None:
    """Console script entry point (skfleet)."""
    fleet()
