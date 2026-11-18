"""`skcapstone fleet clients`: pinned pi/opencode/codex/claude across the ring.

Design: docs/fleet/CLIENTS.md. The timer entry point is `tick`; `upstream`
runs daily and opens pin-bump PRs.
"""

from __future__ import annotations

import json as jsonlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import click

from . import clients as spec_mod
from . import clients_ring as ring


def _repo(path: str | None) -> Path:
    return Path(
        os.path.expanduser(path or os.environ.get("SKFLEET_CLIENTS_REPO") or ring.DEFAULT_REPO)
    )


def _load(repo: Path) -> spec_mod.Spec:
    try:
        return spec_mod.load_spec(repo)
    except spec_mod.SpecError as exc:
        raise click.ClickException(str(exc)) from None


def _rows(spec: spec_mod.Spec) -> list[dict]:
    try:
        return spec_mod.fetch_catalog(spec.gateway)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"gateway catalog unavailable: {type(exc).__name__}: {exc}"
        ) from None


repo_opt = click.option(
    "--config-repo",
    default=None,
    help="skgateway-config-chi checkout (default ~/deploy/skgateway-config-chi).",
)
host_opt = click.option("--host", "hosts", multiple=True, help="Limit to these ring hosts.")


@click.group(name="clients")
def clients() -> None:
    """Pinned coding clients (pi, opencode, codex, claude) on every ring host."""


@clients.command("render")
@repo_opt
@click.option("--host", default=None, help="Ring host (default: canary).")
def render_cmd(config_repo, host) -> None:
    """Print one host's desired state as JSON (secrets stay as placeholders)."""
    spec = _load(_repo(config_repo))
    click.echo(
        jsonlib.dumps(spec_mod.render_host(spec, host or spec.canary, _rows(spec)), indent=2)
    )


def _inspect_all(spec, rows, hosts):
    for host in [h for h in spec.ring if not hosts or h in hosts]:
        desired = spec_mod.render_host(spec, host, rows)
        yield (
            host,
            desired,
            (
                ring.call_agent(host, "inspect", desired, timeout=180)
                if ring.reachable(host)
                else {"error": "unreachable"}
            ),
        )


@clients.command("drift")
@repo_opt
@host_opt
@click.option("--json", "as_json", is_flag=True)
def drift_cmd(config_repo, hosts, as_json) -> None:
    """Host x client table of versions, paths and config drift. Exit 1 on drift."""
    spec = _load(_repo(config_repo))
    rows = _rows(spec)
    report, drifted = {}, False
    for host, desired, view in _inspect_all(spec, rows, hosts):
        report[host] = view
        if view.get("error"):
            drifted = True
            continue
        for c in desired["clients"]:
            row = view["clients"][c["name"]]
            first = (row.get("first") or "").replace(str(Path.home()), "~")
            row["ok"] = bool(
                row["shim_ok"]
                and row["version"] == c["version"]
                and first == f"{desired['bin_dir']}/{c['name']}"
            )
            drifted |= not row["ok"]
        for f in view["files"]:
            f["ok"] = not f.get("changed") and not f.get("missing_secrets")
            drifted |= not f["ok"] and f.get("skipped") is not True
    if as_json:
        click.echo(jsonlib.dumps(report, indent=2))
    else:
        names = list(spec.clients)
        click.echo("host     " + "".join(f"{n:<26}" for n in names) + "configs")
        for host, view in report.items():
            if view.get("error"):
                click.echo(f"{host:<9}ERROR {view['error']}")
                continue
            cells = []
            for n in names:
                row = view["clients"].get(n)
                if not row:
                    cells.append(f"{'(skipped)':<26}")
                    continue
                where = (row.get("first") or "none").replace(str(Path.home()), "~")
                mark = "" if row.get("ok") else "*"
                cells.append(f"{mark}{row.get('version') or 'none'} {where}"[:25].ljust(26))
            cfg = [f"{Path(f['file']).name}:{_cfg_mark(f)}" for f in view["files"]]
            click.echo(f"{host:<9}" + "".join(cells) + " ".join(cfg))
        click.echo("* = not the pinned version via the ~/.local/bin shim")
    sys.exit(1 if drifted else 0)


def _cfg_mark(f: dict) -> str:
    if f["ok"]:
        return "ok"
    return "nokey" if f.get("missing_secrets") else "drift"


