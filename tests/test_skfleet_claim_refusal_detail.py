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
from types import SimpleNamespace

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


def _claim_retry_helpers():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    names = {"_is_board_mutation_lock_timeout", "_run_coord_claim"}
    body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace: dict = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def test_lock_timeout_retries_with_fresh_readback_before_each_claim():
    """A transient board lock timeout is retried without hiding a later claim."""
    helpers = _claim_retry_helpers()
    results = iter(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="TimeoutError: timed out acquiring board mutation lock",
            ),
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="TimeoutError: timed out acquiring board mutation lock",
            ),
            SimpleNamespace(returncode=0, stdout="claimed", stderr=""),
        ]
    )
    owners = iter(
        [(None, None, None), (None, None, None), ("glm-chiap01-abcd1234", "now", "rev-3")]
    )
    calls = []
    sleeps = []

    result, owner, claimed_at, revision, attempts = helpers["_run_coord_claim"](
        ["skcapstone", "coord", "claim", "abcd1234"],
        "abcd1234",
        run=lambda command, **kwargs: (calls.append((command, kwargs)) or next(results)),
        readback=lambda card_id: (card_id == "abcd1234" and next(owners)),
        sleep=sleeps.append,
    )

    assert (result.returncode, owner, claimed_at, revision, attempts) == (
        0,
        "glm-chiap01-abcd1234",
        "now",
        "rev-3",
        3,
    )
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_policy_refusal_is_not_retried():
    helpers = _claim_retry_helpers()
    calls = []
    result, owner, claimed_at, revision, attempts = helpers["_run_coord_claim"](
        ["skcapstone", "coord", "claim", "abcd1234"],
        "abcd1234",
        run=lambda command, **kwargs: (
            calls.append(command)
            or SimpleNamespace(
                returncode=1,
                stdout="Error: human claim denied: blocked_on_human=approval:exact-head",
                stderr="",
            )
        ),
        readback=lambda _card_id: (None, None, None),
        sleep=lambda _delay: None,
    )

    assert result.returncode == 1
    assert owner is claimed_at is revision is None
    assert attempts == 1
    assert len(calls) == 1


def test_lock_timeout_stops_if_another_owner_appears():
    helpers = _claim_retry_helpers()
    calls = []
    result, owner, claimed_at, revision, attempts = helpers["_run_coord_claim"](
        ["skcapstone", "coord", "claim", "abcd1234"],
        "abcd1234",
        run=lambda command, **kwargs: (
            calls.append(command)
            or SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="TimeoutError: timed out acquiring board mutation lock",
            )
        ),
        readback=lambda _card_id: ("other-owner", "then", "other-revision"),
        sleep=lambda _delay: None,
    )

    assert result.returncode == 1
    assert (owner, claimed_at, revision, attempts) == (
        "other-owner",
        "then",
        "other-revision",
        1,
    )
    assert len(calls) == 1


def test_lock_timeout_retries_are_bounded():
    helpers = _claim_retry_helpers()
    calls = []
    sleeps = []
    result, owner, claimed_at, revision, attempts = helpers["_run_coord_claim"](
        ["skcapstone", "coord", "claim", "abcd1234"],
        "abcd1234",
        run=lambda command, **kwargs: (
            calls.append(command)
            or SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="TimeoutError: timed out acquiring board mutation lock",
            )
        ),
        readback=lambda _card_id: (None, None, None),
        sleep=sleeps.append,
    )

    assert result.returncode == 1
    assert owner is claimed_at is revision is None
    assert attempts == 3
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_claim_refusal_detail_keeps_traceback_terminal_reason():
    detail = _detail()
    stderr = (
        "Traceback (most recent call last):\n  File 'coordination.py', line 106\n"
        "TimeoutError: timed out acquiring board mutation lock"
    )
    assert detail("", stderr) == "TimeoutError: timed out acquiring board mutation lock"


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
    assert "_run_coord_claim(" in source
    assert "CLAIM_LOCK_RETRIED|" in source
