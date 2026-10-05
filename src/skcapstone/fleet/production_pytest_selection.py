"""Trusted pytest collection hook: exact baseline IDs, no prefix suppression."""

import json
import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Accept only the profile's exact operator-approved baseline node IDs."""
    parser.addoption("--skfleet-baseline-node", action="append", default=[])


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Record collection and remove precisely the named existing baseline items."""
    baseline = sorted(config.getoption("--skfleet-baseline-node"))
    known = {item.nodeid for item in items}
    if set(baseline) - known:
        raise pytest.UsageError("baseline node IDs missing from collected sealed source")
    removed = [item for item in items if item.nodeid in baseline]
    items[:] = [item for item in items if item.nodeid not in baseline]
    if removed:
        config.hook.pytest_deselected(items=removed)
    value = {
        "schema": "skfleet.pytest-selection/v1",
        "baseline": baseline,
        "deselected": sorted(item.nodeid for item in removed),
        "selected": sorted(item.nodeid for item in items),
    }
    path = Path(os.environ.get("SKFLEET_SELECTION_OUTPUT", "/output/selection.json"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
