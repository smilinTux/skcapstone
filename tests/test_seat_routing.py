"""Seat routing: a card labelled seat-<name> runs under that seat's identity.

Both functions are lifted verbatim out of the shipped script, so this tests the
source that runs rather than a paraphrase of it.

Run: python3 tests/test_seat_routing.py
"""

import ast
import os
import re
import sys
import tempfile

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


def _load(home):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fns = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("seat_for", "_seat_is_provisioned")
    ]
    assert len(fns) == 2, "expected seat_for and _seat_is_provisioned in the shipped script"
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", "") in ("_SEAT_LABEL_PREFIX", "_SEAT_RE")
    ]
    labels = {}
    ns = {
        "re": re,
        "os": os,
        "folded_labels": lambda cid, core: labels.get(cid, []),
        "log": lambda *a, **k: None,
        "d": None,
        "HOST": "testhost",
        "HOME": home,
    }
    exec(compile(ast.Module(body=assigns + fns, type_ignores=[]), SRC, "exec"), ns)
    return ns["seat_for"], labels


def provision(home, seat):
    d = os.path.join(home, ".skcapstone/agents", seat, "capauth/identity")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "public.asc"), "w").write("-----BEGIN PGP PUBLIC KEY BLOCK-----\n")


def main():
    home = tempfile.mkdtemp()
    seat_for, labels = _load(home)
    provision(home, "link")
    provision(home, "mero")
    # an agent home with NO public key: present but not a provisioned seat
    os.makedirs(os.path.join(home, ".skcapstone/agents/lumina"), exist_ok=True)

    cases = [
        (["seat-link", "trunk"], "link", "provisioned seat is used"),
        (["seat-mero"], "mero", "the mechanism is generic, not link-specific"),
        (["trunk", "integrator"], None, "no seat label means lane naming, unchanged"),
        (["SEAT-Link"], "link", "case is normalised, labels are not case sensitive"),
        (["seat-lnik"], None, "TYPO: well formed but not provisioned, no phantom seat"),
        (["seat-lumina"], None, "agent home without a public key is not a seat"),
        (["seat-"], None, "empty seat name is rejected, not interpolated"),
        (["seat-a b"], None, "a space would reach a shell command line: rejected"),
        (["seat-x;rm -rf /"], None, "shell metacharacters are rejected"),
        (["seat-../../etc"], None, "path traversal is rejected"),
        (["seat-" + "z" * 40], None, "over-long seat name is rejected"),
        (["seat-9lives"], None, "must start with a letter"),
        (["seat-lnik", "seat-link"], "link", "a bad seat does not mask a good one"),
        (["seat-mero", "seat-link"], "mero", "first valid label wins, deterministically"),
    ]
    failed = 0
    for i, (labs, want, why) in enumerate(cases):
        cid = "card%02d" % i
        labels[cid] = labs
        got = seat_for(cid, {})
        ok = got == want
        failed += not ok
        print(
            "  %-30s got=%-6s want=%-6s %s  %s"
            % (str(labs)[:30], got, want, "PASS" if ok else "FAIL", why)
        )
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


def test_seat_routing():
    """Pytest entry point.

    The 14 cases below run as a standalone script, which is how they were
    written and how they are still most readable. Living in tests/ without a
    test_ function meant pytest collected 0 items and CI never ran a single
    one of them, so the check existed while the gate ignored it.
    """
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
