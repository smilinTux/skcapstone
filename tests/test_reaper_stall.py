"""Focused check for the reaper stall blind spot. Card 7b656e7b.

Liveness used to mean only that a tmux session exists. A launch wrapper whose
child never starts sits in do_wait forever, keeps its session, and holds the
claim while every reaper tick correctly reports it live. Observed 2026-08-31 on
b27301c0: elapsed 17:24:15, CPU time 00:00:00, log 0 bytes, claim held 17.4h.

The predicate is lifted verbatim out of the shipped script, so this tests the
source that runs rather than a paraphrase of it.

Run: python3 tests/test_reaper_stall.py
"""

import ast
import glob
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


def _load():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_never_started"
    )
    grace = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "STALL_GRACE"
    )
    home = tempfile.mkdtemp()
    ns = {"glob": glob, "os": os, "time": time, "HOME": home}
    exec(compile(ast.Module(body=[grace, fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["_never_started"], home


def main():
    never_started, home = _load()
    logs = os.path.join(home, ".skcapstone/fleet/logs")
    os.makedirs(logs)

    def mk(cid, size, age):
        p = os.path.join(logs, "%s-20260831T000000Z.log" % cid)
        open(p, "wb").write(b"x" * size)
        os.utime(p, (time.time() - age, time.time() - age))

    mk("aaaa0001", 0, 3600)
    mk("aaaa0002", 1, 3600)
    mk("aaaa0003", 0, 60)

    cases = [
        ("aaaa0001", True, "zero bytes and older than grace: never started, exclude"),
        ("aaaa0002", False, "one byte written: alive, keep regardless of age"),
        ("aaaa0003", False, "zero bytes but inside grace: still starting, keep"),
        ("aaaa0404", False, "no log at all: absence is not evidence of death, keep"),
    ]
    failed = 0
    for cid, want, why in cases:
        got = bool(never_started(cid))
        ok = got == want
        failed += not ok
        print("  %-9s got=%-5s want=%-5s %s  %s" % (cid, got, want, "PASS" if ok else "FAIL", why))
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


def test_never_started_boundaries():
    assert main() == 0


def test_publish_live_logs_exact_attribution(tmp_path):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    nodes = [
        n
        for n in tree.body
        if (
            isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") in {"STALL_GRACE", "_NO_PROGRESS"}
        )
        or (
            isinstance(n, ast.FunctionDef)
            and n.name
            in {
                "_never_started",
                "_record_live_no_progress",
                "publish_live",
                "_worker_cards",
            }
        )
    ]
    home = str(tmp_path)
    live = tmp_path / "live"
    messages = []
    ns = {
        "CardStore": type(
            "CardStore",
            (),
            {
                "__init__": lambda self, _root: None,
                "fold": lambda self, _cid: {
                    "owner": "pi-glm-chiap08-aaaa0001",
                    "meta": {"_claim_revision": "revision-1"},
                },
            },
        ),
        "d": home,
        "glob": glob,
        "hashlib": hashlib,
        "HOME": home,
        "HOST": "chiap08",
        "json": json,
        "LANES": [{"prefix": "glm-auto-"}],
        "LIVE": str(live),
        "Path": Path,
        "log": lambda _directory, message: messages.append(message),
        "os": os,
        "time": time,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), SRC, "exec"), ns)
    logs = tmp_path / ".skcapstone" / "fleet" / "logs"
    logs.mkdir(parents=True)
    path = logs / "aaaa0001-20260831T000000Z.log"
    path.touch()
    old = time.time() - 3600
    os.utime(path, (old, old))

    unit = {
        "card": "aaaa0001",
        "lane": "glm",
        "unit": "skfleet-worker-qwen-aaaa0001.service",
    }
    assert ns["publish_live"]([], [unit]) == ["aaaa0001"]
    assert len(messages) == 1
    assert "|aaaa0001|worker=skfleet-worker-qwen-aaaa0001.service|" in messages[0]
    assert "|log=%s|" % path in messages[0]
    assert "|age_seconds=" in messages[0]
    assert "worker remains live" in messages[0]
    assert json.loads((live / "chiap08.json").read_text())["cards"] == ["aaaa0001"]
    markers = list((tmp_path / ".skcapstone/evidence/live-no-progress").glob("*.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text())
    assert marker["card"] == "aaaa0001"
    assert marker["claim_revision"] == "revision-1"

    assert ns["publish_live"]([], [unit]) == ["aaaa0001"]
    assert len(list((tmp_path / ".skcapstone/evidence/live-no-progress").glob("*.json"))) == 1


if __name__ == "__main__":
    sys.exit(main())
