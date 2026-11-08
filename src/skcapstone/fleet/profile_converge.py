"""Converge this node's install profile right after a rollout deploy.

A rollout used to stop at "code and unit files are on disk": nothing then
installed a newly required binary, ran its config and post-install steps, or
enabled a newly required timer. Every profile addition (worktrunk's `wt` and
`skfleet-wt-gc.timer`, 2026-11-08) needed a hand pass on every builder after
its code had already rolled.

This step closes that gap with the same machinery an operator would use:

1. `installer.run_install(mode="apply", enable=True, start=True)`: installs
   missing required items and enables required timers. It stays gated by the
   fleet freeze and this node's own actuation opt-in, so a frozen fleet or a
   report-only node is skipped, never actuated.
2. `binaries.finish()` for every required binary already at its pin: the
   config and post-install steps that `install --apply` never reaches for a
   binary that is not missing (the same thing `skfleet binary-setup` does).

The step is deliberately non-fatal: it prints what it did and exits 0 even
when an item fails. The rollout's drift gate, which runs next, stays the one
authority on whether the node is fit, so a broken item is reported there
instead of this step halting the whole ring on its own.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import binaries, install_backends, installer, node_controller
from .paths import default_paths, self_node_name


def _role(paths, node: str) -> str | None:
    views = {view.name: view for view in node_controller.node_views(paths)}
    view = views.get(node)
    return view.role if view is not None and view.role else None


def converge(
    *,
    paths=None,
    node: str | None = None,
    run_install: Callable = installer.run_install,
    finish: Callable = binaries.finish,
    probe: Callable = binaries.probe,
) -> list[str]:
    """Apply this node's profile; return one human-readable line per outcome.

    Never raises for an expected refusal (no role, profile not applied,
    frozen, not opted in): those are skips, reported as such.
    """
    paths = paths or default_paths()
    node = node or self_node_name()
    role = _role(paths, node)
    if role is None:
        return [f"{node}: no spec.role bound, skipped"]
    try:
        summary = run_install(
            paths,
            role,
            node=node,
            mode="apply",
            dry_run=False,
            enable=True,
            start=True,
            only=None,
            backends=install_backends.default_backends(),
        )
    except installer.ProfileNotApplied:
        return [f"{node}: profile {role!r} not applied in the store, skipped"]
    except installer.Frozen:
        return [f"{node}: fleet is frozen, skipped"]
    except installer.ActuationNotAllowed:
        return [f"{node}: actuation not enabled for this node, skipped"]

    lines = [
        f"{r['status']} {r['kind']} {r['name']}: {r['detail']}".rstrip(": ")
        for r in summary.get("results", [])
    ]
    if not lines:
        lines.append(f"{node}: profile {role!r} already converged")

    required = (installer._profile_spec(paths, role).get("binaries") or {}).get("required") or []
    observed = probe(list(required)) if required else {}
    for name in required:
        entry = binaries.CATALOG.get(name)
        if entry is None or observed.get(name) != entry.version:
            continue
        try:
            note = finish(entry)
        except Exception as exc:  # noqa: BLE001 - finish() already never raises; belt and braces
            note = f"post-install failed: {exc}"
        lines.append(f"binary {name}: {note or 'config converged'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print each outcome; always exit 0 (see module docstring)."""
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    try:
        lines = converge()
    except Exception as exc:  # noqa: BLE001 - the drift gate is the authority
        lines = [f"profile_converge error (gate decides): {type(exc).__name__}: {exc}"]
    for line in lines:
        print(f"profile_converge: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
