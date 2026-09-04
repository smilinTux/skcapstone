"""Apply a rendered fleet Service object to the systemd user instance.

``fleet/materialize.py`` renders unit text and never touches the box. This
module is the other half: it compares the rendered bytes against what is on
disk, writes only what differs, and reloads systemd only when a write
actually happened.

Idempotence is the point. A second apply with an unchanged spec writes
nothing and returns ``[]``, which is what makes "install fresh" and
"reconverge an existing node" the same command.

Two ordering rules are load-bearing:

* Writes are atomic (tmp file in the same directory, then ``os.replace``).
  A half-written unit that systemd reads during ``daemon-reload`` is a live
  failure, not a cosmetic one.
* ``remove_unit`` disables BEFORE it unlinks. Unlink first and systemd keeps
  a dangling symlink in ``default.target.wants`` that survives the reload.
"""

from __future__ import annotations

import os
from pathlib import Path

from skcapstone.fleet.actuation import Runner, default_runner
from skcapstone.fleet.materialize import render_service_unit, render_timer_unit

__all__ = ["UnitApplyError", "apply_unit", "default_unit_dir", "remove_unit"]


class UnitApplyError(RuntimeError):
    """A systemctl step failed while applying or removing a unit."""


def default_unit_dir() -> Path:
    """Return the systemd user unit directory for the current user.

    Returns:
        ``~/.config/systemd/user``. Callers pass ``unit_dir`` explicitly to
        stay hermetic; this is only the fallback.
    """
    return Path.home() / ".config" / "systemd" / "user"


def _render_targets(name: str, spec: dict) -> dict[Path, str]:
    """Render every unit file the spec calls for, keyed by bare filename.

    Rendering happens for all targets before any write so a bad spec raises
    ``UnitRenderError`` with nothing half-applied on disk.
    """
    targets = {Path(f"{name}.service"): render_service_unit(name, spec)}
    schedule = spec.get("schedule")
    if schedule:
        targets[Path(f"{name}.timer")] = render_timer_unit(name, schedule)
    return targets


def _read_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory tmp file and rename."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _systemctl(args: list[str], runner: Runner) -> None:
    cmd = ["systemctl", "--user", *args]
    result = runner(cmd)
    if result.returncode != 0:
        detail = (getattr(result, "stderr", "") or "").strip()
        raise UnitApplyError(f"{' '.join(cmd)} failed ({result.returncode}): {detail}")


def apply_unit(
    name: str,
    spec: dict,
    *,
    unit_dir: Path | None = None,
    runner: Runner = default_runner,
    dry_run: bool = False,
) -> list[str]:
    """Materialize a fleet Service object as systemd user unit files.

    Args:
        name: Fleet object name; writes ``<name>.service`` and, when the spec
            has a ``schedule``, ``<name>.timer``.
        spec: Raw Service spec, passed through to the renderers.
        unit_dir: Directory to write into. Defaults to
            ``~/.config/systemd/user``; inject a ``tmp_path`` in tests.
        runner: Callable that executes a ``systemctl`` argv and returns an
            object with ``returncode``. Defaults to a real subprocess.
        dry_run: When true, return the paths that would change and touch
            neither disk nor ``runner``.

    Returns:
        Absolute paths written, as strings. Empty when every target already
        matches byte-for-byte, in which case systemd is not reloaded.

    Raises:
        UnitRenderError: the spec cannot be rendered (nothing is written).
        UnitApplyError: a systemctl step returned nonzero.
    """
    base = Path(unit_dir) if unit_dir is not None else default_unit_dir()
    targets = _render_targets(name, spec)
    changed = {
        base / rel: text for rel, text in targets.items() if _read_or_none(base / rel) != text
    }
    if dry_run or not changed:
        return [str(p) for p in changed]

    base.mkdir(parents=True, exist_ok=True)
    for path, text in changed.items():
        _write_atomic(path, text)

    _systemctl(["daemon-reload"], runner)
    if Path(f"{name}.timer") in targets:
        _systemctl(["enable", "--now", f"{name}.timer"], runner)
    else:
        _systemctl(["enable", f"{name}.service"], runner)
    return [str(p) for p in changed]


def remove_unit(
    name: str,
    *,
    unit_dir: Path | None = None,
    runner: Runner = default_runner,
) -> list[str]:
    """Disable and delete the unit files for a fleet Service object.

    Order matters: each unit is disabled (and stopped) while its file still
    exists, then unlinked, then systemd is reloaded once. Disabling after
    unlink leaves a dangling wants symlink behind.

    Args:
        name: Fleet object name; removes ``<name>.timer`` (if present) and
            ``<name>.service``.
        unit_dir: Directory to remove from. Defaults to
            ``~/.config/systemd/user``.
        runner: Callable that executes a ``systemctl`` argv.

    Returns:
        Absolute paths removed, as strings. Empty (and no reload) when
        neither file existed.

    Raises:
        UnitApplyError: a systemctl step returned nonzero.
    """
    base = Path(unit_dir) if unit_dir is not None else default_unit_dir()
    present = [p for p in (base / f"{name}.timer", base / f"{name}.service") if p.is_file()]
    if not present:
        return []
    for path in present:
        _systemctl(["disable", "--now", path.name], runner)
    for path in present:
        path.unlink()
    _systemctl(["daemon-reload"], runner)
    return [str(p) for p in present]
