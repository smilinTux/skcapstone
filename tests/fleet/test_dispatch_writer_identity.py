"""The dispatcher writes the board as the seat that owns dispatch (ADR-0006).

skfleet-rotate.py kept writing its releases as jarvis after ADR-0006 moved
fleet dispatch to the niobe seat: measured on chi, 12,230 jarvis events in
the 30 days to 2026-09-18, 4,487 of them after the ADR declared Jarvis
outside recurring scheduling. These tests pin three properties:

  1. the DEFAULT board writer for dispatcher-side mutations is niobe;
  2. the override is the explicit SKFLEET_DISPATCH_AGENT variable and never
     the ambient SKAGENT, which the chi hosts export as jarvis in .bashrc,
     the exact second spelling that would make the drift recur; and
  3. a pre-existing claim HELD by jarvis is still released, not skipped and
     not stolen: the released owner travels separately in --owner and the
     --expected-claim-revision CAS fence is unchanged, so the actor rename
     changes only the audit identity on the release event.

The helpers are lifted out of the shipped script with `ast`, the way
tests/fleet/test_claim_expiry_reaper.py does, so these tests exercise the
source that runs on the fleet rather than a paraphrase of it.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"


def _functions(*names: str) -> dict:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    missing = set(names) - set(wanted)
    assert not missing, f"helpers missing from the script: {sorted(missing)}"
    namespace: dict[str, object] = {"os": os}
    module = ast.Module(body=[wanted[name] for name in names], type_ignores=[])
    exec(compile(module, str(SRC), "exec"), namespace)
    return namespace


def test_default_dispatch_writer_is_niobe() -> None:
    namespace = _functions("_dispatch_writer")
    assert namespace["_dispatch_writer"](env={}) == "niobe"


def test_explicit_override_still_wins() -> None:
    namespace = _functions("_dispatch_writer")
    writer = namespace["_dispatch_writer"](env={"SKFLEET_DISPATCH_AGENT": " Custodian "})
    assert writer == "custodian"


def test_blank_override_falls_back_to_the_seat() -> None:
    namespace = _functions("_dispatch_writer")
    assert namespace["_dispatch_writer"](env={"SKFLEET_DISPATCH_AGENT": "   "}) == "niobe"


def test_ambient_agent_variables_never_choose_the_board_writer() -> None:
    """SKAGENT=jarvis is exported host-wide on chi; it must be inert here."""
    namespace = _functions("_dispatch_writer")
    ambient = {"SKAGENT": "jarvis", "SKCAPSTONE_AGENT": "jarvis", "SKMEMORY_AGENT": "jarvis"}
    assert namespace["_dispatch_writer"](env=ambient) == "niobe"


def test_no_dispatch_site_hardcodes_the_jarvis_writer() -> None:
    source = SRC.read_text(encoding="utf-8")
    assert '"--agent", "jarvis"' not in source
    assert '"--agent","jarvis"' not in source
    resolved = source.count('"--agent", DISPATCH_AGENT') + source.count('"--agent",DISPATCH_AGENT')
    assert resolved == 6, f"expected 6 DISPATCH_AGENT release sites, found {resolved}"
    assert "run_production_cycle(agent=_dispatch_writer())" in source


def test_preexisting_jarvis_held_claim_is_released_not_skipped_or_stolen() -> None:
    namespace = _functions("_dispatch_writer", "_claim_ttl_release_cmd")
    namespace["DISPATCH_AGENT"] = namespace["_dispatch_writer"](env={})
    namespace["SKC"] = "/home/test/.skenv/bin/skcapstone"
    cmd = namespace["_claim_ttl_release_cmd"]("aaaa0001", "jarvis", "rev-aaaa0001")
    argv = [str(part) for part in cmd]
    # The released OWNER is named separately from the release ACTOR, so a
    # claim held by jarvis is still targeted after the actor becomes niobe.
    assert argv[argv.index("--owner") + 1] == "jarvis"
    assert argv[argv.index("--agent") + 1] == "niobe"
    # The CAS fence is untouched: a jarvis worker that re-claimed since the
    # observation produces a revision conflict, a refusal rather than a theft.
    assert argv[argv.index("--expected-claim-revision") + 1] == "rev-aaaa0001"
