"""``skcapstone deploy``: run released main, never a working tree.

Commands:
    skcapstone deploy plan       what a rollout would change and restart
    skcapstone deploy apply      pre-flight, build, verify, switch, restart, smoke
    skcapstone deploy rollback   back to the previous release
    skcapstone deploy status     the release this node runs
    skcapstone deploy drift      editable installs, exec paths in git trees, version drift
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import click

from ._common import console


def _paths():
    from ..deploy.rollout import Paths

    return Paths.default()


def _manifest(path, ref, fetch=True):
    from ..deploy import ManifestUnavailableError, resolve_manifest

    try:
        return resolve_manifest(_paths().state_dir, path=path, ref=ref, fetch=fetch)
    except (ManifestUnavailableError, OSError, ValueError) as exc:
        console.print(f"[red]manifest unavailable:[/] {exc}")
        sys.exit(2)


def register_deploy_commands(main: click.Group) -> None:
    """Register the ``deploy`` group on the main CLI."""

    @main.group("deploy")
    def deploy() -> None:
        """Deploy released builds of main from the release manifest."""

    manifest_opt = click.option(
        "--manifest",
        "manifest_path",
        default=None,
        help="Manifest file (overrides the manifest repo).",
    )
    ref_opt = click.option(
        "--ref",
        default=None,
        help="Earlier commit of the manifest repo's main (rollback by manifest).",
    )
    node_opt = click.option(
        "--node", default=None, help="Node profile to use (default: this hostname)."
    )

    @deploy.command("plan")
    @manifest_opt
    @ref_opt
    @node_opt
    @click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
    def plan_cmd(manifest_path, ref, node, as_json) -> None:
        """Show what a rollout would change and restart."""
        from ..deploy.rollout import make_plan

        manifest, mid = _manifest(manifest_path, ref)
        plan = make_plan(manifest, _paths().venv_link / "bin" / "python", node)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "manifest": mid,
                        "changed": plan.changed,
                        "editable": plan.editable,
                        "restarts": plan.restarts,
                    },
                    indent=2,
                )
            )
            return
        console.print(f"manifest {mid[:12]}")
        if plan.empty:
            console.print("[green]nothing to do: this node runs the manifest[/]")
            return
        for name, (have, want) in sorted(plan.changed.items()):
            tag = " (editable)" if name in plan.editable else ""
            console.print(f"  {name}: {have or '-'}{tag} -> {want}")
        console.print(f"restarts: {', '.join(plan.restarts) or 'none'}")

    @deploy.command("apply")
    @manifest_opt
    @ref_opt
    @node_opt
    @click.option(
        "--python",
        "python",
        default=None,
        help="Interpreter for the new venv (default: the live venv's version).",
    )
    @click.option("--no-restart", is_flag=True, help="Build, verify and switch only.")
    @click.option(
        "--allow-missing-bin", multiple=True, help="bin/ entry the new venv may drop on purpose."
    )
    def apply_cmd(manifest_path, ref, node, python, no_restart, allow_missing_bin) -> None:
        """Roll this node onto the manifest (rolls back on a failed smoke)."""
        from ..deploy.rollout import DeployError, apply

        if shutil.which("uv") is None:
            console.print("[red]uv is required[/] (it builds the release venv)")
            sys.exit(2)
        manifest, mid = _manifest(manifest_path, ref)
        try:
            res = apply(
                manifest,
                mid,
                _paths(),
                python or manifest.python,
                node=node,
                restart=not no_restart,
                allow_missing_bin=tuple(allow_missing_bin),
                log=lambda m: console.print(f"[dim]{m}[/]"),
            )
        except DeployError as exc:
            console.print(f"[red]{exc}[/]")
            sys.exit(1)
        for p in res.problems:
            console.print(f"  [red]x[/] {p}")
        if res.ok:
            console.print(f"[green]deployed[/] {res.release} (previous: {res.previous})")
        elif res.rolled_back:
            console.print(
                f"[yellow]rolled back[/] to {res.previous}; failed release kept at {res.release}"
            )
            sys.exit(1)
        else:
            console.print(
                f"[red]not switched[/]: the new venv failed verification ({res.release})"
            )
            sys.exit(1)

    @deploy.command("tools")
    @manifest_opt
    @ref_opt
    @node_opt
    @click.option("--no-restart", is_flag=True, help="Export and rewrite only.")
    def tools_cmd(manifest_path, ref, node, no_restart) -> None:
        """Roll only the manifest's tool exports (no venv rebuild)."""
        from ..deploy.rollout import apply_tools_only

        manifest, _mid = _manifest(manifest_path, ref)
        res = apply_tools_only(
            manifest,
            _paths(),
            node=node,
            restart=not no_restart,
            log=lambda m: console.print(f"[dim]{m}[/]"),
        )
        for p in res.problems:
            console.print(f"  [red]x[/] {p}")
        if res.ok:
            console.print(
                f"[green]tools deployed[/]; restarted: {', '.join(res.restarted) or '-'}"
            )
            return
        console.print("[red]tools not deployed[/]" + (" (restored)" if res.rolled_back else ""))
        sys.exit(1)

    @deploy.command("rollback")
    @manifest_opt
    @node_opt
    def rollback_cmd(manifest_path, node) -> None:
        """Switch back to the previous release and restart its services."""
        from ..deploy.rollout import DeployError, rollback

        manifest, _mid = _manifest(manifest_path, None, fetch=False)
        try:
            res = rollback(_paths(), list(manifest.services_for(node)))
        except DeployError as exc:
            console.print(f"[red]{exc}[/]")
            sys.exit(1)
        for p in res.problems:
            console.print(f"  [red]x[/] {p}")
        console.print(f"now on {res.release} (was {res.previous})")
        sys.exit(0 if res.ok else 1)

    @deploy.command("status")
    def status_cmd() -> None:
        """The release this node runs."""
        from ..deploy.rollout import load_state

        paths = _paths()
        link = paths.venv_link
        target = link.resolve() if link.exists() else None
        kind = (
            "symlink (managed)"
            if link.is_symlink()
            else ("directory (unmanaged)" if link.exists() else "missing")
        )
        console.print(f"{link}: {kind} -> {target}")
        click.echo(json.dumps(load_state(paths), indent=2))

    @deploy.command("drift")
    @manifest_opt
    @node_opt
    @click.option("--alert", is_flag=True, help="Send one sk-alert when the findings changed.")
    @click.option("--offline", is_flag=True, help="Do not fetch the manifest repo.")
    @click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
    def drift_cmd(manifest_path, node, alert, offline, as_json) -> None:
        """Report what makes this node something other than released main."""
        from ..deploy import ManifestUnavailableError, resolve_manifest
        from ..deploy.drift import alert_on_change, compute

        paths = _paths()
        try:
            manifest, _ = resolve_manifest(paths.state_dir, path=manifest_path, fetch=not offline)
        except (ManifestUnavailableError, OSError, ValueError):
            manifest = None
        report = compute(Path.home(), paths.venv_link / "bin" / "python", manifest, node=node)
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "findings": report.lines(),
                        "unknown": report.unknown,
                        "dormant": report.dormant,
                        "digest": report.digest(),
                    },
                    indent=2,
                )
            )
        else:
            for line in report.lines():
                console.print(f"  {line}")
            for line in report.unknown:
                console.print(f"  [yellow]unknown:[/] {line}")
            if report.dormant:
                console.print(
                    f"  [dim]{len(report.dormant)} line(s) from units nothing can start "
                    "(not findings; --json lists them)[/]"
                )
            console.print(f"{len(report.lines())} finding(s)")
        if alert:
            outcome = alert_on_change(report, paths.state_dir / "drift-alert.json")
            console.print(f"alert: {outcome}")
        sys.exit(1 if report.lines() else 0)
