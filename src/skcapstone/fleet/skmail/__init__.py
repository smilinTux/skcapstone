"""Installed entry point for the existing append-only SKMail script."""

import os
import sys
from importlib.metadata import version
from importlib.resources import as_file, files


def main() -> int:
    """Execute packaged mail with the installing interpreter and original argv."""
    if sys.argv[1:] == ["--version"]:
        print(f"skmail {version('skcapstone')}")
        return 0
    env = os.environ.copy()
    env["SKMAIL_PYTHON"] = sys.executable
    with as_file(files(__package__).joinpath("skmail")) as script:
        os.execve("/bin/bash", ["bash", str(script), *sys.argv[1:]], env)
