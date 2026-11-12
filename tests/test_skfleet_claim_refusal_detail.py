"""A claim refusal must report the reason, not the noise that drowned it.

Measured 2026-09-21 on chiap04 card bde8bd35: every CLAIM_REFUSED receipt read
"card_events chiap08.jsonl line 19797 is not a card event" while the actual
reason, on stdout, was "human claim denied:
blocked_on_human=approval:exact-repository-head". The host looked broken for
hours when it was correctly waiting on an approval and saying so, into a field
nobody could see.
"""

import ast
from pathlib import Path

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"

FOLD_WARNING = (
    "card_events chiap08.jsonl line 19797 is not a card event, dropping it "
    "from the fold: ValidationError"
)
REAL_ERROR = "Error: human claim denied: blocked_on_human=approval:exact-repository-head"


def _detail():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "_claim_failure_detail")
        or (
            isinstance(node, ast.Assign)
            and {t.id for t in node.targets if isinstance(t, ast.Name)} == {"_BENIGN_FOLD_NOISE"}
        )
    ]
    namespace: dict = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_claim_failure_detail"]


def test_the_real_reason_wins_over_a_benign_fold_warning():
    """The exact bde8bd35 shape."""
    assert _detail()(REAL_ERROR, FOLD_WARNING) == REAL_ERROR


def test_a_real_stderr_error_still_wins_over_stdout():
    """stderr is still preferred when it carries something that is not noise."""
    assert _detail()("some stdout", "boom: permission denied") == "boom: permission denied"


def test_noise_on_both_streams_falls_back_rather_than_vanishing():
    """An unexplained refusal is worse than a noisy one."""
    got = _detail()(FOLD_WARNING, FOLD_WARNING)
    assert got, "must never return an empty detail"
    assert "card_events" in got


def test_both_streams_empty_gets_the_documented_fallback():
    assert "CardStore fold" in _detail()("", "")


def test_the_warning_is_dropped_even_when_interleaved_with_the_reason():
    """The fold can warn on several lines around the real message."""
    stderr = FOLD_WARNING + "\ncard_events chiap08.jsonl: 3 unreadable lines total"
    assert _detail()(REAL_ERROR, stderr) == REAL_ERROR


def test_the_refusal_site_uses_the_helper():
    """Guards the wiring, not just the helper."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "_claim_failure_detail(claim.stdout, claim.stderr)" in source
    assert (
        "claim.stderr or claim.stdout" not in source
    ), "the old stderr-wins expression must be gone"
