"""Exclude a native Node file only from a sealed baseline assertion failure."""

import json
from pathlib import Path
from xml.etree import ElementTree as ET

from . import production_node_test_recipe as native
from . import production_test_plan as plan


def failed_report(directory: Path, checks: list[dict], recipe: dict) -> bytes:
    """Bind the failed file's raw JUnit to the exact executed command prefix."""
    expected = native.checks(recipe)
    if (
        not checks
        or len(checks) > len(expected)
        or any(
            actual.get("id") != command["id"]
            or actual.get("argv") != command["argv"]
            or actual.get("exit_code") != (1 if index == len(checks) - 1 else 0)
            for index, (actual, command) in enumerate(zip(checks, expected))
        )
    ):
        raise plan.TestEvidenceError("native Node baseline needs a failed command prefix")
    failed = checks[-1]
    if failed.get("output_sha256") != plan.sha(
        plan.read_private(directory / (failed["id"] + ".log"))
    ):
        raise plan.TestEvidenceError("native Node baseline log changed")
    raw = plan.read_private(directory / (failed["id"] + ".xml"))
    path = list(native.targets(recipe))[len(checks) - 1]
    return json.dumps({"target": path, "junit": raw.decode("utf-8")}).encode()


def baseline_recipe(raw: bytes, workspace: Path, recipe: dict) -> str:
    """Remove the failing file, keeping all other obligations for the next run."""
    native.validate_source(recipe, workspace)
    report = json.loads(raw)
    xml = report["junit"].encode()
    if len(xml) > plan.MAX_OUTPUT or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml or b"\x00" in xml:
        raise plan.TestEvidenceError("invalid native baseline report")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise plan.TestEvidenceError("invalid native baseline XML") from exc
    failures = root.findall(".//failure")
    # Node reports uncaught imports, timeouts and process exits as test failures
    # too. Only an actual assertion failure establishes a product baseline.
    if (
        root.tag != "testsuites"
        or not failures
        or any("ERR_ASSERTION" not in (failure.text or "") for failure in failures)
    ):
        raise plan.TestEvidenceError("native Node failure is not a proven assertion baseline")
    tests = native.targets(recipe)
    if report["target"] not in tests:
        raise plan.TestEvidenceError("native baseline target outside recipe")
    del tests[report["target"]]
    result = {"node_test": tests}
    native.validate_source(result, workspace)
    return json.dumps(result, sort_keys=True, separators=(",", ":"))
