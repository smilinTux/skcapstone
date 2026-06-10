"""Identity commands: migrate.

The ``skcapstone identity migrate`` command backfills every provisioned
agent's ``identity/identity.json`` with the explicit sovereign-identity
fields the unified layer expects (skcomms T2 / epic ``2b264064``):

  * ``realm`` + ``operator`` — mirrored from ``cluster.json``.
  * ``fqid`` — the three-tier ``<agent>@<operator>.<realm>`` label, sourced
    from :func:`capauth.resolve_agent_identity` (the canonical resolver).
  * ``pgp_fingerprint`` — the agent's 40-char PGP fingerprint, also from the
    resolver / the agent's CapAuth profile.

This command does **not** reimplement identity logic — it delegates to
``capauth.resolve_agent_identity`` for the per-agent identity and only mirrors
``realm``/``operator`` from cluster.json directly (those are cluster facts, not
agent facts). It is a *walker*: it finds every provisioned agent (one with a
CapAuth home, never a ``*-template``) and merges the missing fields into its
identity.json without clobbering unrelated keys.

Safety: these are LIVE identity files, so the command defaults to a dry-run
(it prints a plan and writes nothing). Pass ``--apply`` (alias ``--write``) to
actually modify files. The operation is idempotent — a second run on an
already-complete home reports every agent as unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import click

from ._common import SHARED_ROOT, console

# Fields the walker backfills, in stable display order.
_MANAGED_FIELDS = ("realm", "operator", "fqid", "pgp_fingerprint")

# cluster.json search path (mirrors capauth.agent_identity._CLUSTER_LOOKUP so
# realm/operator come from the same source the resolver uses for the fqid).
_CLUSTER_LOOKUP = [
    Path("/etc/skcapstone/cluster.json"),
]


@dataclass
class AgentPlan:
    """Planned identity.json changes for a single agent.

    Attributes:
        agent: Short agent name.
        path: Path to the agent's identity/identity.json.
        additions: Field → value mapping that would be written. Empty when
            the agent is already complete (nothing to add).
        applied: Whether the additions were actually written to disk.
        error: Non-empty when the agent could not be processed (e.g. an
            unreadable identity.json); such agents are skipped, not crashed.
    """

    agent: str
    path: Path
    additions: dict[str, str] = field(default_factory=dict)
    applied: bool = False
    error: str = ""

    @property
    def changed(self) -> bool:
        """True when this agent has at least one field to add."""
        return bool(self.additions)


@dataclass
class MigrationPlan:
    """Aggregate plan across every walked agent.

    Attributes:
        home: Shared root that was walked (``~/.skcapstone``).
        dry_run: True when nothing was written to disk.
        cluster_found: Whether a cluster.json was located (realm/operator are
            unavailable when False).
        agents: Per-agent plans (one per provisioned, non-template agent).
    """

    home: Path
    dry_run: bool
    cluster_found: bool
    agents: list[AgentPlan] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        """Number of agents with at least one field to add."""
        return sum(1 for a in self.agents if a.changed)

    @property
    def unchanged_count(self) -> int:
        """Number of already-complete agents (no additions, no error)."""
        return sum(1 for a in self.agents if not a.changed and not a.error)

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict."""
        return {
            "home": str(self.home),
            "dry_run": self.dry_run,
            "cluster_found": self.cluster_found,
            "changed": self.changed_count,
            "unchanged": self.unchanged_count,
            "agents": [
                {
                    "agent": a.agent,
                    "path": str(a.path),
                    "additions": a.additions,
                    "applied": a.applied,
                    "error": a.error,
                }
                for a in self.agents
            ],
        }


