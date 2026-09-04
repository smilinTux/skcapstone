from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "pr-freshness.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pr_freshness", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scan_reports_fixture_counts_and_recommendation(monkeypatch):
    m = load_module()
    fixture = [
        {
            "number": 1,
            "title": "current",
            "headRefOid": "aaa",
            "baseRefName": "main",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 2,
            "title": "stale despite blocked state",
            "headRefOid": "bbb",
            "baseRefName": "main",
            "mergeStateStatus": "BLOCKED",
        },
    ]
    comparisons = {
        "aaa": {"ahead_by": 3, "behind_by": 0},
        "bbb": {"ahead_by": 2, "behind_by": 7},
    }
    monkeypatch.setattr(m, "fetch_open_prs", lambda repo: fixture)
    monkeypatch.setattr(m, "fetch_comparison", lambda repo, oid: comparisons[oid])

    findings, errors = m.scan(["example/demo"])

    assert errors == []
    assert findings[0]["mergeStateStatus"] == "CLEAN"
    assert (findings[0]["ahead"], findings[0]["behind"]) == (3, 0)
    assert findings[0]["refresh"] == "CURRENT"
    assert findings[0]["recommendation"] == "none"
    assert findings[1]["mergeStateStatus"] == "BLOCKED"
    assert (findings[1]["ahead"], findings[1]["behind"]) == (2, 7)
    assert findings[1]["refresh"] == "REFRESH_NEEDED"
    assert findings[1]["recommendation"] == m.REFRESH_RECOMMENDATION


def test_merge_state_behind_requires_refresh_even_when_count_is_zero(monkeypatch):
    m = load_module()
    monkeypatch.setattr(
        m,
        "fetch_open_prs",
        lambda repo: [
            {
                "number": 9,
                "title": "race",
                "headRefOid": "ccc",
                "baseRefName": "main",
                "mergeStateStatus": "BEHIND",
            }
        ],
    )
    monkeypatch.setattr(m, "fetch_comparison", lambda repo, oid: {"ahead_by": 1, "behind_by": 0})
    findings, errors = m.scan(["example/demo"])
    assert errors == []
    assert findings[0]["refresh"] == "REFRESH_NEEDED"


def test_scan_collects_attributable_failures(monkeypatch):
    m = load_module()

    def fail(repo):
        raise RuntimeError(f"gh pr list for {repo} failed: no auth")

    monkeypatch.setattr(m, "fetch_open_prs", fail)
    findings, errors = m.scan(["example/demo"])
    assert findings == []
    assert errors == ["gh pr list for example/demo failed: no auth"]


def test_json_cli_over_fixture_gh(tmp_path):
    fixture = [
        {
            "number": 42,
            "title": "fixture stale PR",
            "headRefOid": "deadbeef",
            "baseRefName": "main",
            "mergeStateStatus": "BEHIND",
        }
    ]
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"prs = {fixture!r}\n"
        "if sys.argv[1:3] == ['pr', 'list']:\n"
        "    print(json.dumps(prs))\n"
        "elif sys.argv[1] == 'api':\n"
        "    print(json.dumps({'ahead_by': 4, 'behind_by': 6}))\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "example/demo", "--json"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["errors"] == []
    assert report["findings"][0]["ahead"] == 4
    assert report["findings"][0]["behind"] == 6
    assert report["findings"][0]["mergeStateStatus"] == "BEHIND"
    assert "merge current main" in report["findings"][0]["recommendation"]


def test_json_cli_current_fixture_exits_zero(tmp_path):
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:3] == ['pr', 'list']:\n"
        "    print(json.dumps([{'number': 1, 'title': 'ok', "
        "'headRefOid': 'abc', 'baseRefName': 'main', "
        "'mergeStateStatus': 'CLEAN'}]))\n"
        "elif sys.argv[1] == 'api':\n"
        "    print(json.dumps({'ahead_by': 1, 'behind_by': 0}))\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "example/demo", "--json"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    assert result.returncode == 0
