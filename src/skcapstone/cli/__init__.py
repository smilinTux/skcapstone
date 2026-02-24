"""
SKCapstone CLI — the sovereign agent command line.

This package organizes the CLI into modular command groups.
Each group lives in its own module for maintainability.
The main Click group is defined here and all subcommands
are registered via imports.

Entry point: skcapstone.cli:main
"""

from __future__ import annotations

# Re-export everything from the monolithic cli module during migration.
# Once all groups are extracted, this file becomes the thin router.
from .._cli_monolith import *  # noqa: F401,F403
from .._cli_monolith import main  # explicit for entry point
