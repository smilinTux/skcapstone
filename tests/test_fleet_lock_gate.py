"""The fleet-lock CI gate's checks (scripts/ci/fleet_lock_gate.py)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fleet_lock_gate", ROOT / "scripts" / "ci" / "fleet_lock_gate.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def test_security_downgrade_fails_without_a_reason():
    base = "cryptography==50.0.2\nfastapi==0.141.0\nclick==8.4.2\n"
    head = "cryptography==50.0.1\nfastapi==0.141.0\nclick==8.1.8\n"
    findings = gate.security_downgrades(base, head, "")
    # click is not a security package: only cryptography is reported.
    assert len(findings) == 1 and findings[0].startswith("cryptography: 50.0.2 -> 50.0.1")


def test_security_downgrade_passes_with_a_reason_line():
    base, head = "PyJWT==2.15.1\n", "pyjwt==2.15.0\n"
    body = "Pin moves.\n\nsecurity-downgrade: PyJWT 2.15.1 breaks the HS256 verifier, see #12\n"
    assert gate.security_downgrades(base, head, body) == []
    # A bare line with no reason does not excuse it.
    assert gate.security_downgrades(base, head, "security-downgrade: pyjwt\n")


def test_security_upgrades_and_new_packages_pass():
    base = "urllib3==2.7.0\n"
    head = "urllib3==2.8.0\nparamiko==4.0.0\n"
    assert gate.security_downgrades(base, head, "") == []


def test_lock_versions_reads_every_marker_branch():
    text = (
        "# header\n"
        'numpy==2.5.2 ; python_version < "3.14"\n'
        'numpy==2.5.4 ; python_version >= "3.14"\n'
        "-c other.txt\n"
    )
    versions = gate.lock_versions(text)
    assert {str(v) for v in versions["numpy"]} == {"2.5.2", "2.5.4"}


def test_caps_table_rows_parse_and_prose_does_not():
    md = (ROOT / "deploy" / "dependency-caps.md").read_text(encoding="utf-8")
    rows = gate.listed_caps(md)
    assert ("skcapstone", "mcp", "<2.0") in rows
    assert ("sksecurity", "cryptography", "<51.0.0") in rows
    # The "Removed" section lists caps that are gone; it must never re-allow one.
    assert not any(dep == "rich" and pkg == "skcapstone" for pkg, dep, _ in rows)
    assert not any(pkg == "skbrain" for pkg, _, _ in rows)


def test_every_listed_cap_has_a_status_reason_and_date():
    md = (ROOT / "deploy" / "dependency-caps.md").read_text(encoding="utf-8")
    for line in md.splitlines():
        if gate._ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            assert len(cells) == 7, line
            assert cells[3] in ("proven", "pin", "dev-only", "open"), line
            assert cells[4] and cells[5], line
            assert cells[6].count("-") == 2, line


def test_upper_bound_extraction():
    from packaging.specifiers import SpecifierSet

    assert gate._upper(SpecifierSet(">=8.1,<9.0")) == "<9.0"
    assert gate._upper(SpecifierSet(">=0.110")) == ""
    assert gate._upper(SpecifierSet("==2.21.3")) == "==2.21.3"


def test_cli_exit_codes(tmp_path):
    base, head = tmp_path / "b.txt", tmp_path / "h.txt"
    base.write_text("requests==2.34.2\n")
    head.write_text("requests==2.32.5\n")
    script = ROOT / "scripts" / "ci" / "fleet_lock_gate.py"
    run = lambda *a, body="": subprocess.run(  # noqa: E731
        ["python3", str(script), *a], capture_output=True, text=True, env={"PR_BODY": body}
    )
    r = run("security-downgrades", str(base), str(head))
    assert r.returncode == 1 and "SECURITY_DOWNGRADE: requests" in r.stdout
    r = run("security-downgrades", str(base), str(head), body="security-downgrade: requests x\n")
    assert r.returncode == 0
    assert run("bogus").returncode == 2


@pytest.mark.parametrize("name", ["fleet-requirements.in", "fleet-tools.in", "dependency-caps.md"])
def test_lock_inputs_exist(name):
    assert (ROOT / "deploy" / name).is_file()


def test_workflow_runs_every_check():
    wf = (ROOT / ".github" / "workflows" / "fleet-lock.yml").read_text(encoding="utf-8")
    for needle in (
        "scripts/fleet-lock.sh --check",
        "-m pip check",
        "dependency_parity.py",
        "fleet_lock_gate.py \\\n            smoke",
        "caps deploy/dependency-caps.md",
        "security-downgrades",
        "pip-audit -r deploy/fleet-constraints.txt",
        '"3.12", "3.14"',
    ):
        assert needle in wf, needle
