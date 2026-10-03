"""Qualified Node/Vitest inputs for the existing native test sandbox."""

from __future__ import annotations

import json
import os
import platform
import re
import stat
from pathlib import Path
from xml.etree import ElementTree

from . import production_test_plan as plan

SCHEMA = "skfleet.qualified-node-test-profile/v1"
NODE = Path("/usr/bin/node")
# Node-local, outside Syncthing, which can normalize read-only directory modes.
ARTIFACT_ROOT = Path.home() / ".local/share/skcapstone/test-dependencies"
SOURCE_FILES = (
    "package.json",
    "package-lock.json",
    "apps/web/package.json",
    "apps/web/vite.config.ts",
    "apps/web/tsconfig.json",
    "apps/web/eslint.config.js",
)


def is_node(profile: dict | None) -> bool:
    """Identify the explicit variant, leaving Python profiles unchanged."""
    return isinstance(profile, dict) and profile.get("schema") == SCHEMA


def checks(recipe: dict) -> list[dict]:
    """Compile only full-suite Vitest, TypeScript build and ESLint commands."""
    if not isinstance(recipe, dict) or set(recipe) != {"vitest"}:
        raise plan.TestEvidenceError("unsupported Node recipe")
    tests = recipe["vitest"]
    if (
        not isinstance(tests, dict)
        or not 1 <= len(tests) <= 256
        or any(type(n) is not int or not 1 <= n <= 100000 for n in tests.values())
        or any(
            not isinstance(p, str)
            or len(p) > 240
            or not re.fullmatch(r"src/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.test\.tsx?", p)
            for p in tests
        )
    ):
        raise plan.TestEvidenceError("invalid Node per-file coverage")
    base = "/work/node_modules/"
    return [
        {
            "id": "vitest",
            "argv": [
                str(NODE),
                base + "vitest/vitest.mjs",
                "run",
                "--configLoader",
                "runner",
                "--no-cache",
                "--maxWorkers",
                "1",
                "--reporter=junit",
                "--outputFile=/output/vitest.xml",
            ],
        },
        {"id": "typecheck", "argv": [str(NODE), base + "typescript/bin/tsc", "-b"]},
        {"id": "lint", "argv": [str(NODE), base + "eslint/bin/eslint.js", "."]},
    ]


def artifact_digest(path: Path) -> str:
    """Hash bounded immutable owned content, allowing only internal relative links."""
    rows, total = [], 0
    if path.is_symlink() or not path.is_dir():
        raise plan.TestEvidenceError("Node artifact is not a real directory")
    for item in [path, *sorted(path.rglob("*"))]:
        info = item.lstat()
        name = str(item.relative_to(path))
        if info.st_uid != os.getuid():
            raise plan.TestEvidenceError("Node artifact owner mismatch")
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(item)
            try:
                resolved = item.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise plan.TestEvidenceError("Node artifact link is broken or cyclic") from exc
            if Path(target).is_absolute() or not resolved.is_relative_to(path.resolve()):
                raise plan.TestEvidenceError("Node artifact link escapes")
            rows.append((name, "link", target))
            continue
        if info.st_mode & 0o222:
            raise plan.TestEvidenceError("Node artifact is writable")
        if stat.S_ISDIR(info.st_mode):
            rows.append((name, "dir", info.st_mode & 0o777))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            total += info.st_size
            if total > 1024**3 or len(rows) > 100000:
                raise plan.TestEvidenceError("Node artifact exceeds bound")
            rows.append((name, info.st_mode & 0o777, plan.sha(item.read_bytes())))
        else:
            raise plan.TestEvidenceError("Node artifact contains special files")
    return plan.sha(json.dumps(rows, separators=(",", ":")).encode())


def artifact_path(environment: dict) -> Path:
    """Resolve a content identity under the controller root, never a supplied path."""
    identity = environment.get("artifact_sha256")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise plan.TestEvidenceError("invalid Node artifact identity")
    plan.private_dir(ARTIFACT_ROOT)
    return ARTIFACT_ROOT / identity


def validate_environment(environment: dict, workspace: Path | None = None) -> None:
    """Recheck the pinned runtime/artifact and optional exact candidate configuration."""
    try:
        _validate_environment(environment, workspace)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise plan.TestEvidenceError(str(exc)) from exc


