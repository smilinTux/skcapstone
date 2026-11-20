"""scripts/fleet/sync-cursor-auth.sh: an unusable source login is one clear
line and exit 2, never a traceback (2026-11-15)."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "sync-cursor-auth.sh"


def _jwt(exp: int) -> str:
    def enc(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


def _run(tmp_path: Path, content) -> subprocess.CompletedProcess:
    src = tmp_path / "auth.json"
    if content is not None:
        src.write_text(content if isinstance(content, str) else json.dumps(content))
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CURSOR_AUTH_SRC": str(src),
        "CURSOR_AUTH_VALIDATE_ONLY": "1",
    }
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_expired_token_is_one_line_and_exit_2(tmp_path):
    r = _run(tmp_path, {"accessToken": _jwt(1_762_275_300), "refreshToken": "r"})
    assert r.returncode == 2
    assert "Traceback" not in r.stderr and "AssertionError" not in r.stderr
    assert "accessToken expired at 2025-11-04" in r.stderr
    assert "cursor-agent login" in r.stderr
    assert r.stderr.count("\n") == 1


@pytest.mark.parametrize(
    "content, reason",
    [
        (None, "missing"),
        ("not json", "unreadable"),
        ({"accessToken": "opaque"}, "no JWT accessToken"),
        ({"accessToken": _jwt(int(time.time()) + 3600)}, "no refreshToken"),
    ],
)
def test_other_unusable_sources_exit_2(tmp_path, content, reason):
    r = _run(tmp_path, content)
    assert r.returncode == 2 and reason in r.stderr and "Traceback" not in r.stderr


def test_valid_token_passes_validation_without_printing_it(tmp_path):
    tok = _jwt(int(time.time()) + 3600)
    r = _run(tmp_path, {"accessToken": tok, "refreshToken": "secret-refresh"})
    assert r.returncode == 0, r.stderr
    assert tok not in r.stdout + r.stderr and "secret-refresh" not in r.stdout + r.stderr


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
