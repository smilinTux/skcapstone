"""Executable entry point for the skcapstone CLI.

Runs the full ``skcapstone.cli:main`` command surface (the live ``cli/``
package). Used both by ``python -m skcapstone`` and as the PyInstaller CLI
entry point (see ``installer/build.py``).
"""

from __future__ import annotations

from skcapstone.cli import main

if __name__ == "__main__":
    main()