def _validate_environment(environment: dict, workspace: Path | None) -> None:
    """Validate one environment; normalize invalid inputs at the public boundary."""
    if (
        not isinstance(environment, dict)
        or set(environment) != {"node_sha256", "platform", "artifact_sha256", "source_sha256"}
        or environment["platform"] != [platform.system(), platform.machine()]
        or environment["node_sha256"] != plan.sha(NODE.read_bytes())
        or not isinstance(environment["source_sha256"], dict)
        or set(environment["source_sha256"]) != set(SOURCE_FILES)
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
            for v in environment["source_sha256"].values()
        )
    ):
        raise plan.TestEvidenceError("qualified Node environment changed")
    artifact = artifact_path(environment)
    if artifact_digest(artifact) != environment["artifact_sha256"]:
        raise plan.TestEvidenceError("qualified Node dependencies changed")
    for relative in ("node_modules", "apps/web/node_modules"):
        if (artifact / relative).is_symlink() or not (artifact / relative).is_dir():
            raise plan.TestEvidenceError("Node dependency mount is missing or redirected")
    if workspace is None:
        return
    for relative, expected in environment["source_sha256"].items():
        path = workspace / relative
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(workspace.resolve())
            or (plan.sha(path.read_bytes()) != expected)
        ):
            raise plan.TestEvidenceError("Node package, lock or configuration changed")
    scripts = json.loads((workspace / "apps/web/package.json").read_bytes())["scripts"]
    if (
        not isinstance(scripts, dict)
        or any(
            scripts.get(k) != v
            for k, v in {"test": "vitest run", "typecheck": "tsc -b", "lint": "eslint ."}.items()
        )
        or any(
            k in scripts
            for k in (
                "pretest",
                "posttest",
                "pretypecheck",
                "posttypecheck",
                "prelint",
                "postlint",
            )
        )
    ):
        raise plan.TestEvidenceError("Node package scripts do not match qualified commands")


def junit_counts(raw: bytes, profile: dict) -> dict:
    """Require exact flat Vitest file suites, identities and positive per-file counts."""
    if len(raw) > plan.MAX_OUTPUT or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise plan.TestEvidenceError("invalid Node JUnit size or entities")
    try:
        raw.decode("utf-8")
        if b"\x00" in raw:
            raise ValueError("JUnit must use UTF-8")
        root = ElementTree.fromstring(raw)
        minima = profile["recipe"]["vitest"]
        counts, identities = {}, set()
        if root.tag != "testsuites" or any(s.tag != "testsuite" for s in root):
            raise ValueError("expected flat Vitest suites")
        for suite in root:
            name = suite.get("name", "")
            if name.startswith("/work/apps/web/"):
                name = name[len("/work/apps/web/") :]
            if name not in minima or name in counts:
                raise ValueError("unexpected or duplicate file")
            cases = list(suite.findall("testcase"))
            if (
                any(c.tag not in {"testcase", "system-out", "system-err"} for c in suite)
                or any(int(suite.get(k, "-1")) != 0 for k in ("failures", "errors", "skipped"))
                or int(suite.get("tests", "-1")) != len(cases)
            ):
                raise ValueError("invalid suite totals or failure")
            for case in cases:
                classname = case.get("classname", "").removeprefix("/work/apps/web/")
                identity = (name, case.get("name"))
                if (
                    classname != name
                    or not identity[1]
                    or identity in identities
                    or any(c.tag not in {"system-out", "system-err"} for c in case)
                ):
                    raise ValueError("invalid, duplicate, failed or skipped testcase")
                identities.add(identity)
            counts[name] = len(cases)
        if set(counts) != set(minima) or any(counts[n] < v for n, v in minima.items()):
            raise ValueError("missing or undercounted coverage")
        totals = {"tests": len(identities), "failures": 0, "errors": 0, "skipped": 0}
        for key, expected in totals.items():
            if key in root.attrib and int(root.attrib[key]) != expected:
                raise ValueError("invalid root totals")
    except (ValueError, KeyError, TypeError, ElementTree.ParseError) as exc:
        raise plan.TestEvidenceError("Node JUnit coverage rejected: " + str(exc)) from exc
    return {"total": len(identities), "per_file": counts, "failures": 0, "errors": 0, "skipped": 0}