@clients.command("plan")
@repo_opt
@host_opt
def plan_cmd(config_repo, hosts) -> None:
    """What `apply` would change on each host (read-only)."""
    spec = _load(_repo(config_repo))
    rows = _rows(spec)
    st = ring.RingState()
    for host, desired, view in _inspect_all(spec, rows, hosts):
        if view.get("error"):
            click.echo(f"{host}: ERROR {view['error']}")
            continue
        same = st.host(host).get("hash") == spec_mod.state_hash(desired)
        click.echo(f"{host}: {'up to date' if same else 'pending'}")
        for c in desired["clients"]:
            row = view["clients"][c["name"]]
            if not row["shim_ok"]:
                click.echo(
                    f"  {c['name']}: {row.get('version') or 'none'} at {row.get('first')} -> "
                    f"{c['version']} via {desired['bin_dir']}/{c['name']}"
                )
        for f in view["files"]:
            if f.get("missing_secrets"):
                click.echo(
                    f"  {f['file']}: skip, no {sorted(set(f['missing_secrets']))} key on host"
                )
            elif f.get("changed"):
                click.echo(
                    f"  {f['file']}: {'create' if f.get('created') else 'update'} {f['changed']}"
                )


def _run_tick(config_repo, hosts, force, sync) -> ring.Outcome:
    repo = _repo(config_repo)
    with ring.Lock(ring.lock_path()) as got:
        if not got:
            ring.log("another skfleet-clients run holds the lock; skipping")
            return ring.Outcome("noop")
        if sync:
            why = ring.sync_repo(repo)
            if why:
                ring.log(f"config repo: {why}")
                if "ancestor" in why:
                    return ring.Outcome("refused", reason=why)
                return ring.Outcome("noop")
        spec = _load(repo)
        try:
            rows = spec_mod.fetch_catalog(spec.gateway)
        except Exception as exc:  # noqa: BLE001 - gateway down: keep what hosts have
            ring.log(
                f"gateway catalog unavailable ({type(exc).__name__}); keeping current configs"
            )
            return ring.Outcome("noop")
        return ring.tick(spec, rows, hosts=list(hosts) or None, force=force)


@clients.command("tick")
@repo_opt
def tick_cmd(config_repo) -> None:
    """Timer entry: sync the config repo, then roll anything pending."""
    out = _run_tick(config_repo, (), False, True)
    sys.exit(
        1
        if out.status in ("halted", "refused") and out.reason != "waiting to retry halted state"
        else 0
    )


@clients.command("apply")
@repo_opt
@host_opt
@click.option("--force", is_flag=True, help="Retry a halted state now; accept a large model drop.")
def apply_cmd(config_repo, hosts, force) -> None:
    """Roll pending hosts now (canary first, halt at first failure)."""
    out = _run_tick(config_repo, hosts, force, False)
    click.echo(jsonlib.dumps(out.__dict__))
    sys.exit(1 if out.status in ("halted", "refused") else 0)


@clients.command("smoke")
@repo_opt
@click.option("--host", default=None)
def smoke_cmd(config_repo, host) -> None:
    """One real prompt per client through the installed shims."""
    spec = _load(_repo(config_repo))
    desired = spec_mod.render_host(spec, host or spec.canary, [])
    res = ring.call_agent(host or spec.canary, "smoke", desired)
    click.echo(jsonlib.dumps(res, indent=2))
    sys.exit(0 if res.get("ok") else 1)


@clients.command("rollback")
@click.option("--host", required=True)
def rollback_cmd(host) -> None:
    """Undo the last apply on one host (shims and config files)."""
    click.echo(jsonlib.dumps(ring.call_agent(host, "rollback", {})))


def _git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and res.returncode:
        raise click.ClickException(f"git {' '.join(args)}: {res.stderr.strip()[-300:]}")
    return res.stdout.strip()


