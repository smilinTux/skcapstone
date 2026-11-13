"""scripts/lib/guarded-pip.sh: a constraint conflict aborts install.sh, it never falls back.

install.sh used to discard pip's stderr and retry without extras on any failure.
With constraints/security-floors.txt in force that silently turned a conflict
(for example a floor of cryptography>=50 against a package that still caps
cryptography<50) into a node that lost the skcomms extras or skipped sksecurity
and still reported success. These tests pin the new behaviour, including against
a real pip resolving real (locally built) wheels.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib" / "guarded-pip.sh"
INSTALL = ROOT / "scripts" / "install.sh"


def _run(snippet: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": "/tmp"}
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nsource "{LIB}"\n{snippet}'],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _fake_pip(tmp_path: Path, exit_code: int, stderr: str) -> Path:
    p = tmp_path / "fakepip"
    p.write_text(f"#!/bin/bash\ncat >&2 <<'EOF'\n{stderr}\nEOF\nexit {exit_code}\n")
    p.chmod(0o755)
    return p


def test_success_returns_zero(tmp_path):
    pip = _fake_pip(tmp_path, 0, "")
    r = _run('guarded_pip_install foo -q\necho reached', {"PIP": str(pip)})
    assert r.returncode == 0 and "reached" in r.stdout


def test_conflict_aborts_the_whole_script_with_a_clear_message(tmp_path):
    pip = _fake_pip(
        tmp_path,
        1,
        "ERROR: Cannot install skcomms[crypto] because these package versions have "
        "conflicting dependencies.\nERROR: ResolutionImpossible",
    )
    r = _run(
        'guarded_pip_install -e "/x[crypto]" -q || echo FALLBACK-RAN\necho reached-after',
        {"PIP": str(pip), "PIP_CONSTRAINT": "/x/floors.txt"},
    )
    assert r.returncode == 3
    assert "FALLBACK-RAN" not in r.stdout and "reached-after" not in r.stdout
    assert "dependency or constraint conflict" in r.stderr
    assert "/x/floors.txt" in r.stderr
    assert "ResolutionImpossible" in r.stderr  # pip's own output is kept, not discarded
    assert "SK_SKIP_SECURITY_FLOORS=1" in r.stderr


def test_ordinary_failure_returns_one_and_shows_pip_output(tmp_path):
    pip = _fake_pip(tmp_path, 1, "ERROR: No matching distribution found for nosuchpkg")
    r = _run('guarded_pip_install nosuchpkg -q || echo FALLBACK-RAN', {"PIP": str(pip)})
    assert r.returncode == 0
    assert "FALLBACK-RAN" in r.stdout
    assert "No matching distribution found" in r.stderr


def test_install_sh_has_no_silenced_pip_and_sources_the_guard():
    text = INSTALL.read_text()
    assert 'source "$SCRIPT_DIR/lib/guarded-pip.sh"' in text
    for line in text.splitlines():
        if "pip" in line.lower() and "install" in line and "2>/dev/null" in line:
            pytest.fail(f"pip install still discards stderr: {line.strip()}")
    assert "SK_SKIP_SECURITY_FLOORS" in text


def _wheel(directory: Path, name: str, version: str, requires: list[str]) -> None:
    dist = f"{name}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    meta += "".join(f"Requires-Dist: {r}\n" for r in requires)
    wheel = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files = {f"{dist}/METADATA": meta, f"{dist}/WHEEL": wheel, f"{name}.py": "x = 1\n"}
    record = []
    for fn, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data.encode()).digest()).rstrip(b"=")
        record.append(f"{fn},sha256={digest.decode()},{len(data.encode())}")
    record.append(f"{dist}/RECORD,,")
    files[f"{dist}/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as z:
        for fn, data in files.items():
            z.writestr(fn, data)


def test_real_pip_constraint_conflict_aborts(tmp_path):
    """A floor above a package's declared cap, resolved by real pip, must abort."""
    pytest.importorskip("pip")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "skfakecap", "1.0", ["skfakedep<2"])  # still caps the dep
    _wheel(wheels, "skfakedep", "1.5", [])
    _wheel(wheels, "skfakedep", "3.0", [])  # the "fixed" release the floor demands
    floors = tmp_path / "floors.txt"
    floors.write_text("skfakedep>=3.0\n")
    pip = tmp_path / "pipwrap"
    pip.write_text(
        f"#!/bin/bash\nexec {sys.executable} -m pip \"$@\" --no-index --find-links {wheels} "
        f"--target {tmp_path / 'site'} --disable-pip-version-check\n"
    )
    pip.chmod(0o755)
    r = _run(
        'guarded_pip_install skfakecap -q || echo FALLBACK-RAN\necho reached-after',
        {"PIP": str(pip), "PIP_CONSTRAINT": str(floors), "HOME": str(tmp_path)},
    )
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "FALLBACK-RAN" not in r.stdout
    assert "dependency or constraint conflict" in r.stderr
    # and without the floor the same install succeeds, so the abort is the floor's doing
    ok = _run(
        'guarded_pip_install skfakecap -q\necho installed',
        {"PIP": str(pip), "HOME": str(tmp_path)},
    )
    assert ok.returncode == 0 and "installed" in ok.stdout, ok.stderr
