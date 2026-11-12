"""Native Node recipes with one sealed command and JUnit report per source file."""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

from . import production_test_plan as plan

SOURCE_FILES = ("package.json", "package-lock.json", "tests/_setup.mjs")


def is_native(profile: dict | None) -> bool:
    """Recognize a native recipe inside a qualified Node profile."""
    return isinstance(profile, dict) and "node_test" in profile.get("recipe", {})


def targets(recipe: dict) -> dict:
    """Validate explicit, bounded root test files and minimum case counts."""
    tests = recipe.get("node_test")
    if (
        set(recipe) != {"node_test"}
        or not isinstance(tests, dict)
        or not 1 <= len(tests) <= 512
        or any(type(n) is not int or not 1 <= n <= 100000 for n in tests.values())
        or any(
            not isinstance(p, str)
            or len(p) > 240
            or not re.fullmatch(r"tests/[A-Za-z0-9_-]+\.test\.mjs", p)
            for p in tests
        )
    ):
        raise plan.TestEvidenceError("invalid native Node per-file coverage")
    return dict(sorted(tests.items()))


def checks(recipe: dict) -> list[dict]:
    """Bind each source file to a separate report since Node JUnit omits filenames."""
    from .production_test_node import NODE

    return [
        {
            "id": f"node-{index:03d}",
            "argv": [
                str(NODE),
                "--test",
                "--import",
                "./tests/_setup.mjs",
                "--test-reporter=junit",
                f"--test-reporter-destination=/output/node-{index:03d}.xml",
                path,
            ],
        }
        for index, path in enumerate(targets(recipe))
    ]


def validate_source(recipe: dict, workspace: Path) -> None:
    """Reject missing targets, redirected parents and a mixed web workspace."""
    if (workspace / "apps/web").exists():
        raise plan.TestEvidenceError("native Node recipe cannot target apps/web")
    for relative in (*SOURCE_FILES, *targets(recipe)):
        path = workspace / relative
        if not path.is_file() or path.resolve() != workspace.resolve() / relative:
            raise plan.TestEvidenceError("native Node test input missing or redirected")


def report_names(profile: dict) -> tuple[str, ...]:
    """Return the report names in exact target order."""
    return tuple(check["id"] + ".xml" for check in checks(profile["recipe"]))


def junit_count(raw: bytes, minimum: int) -> int:
    """Count passing native test cases, including nested suites, failing closed."""
    if len(raw) > plan.MAX_OUTPUT or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise plan.TestEvidenceError("invalid native Node JUnit size or entities")
    try:
        raw.decode("utf-8")
        if b"\x00" in raw:
            raise ValueError("JUnit must use UTF-8")
        root = ET.fromstring(raw)
        if root.tag != "testsuites":
            raise ValueError("expected native Node testsuites")

        def count(element: ET.Element, depth: int = 0) -> int:
            """Count leaf cases; Node suite totals count immediate children."""
            if depth > 64:
                raise ValueError("JUnit nesting exceeds bound")
            total = 0
            for child in element:
                if child.tag == "testsuite":
                    subtotal = count(child, depth + 1)
                    total += subtotal
                elif child.tag == "testcase":
                    if (
                        not child.get("name")
                        or child.get("classname") != "test"
                        or len(child)
                        or any(key in child.attrib for key in ("failure", "error", "skipped"))
                    ):
                        raise ValueError("invalid, failed or skipped native testcase")
                    total += 1
                else:
                    raise ValueError("unexpected native JUnit element")
            for key in ("failures", "errors", "skipped", "disabled"):
                if int(element.get(key, "0")) != 0:
                    raise ValueError("native JUnit contains unsuccessful tests")
            if "tests" in element.attrib and int(element.attrib["tests"]) != len(element):
                raise ValueError("invalid native JUnit total")
            return total

        total = count(root)
        if total < minimum:
            raise ValueError("missing or undercounted native coverage")
        return total
    except (ValueError, TypeError, RecursionError, ET.ParseError) as exc:
        raise plan.TestEvidenceError("native Node JUnit coverage rejected: " + str(exc)) from exc


def evidence(raw: dict[str, bytes], profile: dict) -> tuple[str, dict]:
    """Recompute per-file counts using the sealed target-to-report mapping."""
    names = report_names(profile)
    if set(raw) != set(names):
        raise plan.TestEvidenceError("native Node reports missing or unexpected")
    counts = {
        path: junit_count(raw[name], minimum)
        for name, (path, minimum) in zip(names, targets(profile["recipe"]).items(), strict=True)
    }
    digest = plan.production_builder.digest({name: plan.sha(raw[name]) for name in names})
    return digest, {
        "total": sum(counts.values()),
        "per_file": counts,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