def _load_cluster(home: Path) -> Optional[dict]:
    """Load cluster.json from ``/etc/skcapstone`` then the agent home.

    Mirrors :data:`capauth.agent_identity._CLUSTER_LOOKUP` but resolves the
    home-local copy relative to *home* so a test (or alternate root) reads the
    fixture cluster.json rather than the real ``~/.skcapstone`` one.

    Args:
        home: Shared root directory being walked.

    Returns:
        The parsed cluster dict, or ``None`` when no cluster.json exists or it
        cannot be parsed.
    """
    for path in [*_CLUSTER_LOOKUP, home / "cluster.json"]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _provisioned_agents(home: Path) -> list[str]:
    """List agents with a CapAuth home (and thus a real identity).

    Reuses the "provisioned agent" notion from
    :func:`skcapstone.doctor._provisioned_agents`: an agent counts only when
    ``agents/<name>/capauth/`` exists, and ``*-template`` scaffolds are
    excluded.

    Args:
        home: Shared root directory (``~/.skcapstone``).

    Returns:
        Sorted provisioned agent names.
    """
    from ..doctor import _provisioned_agents as _doctor_provisioned

    return _doctor_provisioned(home)


def _plan_agent(home: Path, agent: str, cluster: Optional[dict]) -> AgentPlan:
    """Compute the identity.json additions for one agent.

    Reads the agent's current ``identity/identity.json`` and determines which
    of ``realm``/``operator``/``fqid``/``pgp_fingerprint`` are missing, using
    cluster.json (realm/operator) and ``capauth.resolve_agent_identity`` (fqid
    + fingerprint) as the source of truth. Existing values are never
    overwritten.

    Args:
        home: Shared root directory.
        agent: Short agent name.
        cluster: Parsed cluster.json dict, or ``None``.

    Returns:
        An :class:`AgentPlan` describing the additions (empty when complete or
        when no source value is available), or carrying an ``error`` when the
        identity.json is unreadable.
    """
    path = home / "agents" / agent / "identity" / "identity.json"
    plan = AgentPlan(agent=agent, path=path)

    existing: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError) as exc:
            plan.error = f"unreadable identity.json: {exc}"
            return plan

    # realm / operator come straight from cluster.json (cluster facts).
    desired: dict[str, Optional[str]] = {}
    if cluster is not None:
        desired["realm"] = cluster.get("realm")
        desired["operator"] = cluster.get("operator")

    # fqid + pgp_fingerprint come from the canonical resolver — never
    # reimplemented here (epic 2b264064; capauth is the source of truth).
    try:
        from capauth import resolve_agent_identity

        ident = resolve_agent_identity(agent)
        desired["fqid"] = getattr(ident, "fqid", None)
        desired["pgp_fingerprint"] = getattr(ident, "fingerprint", None)
    except Exception:  # noqa: BLE001 — resolver failure must not crash the walk
        pass

    for key in _MANAGED_FIELDS:
        value = desired.get(key)
        # Only add when we have a real value AND it is not already present.
        if value and not existing.get(key):
            plan.additions[key] = str(value)

    return plan


def migrate_identities(home: Path, *, apply: bool = False) -> MigrationPlan:
    """Walk provisioned agents and backfill their identity.json.

    For every provisioned agent (one with a CapAuth home, never a
    ``*-template``), ensure its ``identity/identity.json`` carries ``realm``,
    ``operator``, ``fqid`` and ``pgp_fingerprint``. Missing fields are merged
    in without clobbering unrelated keys; files are only written when something
    actually changed.

    Args:
        home: Shared root directory (``~/.skcapstone``).
        apply: When ``True``, write the changes to disk. When ``False`` (the
            default — these are live files), nothing is written and the
            returned plan is a preview only.

    Returns:
        A :class:`MigrationPlan` describing per-agent additions and whether
        each was applied.

    Examples:
        >>> plan = migrate_identities(Path("~/.skcapstone").expanduser())
        >>> plan.dry_run
        True
    """
    cluster = _load_cluster(home)
    plan = MigrationPlan(
        home=home,
        dry_run=not apply,
        cluster_found=cluster is not None,
    )

    for agent in _provisioned_agents(home):
        agent_plan = _plan_agent(home, agent, cluster)
        if apply and agent_plan.changed and not agent_plan.error:
            try:
                _apply_additions(agent_plan)
                agent_plan.applied = True
            except OSError as exc:
                agent_plan.error = f"write failed: {exc}"
        plan.agents.append(agent_plan)

    return plan


