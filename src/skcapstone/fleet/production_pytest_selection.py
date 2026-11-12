"""Trusted pytest collection hook: exact baseline IDs, no prefix suppression."""

import json
import os
from pathlib import Path

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Keep pytest 9's passing subtests out of parent-only JUnit coverage."""
    from _pytest.junitxml import xml_key

    try:
        from _pytest.subtests import SubtestReport
    except ImportError:
        return  # pytest before built-in subtests retains its existing JUnit path.
    xml = config.stash.get(xml_key, None)
    if xml is not None:
        config.pluginmanager.register(_SubtestCounts(xml, SubtestReport))


class _SubtestCounts:
    """Normalize only actual passing subreports; failures/skips stay in JUnit."""

    def __init__(self, xml, report_type):
        self.xml = xml
        self.report_type = report_type
        self.passed = 0

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_logreport(self, report):
        before = self.xml.stats["passed"]
        result = yield
        if (
            isinstance(report, self.report_type)
            and report.when == "call"
            and report.passed
            and not hasattr(report, "wasxfail")
            and self.xml.stats["passed"] == before + 1
        ):
            # pytest counts these reports but emits only their parent testcase.
            self.xml.stats["passed"] -= 1
            self.passed += 1
        return result

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self):
        self.xml.add_global_property("skfleet_passing_subtests", self.passed)


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
