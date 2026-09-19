"""The claim-expiry report must be installed, not just written.

Three hand-maintained lists in this repo have each caused the same class of
failure: a mechanism that exists, is tested, and is reachable by nothing.
`script-files` omitted skmail for its whole life; `ALL_UNITS` and
`install.sh` each shipped a unit nothing installed. A CLI absent from
`[project.scripts]` is the same defect: `skfleet-claim-expiry` would not
exist on any host, so the rollout's report phase would have no tool.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def _pyproject() -> dict:
    root = Path(__file__).resolve().parents[2]
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_claim_expiry_cli_has_a_console_script() -> None:
    scripts = _pyproject()["project"].get("scripts", {})
    matching = {k: v for k, v in scripts.items() if "claim_expiry_cli" in v}
    assert matching, f"no console script points at claim_expiry_cli: {scripts}"


def test_the_console_script_target_is_importable_and_callable() -> None:
    """A packaged entry point naming a missing module installs fine and then
    fails at run time, which is the failure this test exists to prevent."""
    scripts = _pyproject()["project"].get("scripts", {})
    target = next(v for v in scripts.values() if "claim_expiry_cli" in v)
    module_path, _, attr = target.partition(":")
    import importlib

    module = importlib.import_module(module_path)
    assert callable(getattr(module, attr)), f"{target} is not callable"
