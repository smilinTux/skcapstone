"""Profile default: third-party tool telemetry is off on every SK node.

Skills installed through skskills can phone home. HeyGen's HyperFrames is the
first: its CLI reads a persisted preference, while its media-use scripts only
honour an environment variable. Both are set here so an agent launched from a
shell, a systemd user unit, or a desktop session all start opted out.

    $XDG_CONFIG_HOME/environment.d/80-sk-telemetry-optout.conf
        HYPERFRAMES_NO_TELEMETRY=1
    ~/.hyperframes/config.json
        "telemetryEnabled": false   (other keys preserved)

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


def _hyperframes_config(home: Path) -> Optional[str]:
    path = home / ".hyperframes" / "config.json"
    config: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if not isinstance(loaded, dict):
            return None  # never clobber a file we cannot parse
        config = loaded
    if config.get("telemetryEnabled") is False:
        return None
    config["telemetryEnabled"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return str(path)


def ensure_telemetry_optout(
    env_dir: Optional[Path] = None,
    home: Optional[Path] = None,
) -> list[str]:
    """Write the opt-out defaults. Idempotent. Returns the paths changed."""
    env_dir = env_dir or environment_d_dir()
    home = home or Path.home()
    changed: list[str] = []

    env_file = env_dir / ENV_FILENAME
    body = _env_file_body()
    if not env_file.exists() or env_file.read_text(encoding="utf-8") != body:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(body, encoding="utf-8")
        changed.append(str(env_file))

    written = _hyperframes_config(home)
    if written:
        changed.append(written)
    return changed


def bashrc_exports() -> str:
    """The export lines for the SKCapstone ~/.bashrc profile block."""
    return "".join(f"export {k}={v}\n" for k, v in OPTOUT_ENV.items())
