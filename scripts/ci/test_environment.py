"""Refuse test installation into a production Python environment."""

import os
import pwd
import sys
from pathlib import Path


def check_environment():
    """Check interpreter and activation independently, including symlink aliases.

    Fleet runtimes use .skenv. Operators must register differently named runtime
    roots in SKCAPSTONE_PRODUCTION_VENVS (an os.pathsep-separated list).
    """
    roots = [Path(pwd.getpwuid(os.getuid()).pw_dir) / ".skenv", Path.home() / ".skenv"]
    for name in ("SKCAPSTONE_PRODUCTION_VENVS", "SKENV_HOME", "SKENV_PREFIX"):
        roots.extend(Path(p) for p in os.environ.get(name, "").split(os.pathsep) if p)
    roots = [p.expanduser().resolve() for p in roots]
    targets = [Path(sys.prefix), Path(sys.executable).parent.parent]
    if os.environ.get("VIRTUAL_ENV"):
        targets.append(Path(os.environ["VIRTUAL_ENV"]))
    for target in targets:
        target = target.expanduser().resolve()
        if ".skenv" in target.parts or any(target == p or p in target.parents for p in roots):
            raise RuntimeError(f"Refusing test installation in production environment: {target}")
        if (target / ".production").exists():
            raise RuntimeError(f"Refusing marked production environment: {target}")
    if os.environ.get("BASH_ENV"):
        raise RuntimeError("Refusing inherited BASH_ENV; launch with env -u BASH_ENV")
    os.environ.pop("BASH_ENV", None)
    os.environ.pop("ENV", None)


if __name__ == "__main__":
    try:
        check_environment()
    except RuntimeError as exc:
        sys.exit(str(exc))
