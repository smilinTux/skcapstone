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
import os
import sys
import tempfile
import time

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


def _load():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_never_started")
    grace = next(n for n in tree.body if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "STALL_GRACE")
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
        got = never_started(cid)
        ok = got == want
        failed += not ok
        print("  %-9s got=%-5s want=%-5s %s  %s"
              % (cid, got, want, "PASS" if ok else "FAIL", why))
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