@clients.command("upstream")
@repo_opt
@click.option("--dry-run", is_flag=True, help="Report newer versions; open nothing.")
def upstream_cmd(config_repo, dry_run) -> None:
    """Daily: for each newer upstream client, smoke it on the canary and open a pin PR."""
    repo = _repo(config_repo)
    spec = _load(repo)
    for pin in spec.clients.values():
        try:
            latest, sha = ring.latest_version(pin)
        except Exception as exc:  # noqa: BLE001
            ring.log(f"{pin.name}: upstream check failed: {exc}")
            continue
        if ring._ver(latest) <= ring._ver(pin.version):
            continue
        branch = f"clients-bump-{pin.name}-{latest}"
        ring.log(f"{pin.name}: {pin.version} -> {latest} available")
        if dry_run:
            continue
        if _git(repo, "ls-remote", "--heads", "origin", branch):
            ring.log(f"{pin.name}: {branch} already pushed; leaving the open PR alone")
            continue
        _open_bump(repo, spec, pin, latest, sha, branch)


def _open_bump(repo: Path, spec: spec_mod.Spec, pin, latest: str, sha: str, branch: str) -> None:
    from dataclasses import replace

    candidate = replace(pin, version=latest, sha256=sha or pin.sha256)
    payload = {
        "install_root": spec.install_root,
        "bin_dir": spec.bin_dir,
        "candidate": True,
        "clients": [candidate.as_install()],
    }
    smoke = ring.call_agent(spec.canary, "smoke", payload)
    ok = bool(smoke.get("ok"))
    major = ring._ver(latest)[:1] != ring._ver(pin.version)[:1]
    hold = pin.hold or major or not ok
    with tempfile.TemporaryDirectory(prefix="skfleet-clients-bump-") as tmp:
        wt = Path(tmp) / "wt"
        _git(repo, "fetch", "-q", "origin", "main")
        _git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "origin/main")
        try:
            pins = wt / "clients" / "pins.yaml"
            pins.write_text(ring.bump_pins_text(pins.read_text(), pin.name, latest, sha))
            touched = ["clients/pins.yaml"]
            if pin.gateway_env.get("file") and pin.gateway_env.get("var"):
                gw = wt / pin.gateway_env["file"]
                gw.write_text(
                    ring.bump_gateway_env(gw.read_text(), pin.gateway_env["var"], latest)
                )
                touched.append(pin.gateway_env["file"])
            _git(wt, "add", *touched)
            _git(
                wt,
                "commit",
                "-q",
                "-m",
                f"clients: {pin.name} {pin.version} -> {latest}\n\n"
                f"Opened by skfleet-clients upstream check. Canary smoke on {spec.canary}: "
                f"{'passed' if ok else 'FAILED'}.",
            )
            _git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        finally:
            _git(repo, "worktree", "remove", "--force", str(wt), check=False)
            _git(repo, "branch", "-D", branch, check=False)
    why = []
    if pin.hold:
        why.append(f"{pin.name} bumps are held for a human merge")
    if major:
        why.append("major version")
    if not ok:
        why.append("canary smoke FAILED")
    body = (
        f"{pin.name} {pin.version} -> {latest}.\n\nCanary smoke on {spec.canary} "
        f"(candidate binary, pin not moved): {'passed' if ok else 'FAILED'}\n\n```\n"
        f"{jsonlib.dumps(smoke.get('results', smoke), indent=1)[:2000]}\n```\n\n"
        + ("Held: " + "; ".join(why) + ".\n" if hold else "Auto-merge armed.\n")
        + "\nAfter merge, skfleet-clients rolls it host by host from chiap08"
        " and halts at the first failed gate.\n"
    )
    res = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            _gh_repo(repo),
            "--head",
            branch,
            "--base",
            "main",
            "--title",
            f"clients: {pin.name} {latest}",
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
    )
    url = res.stdout.strip().splitlines()[-1] if res.returncode == 0 and res.stdout.strip() else ""
    ring.log(f"{pin.name}: PR {url or 'create failed: ' + res.stderr.strip()[-200:]}")
    if url and not hold:
        arm = subprocess.run(
            ["gh", "pr", "merge", "--auto", "--rebase", url], capture_output=True, text=True
        )
        if arm.returncode:
            hold, why = True, why + [f"auto-merge not permitted ({arm.stderr.strip()[-120:]})"]
    subject = f"skfleet-clients: {pin.name} {latest} {'needs a merge' if hold else 'auto-merging'}"
    ring.notify(
        spec.notify, subject, f"{url or branch}. " + ("; ".join(why) or "canary smoke passed")
    )


def _gh_repo(repo: Path) -> str:
    url = _git(repo, "remote", "get-url", "origin")
    tail = url.split("github.com", 1)[-1].lstrip(":/")
    return tail[:-4] if tail.endswith(".git") else tail
