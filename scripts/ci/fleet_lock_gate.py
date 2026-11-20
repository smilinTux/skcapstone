#!/usr/bin/env python3
"""Checks the fleet-lock CI gate runs on a lock PR (.github/workflows/fleet-lock.yml).

Subcommands (each exits 0 on pass, 1 on a finding, 2 on bad input):

  security-downgrades BASE_LOCK HEAD_LOCK
      Fail when HEAD lowers the version of a security-relevant package
      compared with BASE (the PR's merge base on main), unless the PR body
      (env PR_BODY) has a line ``security-downgrade: <name> <why>``.

  caps CAPS_MD
      In the running venv, list every upper bound an SK package puts on a
      third-party library and fail on one that deploy/dependency-caps.md does
      not list. Rows there whose cap no installed SK package carries any more
      are reported as stale, not failed.

  audit-sees AUDIT_JSON CANARY
      Read a pip-audit ``-f json`` report of CANARY (deploy/audit-canary.txt,
      known-vulnerable pins) and fail unless every package in CANARY is
      reported with at least one vulnerability. Proves the audit can see.

  smoke REQUIREMENTS
      Import every top-level module of every SK package pinned in
      REQUIREMENTS (plus skcapstone) and run each of its console scripts with
      ``--help``. A failed import or a non-zero exit fails.

Standard library plus ``packaging`` only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

#: Chef, 2026-11-15: a lock change may not lower any of these without a reason.
SECURITY_PACKAGES = tuple(
    canonicalize_name(n)
    for n in (
        "cryptography",
        "pyOpenSSL",
        "PyJWT",
        "urllib3",
        "requests",
        "certifi",
        "paramiko",
        "Pillow",
        "starlette",
        "fastapi",
        "aiohttp",
        "h11",
        "jinja2",
        "PGPy",
        # 2026-11-15: CVE-2026-111497 (Brotli decoder out-of-bounds write).
        "Brotli",
        "brotlicffi",
    )
)

_REASON = re.compile(r"^\s*security-downgrade:\s*(\S+)\s+\S", re.I | re.M)


def lock_versions(text: str) -> dict[str, set[Version]]:
    """Every version a lock names per package (all markers, not just this env)."""
    out: dict[str, set[Version]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        for spec in req.specifier:
            if spec.operator == "==":
                try:
                    out.setdefault(canonicalize_name(req.name), set()).add(Version(spec.version))
                except InvalidVersion:
                    pass
    return out


def security_downgrades(base: str, head: str, pr_body: str) -> list[str]:
    """One line per security package whose lowest locked version went down."""
    excused = {canonicalize_name(m) for m in _REASON.findall(pr_body or "")}
    base_v, head_v = lock_versions(base), lock_versions(head)
    findings = []
    for name in SECURITY_PACKAGES:
        if name not in base_v or name not in head_v:
            continue
        before, after = min(base_v[name]), min(head_v[name])
        if after < before and name not in excused:
            findings.append(
                f"{name}: {before} -> {after} lowers a security package; say why in the PR "
                f"body with a line 'security-downgrade: {name} <reason>'"
            )
    return findings


def audit_blind_spots(report: dict, canary: str) -> list[str]:
    """Canary packages the pip-audit report shows no vulnerability for."""
    seen = {
        canonicalize_name(d.get("name", ""))
        for d in report.get("dependencies", [])
        if d.get("vulns")
    }
    return sorted(n for n in lock_versions(canary) if n not in seen)


def _sk_names(requirements: Path) -> set[str]:
    names = {"skcapstone"}
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(canonicalize_name(Requirement(line).name))
    return names


def _upper(spec_set) -> str:
    """The upper-bounding part of a specifier, normalised (``<2.0``, ``~=1.2``)."""
    parts = sorted(str(s) for s in spec_set if s.operator in ("<", "<=", "~=", "==", "==="))
    return ",".join(parts)


def installed_caps(sk_names: set[str]) -> set[tuple[str, str, str]]:
    """``(sk_package, dependency, upper)`` for every installed SK package's
    third-party requirement that carries an upper bound (base requirements and
    extras alike; extras are what the fleet installs with)."""
    caps = set()
    for dist in metadata.distributions():
        name = canonicalize_name(dist.metadata["Name"] or "")
        if name not in sk_names:
            continue
        for raw in dist.requires or ():
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            dep = canonicalize_name(req.name)
            if _is_sk(dep, sk_names):
                continue
            upper = _upper(req.specifier)
            if upper:
                caps.add((name, dep, upper))
    return caps


def _is_sk(dep: str, sk_names: set[str]) -> bool:
    """SK-internal coupling (capauth<0.4, skharness==...) is release ordering
    inside the estate, not a third-party cap, so it is not tracked here."""
    return (
        dep in sk_names or dep.startswith("sk") or dep in ("capauth", "cloud9", "cloud9-protocol")
    )


_ROW = re.compile(
    r"^\|\s*`?([A-Za-z0-9_.\-]+)`?\s*\|\s*`?([A-Za-z0-9_.\-]+)`?\s*\|\s*`([^`]+)`\s*\|"
)


def listed_caps(markdown: str) -> set[tuple[str, str, str]]:
    """Rows of the caps table: ``| package | dependency | `cap` | ...``."""
    rows = set()
    for line in markdown.splitlines():
        m = _ROW.match(line)
        if m and m.group(1).lower() not in ("package", "---"):
            spec = ",".join(sorted(p.strip() for p in m.group(3).split(",")))
            rows.add((canonicalize_name(m.group(1)), canonicalize_name(m.group(2)), spec))
    return rows


def check_caps(caps_md: Path, requirements: Path) -> int:
    installed = installed_caps(_sk_names(requirements))
    listed = listed_caps(caps_md.read_text(encoding="utf-8"))
    unlisted = sorted(installed - listed)
    for pkg, dep, cap in sorted(installed & listed):
        print(f"  listed  {pkg} -> {dep} {cap}")
    present = {canonicalize_name(d.metadata["Name"] or "") for d in metadata.distributions()}
    for pkg, dep, cap in sorted(listed - installed):
        why = (
            "the installed release does not carry it"
            if pkg in present
            else "package not in this lock"
        )
        print(f"  stale   {pkg} -> {dep} {cap} ({why})")
    for pkg, dep, cap in unlisted:
        print(
            f"UNLISTED_CAP: {pkg} caps {dep} at {cap}: remove the cap, or add a row to "
            f"{caps_md} with the reason and the test that proves it"
        )
    return 1 if unlisted else 0


#: Console scripts that are not CLIs, so ``--help`` is not a smoke for them.
#: Their entry point must still load. Name the reason with every entry.
NOT_A_CLI = {
    # A uvicorn server: it ignores --help, starts, and exits 3 on shutdown.
    "skchat-voice",
    # A stdio JSON backend for the harness: it parses stdin, not argv.
    "skharness-sk-backend",
}

#: MCP server modules imported as part of the smoke, so a lock that moves mcp
#: past what they support (mcp 2.x dropped Server.list_tools) fails here.
MCP_SERVERS = (
    "skcapstone.mcp_server",
    "skmemory.mcp_server",
    "sksecurity.mcp_server",
    "skchat.mcp_server",
    "skskills.aggregator",
)


def smoke(requirements: Path) -> int:
    failures = []
    for name in sorted(_sk_names(requirements)):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            failures.append(f"{name}: not installed")
            continue
        top = (dist.read_text("top_level.txt") or "").split()
        if not top:
            top = sorted(
                {
                    str(f).split("/", 1)[0].removesuffix(".py")
                    for f in dist.files or ()
                    if "/" in str(f) or str(f).endswith(".py")
                }
                - {"__pycache__"}
            )
            top = [t for t in top if t.isidentifier() and not t.endswith("-info")]
        for module in top:
            r = subprocess.run(
                [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
            )
            status = "ok" if r.returncode == 0 else "FAIL"
            print(f"  import {module:24} ({name}) {status}")
            if r.returncode:
                failures.append(f"{name}: import {module}: {r.stderr.strip().splitlines()[-1:]}")
        scripts = [ep for ep in dist.entry_points if ep.group == "console_scripts"]
        bindir = Path(sys.executable).parent
        for ep in scripts:
            if ep.name in NOT_A_CLI:
                try:
                    ep.load()
                    print(f"  {ep.name:30} entry point loads ({name}) ok")
                except Exception as exc:  # noqa: BLE001 - report any load failure
                    failures.append(f"{name}: {ep.name} entry point: {exc!r}")
                continue
            exe = bindir / ep.name
            env = {**os.environ, "HOME": os.environ.get("SMOKE_HOME", os.environ.get("HOME", ""))}
            try:
                # stdin closed: a stdio MCP server that ignores --help reads EOF and
                # exits instead of waiting for a client.
                r = subprocess.run(
                    [str(exe), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                    stdin=subprocess.DEVNULL,
                )
                code = r.returncode
                err = (r.stderr or r.stdout).strip().splitlines()[-1:]
            except subprocess.TimeoutExpired:
                code, err = "timeout", []
            print(f"  {ep.name:30} --help ({name}) {'ok' if code == 0 else f'FAIL {code}'}")
            if code != 0:
                failures.append(f"{name}: {ep.name} --help exited {code}: {err}")
    for module in MCP_SERVERS:
        r = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
        )
        print(f"  import {module:24} (mcp server) {'ok' if r.returncode == 0 else 'FAIL'}")
        if r.returncode:
            failures.append(f"import {module}: {r.stderr.strip().splitlines()[-1:]}")
    for f in failures:
        print(f"SMOKE_FAIL: {f}")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "security-downgrades" and len(args) == 2:
        base = Path(args[0]).read_text(encoding="utf-8") if Path(args[0]).exists() else ""
        head = Path(args[1]).read_text(encoding="utf-8")
        findings = security_downgrades(base, head, os.environ.get("PR_BODY", ""))
        for f in findings:
            print(f"SECURITY_DOWNGRADE: {f}")
        if not findings:
            print("no security package lowered")
        return 1 if findings else 0
    if cmd == "audit-sees" and len(args) == 2:
        import json

        try:
            report = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"AUDIT_BLIND: no readable pip-audit report ({exc})")
            return 1
        blind = audit_blind_spots(report, Path(args[1]).read_text(encoding="utf-8"))
        for name in blind:
            print(f"AUDIT_BLIND: pip-audit reported no vulnerability for canary {name}")
        if not blind:
            print("pip-audit sees every canary advisory")
        return 1 if blind else 0
    if cmd == "caps" and len(args) == 2:
        return check_caps(Path(args[0]), Path(args[1]))
    if cmd == "smoke" and len(args) == 1:
        return smoke(Path(args[0]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
