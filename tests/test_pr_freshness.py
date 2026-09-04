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


def test_classify_pr_table():
    m = load_module()
    assert m.classify_pr({"mergeStateStatus": "BEHIND"}) == "REFRESH_NEEDED"
    assert m.classify_pr({"mergeStateStatus": "DIRTY"}) == "REFRESH_NEEDED"
    assert m.classify_pr({"mergeStateStatus": "CLEAN"}) == "CLEAN"
    assert m.classify_pr({"mergeStateStatus": "HAS_HOOKS"}) == "CLEAN"
    assert m.classify_pr({"mergeStateStatus": "BLOCKED"}) == "NEEDS_ATTENTION"
    assert m.classify_pr({}) == "NEEDS_ATTENTION"
    assert m.classify_pr({"mergeStateStatus": None}) == "NEEDS_ATTENTION"


def test_scan_classifies_and_counts(monkeypatch):
    m = load_module()
    fixture = [
        {
            "number": 1,
            "title": "fine",
            "headRefOid": "a",
            "baseRefName": "main",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 2,
            "title": "stale",
            "headRefOid": "b",
            "baseRefName": "main",
            "mergeStateStatus": "BEHIND",
        },
        {
            "number": 3,
            "title": "review held",
            "headRefOid": "c",
            "baseRefName": "main",
            "mergeStateStatus": "BLOCKED",
        },
    ]
    monkeypatch.setattr(m, "fetch_open_prs", lambda repo: fixture)
    findings, errors = m.scan(["example/demo"])
    assert errors == []
    verdicts = [f["verdict"] for f in findings]
    assert verdicts == ["CLEAN", "REFRESH_NEEDED", "NEEDS_ATTENTION"]
    assert all(f["repo"] == "example/demo" for f in findings)


def test_scan_collects_gh_errors(monkeypatch):
    m = load_module()

    def boom(repo):
        raise RuntimeError("gh pr list failed for example/demo: no auth")

    monkeypatch.setattr(m, "fetch_open_prs", boom)
    findings, errors = m.scan(["example/demo"])
    assert findings == []
    assert len(errors) == 1 and "no auth" in errors[0]


def test_exit_code_requires_refresh(monkeypatch, capsys):
    m = load_module()
    stale = [
        {
            "number": 9,
            "title": "x",
            "headRefOid": "d",
            "baseRefName": "main",
            "mergeStateStatus": "DIRTY",
        }
    ]
    monkeypatch.setattr(m, "fetch_open_prs", lambda repo: stale)
    assert m.main(["--repo", "example/demo"]) == 1
    assert "REFRESH_NEEDED" in capsys.readouterr().out

    clean = [
        {
            "number": 8,
            "title": "y",
            "headRefOid": "e",
            "baseRefName": "main",
            "mergeStateStatus": "CLEAN",
        }
    ]
    monkeypatch.setattr(m, "fetch_open_prs", lambda repo: clean)
    assert m.main(["--repo", "example/demo", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][0]["verdict"] == "CLEAN"


def test_end_to_end_with_fake_gh(tmp_path):
    gh = tmp_path / "gh"
    payload = [
        {
            "number": 7,
            "title": "behind pr",
            "headRefOid": "f",
            "baseRefName": "main",
            "mergeStateStatus": "BEHIND",
        }
    ]
    gh.write_text("#!/bin/sh\necho '%s'\n" % json.dumps(payload))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "example/demo"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 1
    assert "example/demo#7" in r.stdout