def _apply_additions(plan: AgentPlan) -> None:
    """Merge a plan's additions into its identity.json on disk.

    Reads the current file (or starts from ``{}`` if absent), updates only the
    planned keys, and writes the result back with stable indentation. Unrelated
    keys are preserved.

    Args:
        plan: The agent plan whose ``additions`` should be persisted.

    Raises:
        OSError: If the file cannot be read or written.
    """
    data: dict = {}
    if plan.path.exists():
        try:
            loaded = json.loads(plan.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    data.update(plan.additions)
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    plan.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def register_identity_commands(main: click.Group) -> None:
    """Register the ``identity`` command group on the main CLI."""

    @main.group()
    def identity():
        """Identity management — migrate per-agent identity.json files."""

    @identity.command("migrate")
    @click.option(
        "--home", default=SHARED_ROOT, type=click.Path(),
        help="Shared root directory (~/.skcapstone).",
    )
    @click.option(
        "--apply", "--write", "apply_", is_flag=True,
        help="Actually write changes. Default is a dry-run (writes nothing).",
    )
    @click.option(
        "--dry-run", is_flag=True,
        help="Explicitly preview only (the default). Overrides --apply if both given.",
    )
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def migrate(home: str, apply_: bool, dry_run: bool, json_out: bool) -> None:
        """Backfill realm/operator/fqid/pgp_fingerprint into agent identity.json.

        Walks every provisioned agent (one with a CapAuth home, excluding
        ``*-template`` dirs) under ``~/.skcapstone/agents/`` and ensures each
        agent's ``identity/identity.json`` carries the explicit sovereign
        fields. Delegates to ``capauth.resolve_agent_identity`` for the fqid
        and fingerprint; realm/operator are mirrored from cluster.json.

        SAFETY: defaults to a dry-run (prints a plan, writes nothing). Pass
        ``--apply`` (or ``--write``) to actually modify the live identity
        files. Idempotent — re-running on a complete home changes nothing.
        """
        home_path = Path(home).expanduser()
        do_apply = apply_ and not dry_run
        plan = migrate_identities(home_path, apply=do_apply)

        if json_out:
            click.echo(json.dumps(plan.to_dict(), indent=2))
            return

        _render_plan(plan)

    return None


def _render_plan(plan: MigrationPlan) -> None:
    """Render a migration plan as human-readable Rich output."""
    mode = "[yellow]DRY-RUN[/] (no files written — pass --apply to write)" \
        if plan.dry_run else "[green]APPLY[/] (files written)"
    console.print()
    console.print(f"  [bold]identity migrate[/]  {mode}")
    console.print(f"  [dim]{plan.home}[/]")
    if not plan.cluster_found:
        console.print(
            "  [yellow]~ cluster.json not found — realm/operator unavailable, "
            "fqid may be incomplete[/]"
        )
    console.print()

    if not plan.agents:
        console.print("  [dim]No provisioned agents found (none with a CapAuth home).[/]")
        console.print()
        return

    for a in plan.agents:
        if a.error:
            console.print(f"  [red]✗ {a.agent}[/]  {a.error}")
        elif not a.changed:
            console.print(f"  [green]✓ {a.agent}[/]  [dim]unchanged (already complete)[/]")
        else:
            verb = "added" if a.applied else "would add"
            fields = ", ".join(f"{k}={v}" for k, v in a.additions.items())
            color = "green" if a.applied else "cyan"
            console.print(f"  [{color}]→ {a.agent}[/]  {verb}: {fields}")
        console.print(f"     [dim]{a.path}[/]")

    console.print()
    summary = (
        f"  {plan.changed_count} to change, {plan.unchanged_count} unchanged"
        if plan.dry_run
        else f"  {plan.changed_count} changed, {plan.unchanged_count} unchanged"
    )
    console.print(f"[bold]{summary}[/]")
    if plan.dry_run and plan.changed_count:
        console.print("  [dim]Re-run with --apply to write these changes.[/]")
    console.print()
