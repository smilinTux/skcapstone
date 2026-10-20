"""Profile default: third-party tool telemetry is off on every SK node.

Skills installed through skskills can phone home. HeyGen's HyperFrames is the
first: its CLI reads a persisted preference, while its media-use scripts only
honour an environment variable. Both are set here so an agent launched from a
shell, a systemd user unit, or a desktop session all start opted out.

    $XDG_CONFIG_HOME/environment.d/80-sk-telemetry-optout.conf
        HYPERFRAMES_NO_TELEMETRY=1
    ~/.hyperframes/config.json
        "telemetryEnabled": false   (only when the key is absent; other keys kept,
        our addition marked with _skcapstone_telemetry_optout)

Undo: ``python -m skcapstone.telemetry_optout undo``. An explicit
``telemetryEnabled: true`` is respected and nothing is written; symlinks are
never written through.

``environment.d`` is host-local on purpose: it is read by the systemd user
manager and desktop sessions, and never synced. Interactive shells get the same
variable from the SKCapstone profile block in ~/.bashrc.

Add a tool by extending ``OPTOUT_ENV`` (and, if it keeps a persisted switch, a
writer like ``_hyperframes_config``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .estate import environment_d_dir

ENV_FILENAME = "80-sk-telemetry-optout.conf"
OPTOUT_ENV: dict[str, str] = {
    "HYPERFRAMES_NO_TELEMETRY": "1",
}


def _env_file_body() -> str:
    lines = [
        "# SKCapstone profile default: third-party telemetry off.",
        "# Written by `skcapstone register`; edit OPTOUT_ENV in",
        "# skcapstone/telemetry_optout.py rather than this file.",
    ]
    lines += [f"{k}={v}" for k, v in OPTOUT_ENV.items()]
    return "\n".join(lines) + "\n"


MARKER_KEY = "_skcapstone_telemetry_optout"


def _unsafe(path: Path) -> bool:
    """True when the path, or its parent directory, is a symlink (never write through one)."""
    return path.is_symlink() or path.parent.is_symlink()


def _read_config(path: Path) -> Optional[dict]:
    """Return the parsed config, {} when absent, or None when unreadable or not an object."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def ensure_telemetry_optout_report(
    env_dir: Optional[Path] = None,
    home: Optional[Path] = None,
) -> dict:
    """Write the opt-out defaults and report what changed and what was refused.

    * An explicit ``telemetryEnabled: true`` in ``~/.hyperframes/config.json`` is a
      stated opt-in and is respected: nothing is written (not even the env file,
      which would override it).
    * Symlinks are never written through (file or parent dir): the path is skipped.
    * Keys we set in config.json are marked with ``MARKER_KEY`` so ``undo`` only
      removes what we added.
    """
    env_dir = env_dir or environment_d_dir()
    home = home or Path.home()
    changed: list[str] = []
    skipped: list[dict] = []

    cfg_path = home / ".hyperframes" / "config.json"
    config: Optional[dict] = None
    if _unsafe(cfg_path):
        skipped.append({"path": str(cfg_path), "reason": "symlink"})
    else:
        config = _read_config(cfg_path)
        if config is None:
            skipped.append({"path": str(cfg_path), "reason": "unparseable"})
        elif config.get("telemetryEnabled") is True:
            skipped.append({"path": "all", "reason": "explicit telemetryEnabled=true respected"})
            return {"changed": changed, "skipped": skipped}

    env_file = env_dir / ENV_FILENAME
    body = _env_file_body()
    if _unsafe(env_file):
        skipped.append({"path": str(env_file), "reason": "symlink"})
    elif not env_file.exists() or env_file.read_text(encoding="utf-8") != body:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(body, encoding="utf-8")
        changed.append(str(env_file))

    if config is not None and "telemetryEnabled" not in config:
        config["telemetryEnabled"] = False
        config[MARKER_KEY] = True
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        changed.append(str(cfg_path))
    return {"changed": changed, "skipped": skipped}


def ensure_telemetry_optout(
    env_dir: Optional[Path] = None,
    home: Optional[Path] = None,
) -> list[str]:
    """Write the opt-out defaults. Idempotent. Returns the paths changed."""
    return ensure_telemetry_optout_report(env_dir=env_dir, home=home)["changed"]


def undo_telemetry_optout(
    env_dir: Optional[Path] = None,
    home: Optional[Path] = None,
) -> list[str]:
    """Remove exactly what ``ensure_telemetry_optout`` wrote. Returns the paths changed.

    The env file is deleted only if it still carries our header; config.json keys
    are removed only when our marker is present.
    """
    env_dir = env_dir or environment_d_dir()
    home = home or Path.home()
    changed: list[str] = []
    env_file = env_dir / ENV_FILENAME
    if not _unsafe(env_file) and env_file.is_file():
        if env_file.read_text(encoding="utf-8").startswith("# SKCapstone profile default"):
            env_file.unlink()
            changed.append(str(env_file))
    cfg_path = home / ".hyperframes" / "config.json"
    config = None if _unsafe(cfg_path) else _read_config(cfg_path)
    if config and config.get(MARKER_KEY) is True:
        config.pop("telemetryEnabled", None)
        config.pop(MARKER_KEY, None)
        cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        changed.append(str(cfg_path))
    return changed


def bashrc_exports() -> str:
    """The export lines for the SKCapstone ~/.bashrc profile block."""
    return "".join(f"export {k}={v}\n" for k, v in OPTOUT_ENV.items())


if __name__ == "__main__":  # pragma: no cover - `python -m skcapstone.telemetry_optout undo`
    import sys

    if sys.argv[1:] == ["undo"]:
        print("\n".join(undo_telemetry_optout()) or "nothing to undo")
    else:
        print("usage: python -m skcapstone.telemetry_optout undo", file=sys.stderr)
        raise SystemExit(2)
