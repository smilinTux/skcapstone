"""Seat routing: a card labelled seat-<name> runs under that seat's identity.

The resolver is lifted verbatim out of the shipped script, so this tests the
source that runs rather than a paraphrase of it.

Run: python3 tests/test_seat_routing.py
"""
import ast
import os
import re
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


def _load():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "seat_for")
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)
               and getattr(n.targets[0], "id", "") in ("_SEAT_LABEL_PREFIX", "_SEAT_RE")]
    labels = {}
    ns = {
        "re": re,
        "folded_labels": lambda cid, core: labels.get(cid, []),
        "log": lambda *a, **k: None,
        "d": None,
        "HOST": "testhost",
    }
    exec(compile(ast.Module(body=assigns + [fn], type_ignores=[]), SRC, "exec"), ns)
    return ns["seat_for"], labels


def main():
    seat_for, labels = _load()
    cases = [
        (["seat-link", "trunk"], "link", "a well formed seat label is used"),
        (["trunk", "integrator"], None, "no seat label means lane naming, unchanged"),
        (["SEAT-Link"], "link", "case is normalised, labels are not case sensitive"),
        (["seat-"], None, "empty seat name is rejected, not interpolated"),
        (["seat-a b"], None, "a space would reach a shell command line: rejected"),
        (["seat-../../etc"], None, "path traversal is rejected"),
        (["seat-x;rm -rf /"], None, "shell metacharacters are rejected"),
        (["seat-" + "z" * 40], None, "over-long seat name is rejected"),
        (["seat-9lives"], None, "must start with a letter"),
        (["seat-mero", "seat-link"], "mero", "first valid label wins, deterministically"),
    ]
    failed = 0
    for i, (labs, want, why) in enumerate(cases):
        cid = "card%02d" % i
        labels[cid] = labs
        got = seat_for(cid, {})
        ok = got == want
        failed += not ok
        print("  %-28s got=%-6s want=%-6s %s  %s"
              % (str(labs)[:28], got, want, "PASS" if ok else "FAIL", why))
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
