"""Regression tests for the fleet launcher's PR/dispatch policy constants.

This file pins policy-level distinctions in ``scripts/fleet/skfleet-rotate.py``
that look interchangeable at a glance but answer different governance
questions. The script is not importable (it is a hyphenated top-level
script), so every test here extracts the relevant constants or functions
straight from the source via ``ast`` and executes just those nodes in an
isolated namespace, following the pattern already used by
``tests/test_skfleet_claimability.py`` and ``tests/test_skfleet_lane_affinity.py``.

Later tasks in this plan append more tests here. Keep the extraction
helpers at the top of the file and add new test functions below the
existing ones so the file stays a single coherent source of truth for
PR/dispatch policy behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_category_matchers() -> dict[str, object]:
    """Extract the two category-matching regex constants from the source.

    ``_SENSITIVE_CATEGORY`` gates whether a card needs the
    ``dispatch-approved`` opt-in before it can be dispatched at all.
    ``_QWEN_UNSUITABLE`` gates whether the qwen lane specifically may take
    a card. They share a subject-matter prefix by design; this loader pulls
    both, unmodified, directly from the live source so a future edit that
    accidentally merges or aliases them is caught here rather than in
    production routing.
    """
    names = {"_SENSITIVE_CATEGORY", "_QWEN_UNSUITABLE"}
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
    ]
    found = {
        target.id for node in nodes for target in node.targets if isinstance(target, ast.Name)
    }
    assert found == names, f"expected {names}, found {found}"
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_both_category_matchers_exist() -> None:
    namespace = _load_category_matchers()
    assert isinstance(namespace["_SENSITIVE_CATEGORY"], re.Pattern)
    assert isinstance(namespace["_QWEN_UNSUITABLE"], re.Pattern)


def test_category_matchers_are_not_equivalent() -> None:
    """They must not be collapsed into one pattern: they answer different questions."""
    namespace = _load_category_matchers()
    sensitive = namespace["_SENSITIVE_CATEGORY"]
    qwen_unsuitable = namespace["_QWEN_UNSUITABLE"]

    assert sensitive.pattern != qwen_unsuitable.pattern

    # Both matchers agree that credential-sensitive cards need gating.
    assert sensitive.search("credential rotation") is not None
    assert qwen_unsuitable.search("credential rotation") is not None

    # Only the qwen-suitability matcher cares about schema/architecture work.
    # The dispatch-approved gate does not require sign-off for these terms.
    assert qwen_unsuitable.search("update the schema") is not None
    assert qwen_unsuitable.search("revise the architecture") is not None
    assert sensitive.search("update the schema") is None
    assert sensitive.search("revise the architecture") is None


def _load_pr_required() -> dict[str, object]:
    """Extract ``_SENSITIVE_CATEGORY`` and ``pr_required`` from the source.

    ``pr_required`` reads the raw ``core.json`` dict directly (never through
    ``CardCore``/``CardStore.fold``: a model read silently drops fields on a
    node running an older skcoord), so this loader pulls both the regex it
    depends on and the function itself, unmodified, straight from the live
    source and executes them together in one namespace so the function's
    module-level lookup of ``_SENSITIVE_CATEGORY`` resolves correctly.
    """
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assign_node = None
    func_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_SENSITIVE_CATEGORY"
            for target in node.targets
        ):
            assign_node = node
        if isinstance(node, ast.FunctionDef) and node.name == "pr_required":
            func_node = node
    assert assign_node is not None, "_SENSITIVE_CATEGORY not found"
    assert func_node is not None, "pr_required not found"
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(body=[assign_node, func_node], type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_sensitive_title_requires_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": "rotate the deploy key"}) is True


def test_ordinary_title_does_not_require_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": "fix a typo in the README"}) is False


def test_sensitive_tag_requires_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    core = {"title": "fix a typo in the README", "initial_labels": ["migration"]}
    assert pr_required(core) is True


def test_missing_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({}) is False


def test_empty_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": ""}) is False


def test_non_string_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": None}) is False
    assert pr_required({"title": 12345}) is False


def _load_done_instructions() -> dict[str, object]:
    """Extract ``_worker_done_instructions`` from the source, unmodified.

    Follows the pattern of ``_worker_mail_instructions`` /
    ``_worker_search_instructions`` in ``tests/test_skfleet_mail_routing.py``
    and ``tests/test_skfleet_worker_search_policy.py``: the worker brief text
    is built by a standalone, argument-driven function rather than inline in
    the launch loop, specifically so it can be extracted and unit tested
    without executing the whole fleet-rotate script.
    """
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_worker_done_instructions"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module([node], []), str(ROTATE), "exec"), namespace)
    return namespace


def test_definition_of_done_drops_pr_mandate() -> None:
    """The old blanket PR mandate must be gone from both variants of the text."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    for pr_flag in (False, True):
        text = done_instructions(pr_flag)
        assert "Work is NOT done until it is an open pull request" not in text


def test_definition_of_done_requires_commit_sha_in_verdict() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "commit SHA" in text
    assert "branch name" in text
    assert "verdict" in text
    assert "skmail" in text


def test_definition_of_done_requires_push_before_pr_language() -> None:
    """Pushing the branch, not opening a PR, is what the definition calls the handoff."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "Push the branch" in text
    assert "handoff" in text


def test_pr_required_true_adds_immediate_pr_instruction() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(True)
    assert "gh pr create" in text
    assert "PR URL" in text
    for category in (
        "capauth",
        "credential",
        "custody",
        "issuer",
        "secret",
        "key",
        "rollback",
        "deploy",
        "production",
        "release",
        "migration",
    ):
        assert category in text


def test_pr_required_false_omits_immediate_pr_instruction() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "gh pr create" not in text


def test_definition_of_done_no_em_dash_or_en_dash() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    for pr_flag in (False, True):
        text = done_instructions(pr_flag)
        assert "—" not in text
        assert "–" not in text


def test_call_site_wires_pr_required_into_done_instructions() -> None:
    """The launch loop must call _worker_done_instructions(pr_required(core)).

    A unit-tested function nobody calls proves nothing about the assembled
    launch string. This checks the raw source for the call site so a future
    edit cannot silently stop wiring the two together.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "_worker_done_instructions(pr_required(core))" in source


def test_old_pr_mandate_string_removed_from_whole_file() -> None:
    """The literal old-policy sentence must not survive anywhere in the file."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "Work is NOT done until it is an open pull request" not in source
    assert (
        "Put the PR URL in your verdict AND in your skmail. A verdict claiming work was"
        not in source
    )


def test_prompt_category_prose_matches_the_regex_it_describes():
    """The prompt restates _SENSITIVE_CATEGORY's terms as prose; pin the pair.

    The immediate-PR clause lists the sensitive categories in words so a worker
    can act on them without reading a regex. That is a second copy of the same
    policy, and this effort has already been bitten twice by a second copy
    drifting from its original. Adding a term to the regex without adding it to
    the prose would gate a card the worker was never told about, which reads to
    the worker as an arbitrary refusal.

    Terms are matched as prefixes because the regex carries stems: "migrat"
    covers the prose word "migration".
    """
    sensitive = _load_category_matchers()["_SENSITIVE_CATEGORY"]
    build = _load_done_instructions()["_worker_done_instructions"]
    prose = build(True)

    terms = [
        term.strip("()").replace("\\b", "") for term in sensitive.pattern.strip("()").split("|")
    ]
    assert terms, "no terms extracted from _SENSITIVE_CATEGORY"

    missing = [term for term in terms if term not in prose]
    assert not missing, (
        "sensitive categories in the regex but not described to the worker: " f"{missing}"
    )


# ---- Task 4: the branch and SHA must actually land in the evidence store ----
#
# The existing evidence-recording path, enumerated by reading the source
# rather than assumed: a worker (or a human) runs
# ``skcapstone coord link <card> <key> <value>`` (src/skcapstone/cli/coord.py,
# ``coord_link``), which appends a CardEvent(action="link") to
# ``coordination/card_events/<host>.jsonl`` via CardEventLog.append
# (skcoord/card.py). ``CardStore.fold`` (skcoord/card_store.py) merges that
# overlay log with each card's own ``cards/<id>/events/`` store log and folds
# every "link" event into ``card.links[link_key] = link_value`` with no
# allowlist. ``coordination/`` is the directory the project CLAUDE.md
# documents as Syncthing-synced, which is what makes this the "replicated
# evidence record" the spec's cross-host-visibility argument depends on. This
# is a different, and separate, mechanism from _fold_claimability's local
# ``state["links"]`` allowlist a few hundred lines below in this same file,
# which only feeds board claimability decisions and intentionally recognizes
# a short, unrelated list of typed review keys (producer_identity, pr, and so
# on); branch and commit_sha do not need to join that allowlist because
# nothing about claimability depends on them.
#
# Before this task, _worker_done_instructions told a worker to put the SHA
# and branch name only in the verdict prose and in skmail. Both are read by a
# human, not queried by another host: skmail is explicitly documented a few
# lines later in this same file as a location "NOTHING reads" programmatically,
# and prose in a verdict string is not a stable field. So the SHA the spec's
# argument depends on was never landing in the queryable evidence store at
# all. This is fixed by having the DEFINITION OF DONE instruct the worker to
# also record ``branch`` and ``commit_sha`` as their own evidence links via
# ``skcapstone coord link``, the existing mechanism above, and nothing new.


def test_valid_commit_sha_accepts_a_genuine_forty_char_sha() -> None:
    namespace = _load_valid_commit_sha()
    valid = namespace["_valid_commit_sha"]
    assert valid("a" * 40) is True
    assert valid("0123456789abcdef0123456789abcdef01234567") is True


def test_valid_commit_sha_accepts_uppercase_hex() -> None:
    """Git itself is case-insensitive about hex digits; do not punish a worker for case."""
    namespace = _load_valid_commit_sha()
    valid = namespace["_valid_commit_sha"]
    assert valid("A" * 40) is True


def test_valid_commit_sha_rejects_the_none_sentinel() -> None:
    """The literal value none is the explicit no-repository-change sentinel, not a SHA."""
    namespace = _load_valid_commit_sha()
    valid = namespace["_valid_commit_sha"]
    assert valid("none") is False


def test_valid_commit_sha_rejects_wrong_length_and_non_hex() -> None:
    namespace = _load_valid_commit_sha()
    valid = namespace["_valid_commit_sha"]
    assert valid("a" * 39) is False
    assert valid("a" * 41) is False
    assert valid("g" * 40) is False
    assert valid("") is False


def test_valid_commit_sha_rejects_non_string() -> None:
    namespace = _load_valid_commit_sha()
    valid = namespace["_valid_commit_sha"]
    assert valid(None) is False
    assert valid(40) is False


def _load_valid_commit_sha() -> dict[str, object]:
    """Extract ``_COMMIT_SHA_RE`` and ``_valid_commit_sha`` from the source."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assign_node = None
    func_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_COMMIT_SHA_RE"
            for target in node.targets
        ):
            assign_node = node
        if isinstance(node, ast.FunctionDef) and node.name == "_valid_commit_sha":
            func_node = node
    assert assign_node is not None, "_COMMIT_SHA_RE not found"
    assert func_node is not None, "_valid_commit_sha not found"
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(body=[assign_node, func_node], type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_definition_of_done_instructs_recording_branch_as_an_evidence_link() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "coord link" in text
    assert "branch" in text


def test_definition_of_done_instructs_recording_commit_sha_as_an_evidence_link() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "commit_sha" in text
    assert "coord link" in text


def test_definition_of_done_distinguishes_verdict_prose_from_evidence_link() -> None:
    """The prose-only channels (verdict, skmail) must not be presented as sufficient.

    This is the exact gap the brief opened with: recording the SHA only in
    prose is indistinguishable, to a downstream reader, from never recording
    it at all. The instructions must say the link is what gets read, not the
    prose.
    """
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "not only" in text or "not just" in text


def test_definition_of_done_gives_an_explicit_no_repo_change_sentinel() -> None:
    """A card needing no repository change must record that, not omit the key.

    Otherwise a legitimate "nothing to link" outcome is indistinguishable from
    a worker that simply failed to record its SHA, which is exactly the
    ambiguity design point 3 of the brief calls out.
    """
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False)
    assert "commit_sha" in text
    assert "none" in text


def test_definition_of_done_with_links_still_has_no_em_dash_or_en_dash() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    for pr_flag in (False, True):
        text = done_instructions(pr_flag)
        assert "—" not in text
        assert "–" not in text


def test_branch_and_commit_sha_round_trip_through_coord_link(tmp_path) -> None:
    """Prove the enumerated path actually works end to end, not just in prose.

    This runs the real write path a worker is told to use
    (``skcapstone coord link``) and the real read path another host uses
    (``CardStore.fold``), against a real temporary ``~/.skcapstone``-shaped
    home. It is deliberately NOT another extracted-source unit test: the
    claim under test is that the SHA genuinely lands in and comes back out of
    the replicated evidence store, which an isolated-namespace exec of one
    function can never demonstrate.
    """
    import click
    from click.testing import CliRunner

    from skcapstone.card_store import CardCore, CardStore
    from skcapstone.cli.coord import register_coord_commands

    @click.group()
    def main():
        pass

    register_coord_commands(main)

    CardStore(tmp_path).create(CardCore(id="ev00001", title="fix a typo in the README"))

    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    branch = "fix/ev00001-typo"
    runner = CliRunner()
    for key, value in (("branch", branch), ("commit_sha", sha)):
        result = runner.invoke(
            main,
            [
                "coord",
                "link",
                "ev00001",
                key,
                value,
                "--home",
                str(tmp_path),
                "--agent",
                "worker-test",
            ],
        )
        assert result.exit_code == 0, result.output

    card = CardStore(tmp_path).fold("ev00001")
    assert card.links["branch"] == branch
    assert card.links["commit_sha"] == sha


def test_branch_and_commit_sha_no_repo_change_sentinel_round_trips(tmp_path) -> None:
    """The explicit "no repository change" sentinel must also round trip and read
    back as an invalid SHA, distinguishable from a real one."""
    import click
    from click.testing import CliRunner

    from skcapstone.card_store import CardCore, CardStore
    from skcapstone.cli.coord import register_coord_commands

    @click.group()
    def main():
        pass

    register_coord_commands(main)

    CardStore(tmp_path).create(CardCore(id="ev00002", title="update docs only"))

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "coord",
            "link",
            "ev00002",
            "commit_sha",
            "none",
            "--home",
            str(tmp_path),
            "--agent",
            "worker-test",
        ],
    )
    assert result.exit_code == 0, result.output

    card = CardStore(tmp_path).fold("ev00002")
    assert card.links["commit_sha"] == "none"

    namespace = _load_valid_commit_sha()
    assert namespace["_valid_commit_sha"](card.links["commit_sha"]) is False


def test_branch_link_instruction_is_repo_qualified():
    """A bare branch name is ambiguous across a multi-repo fleet.

    Measured before this was added: the 14 existing branch links in the live
    event log hold three distinct values in three different shapes, a bare
    branch, a GitHub tree URL, and one repo-qualified name. So there was no
    convention to match, only one to establish. The fleet dispatches work
    across skcapstone, skcoord, skchat and others, and the Integrator reading
    a recorded branch has to know which repository to fetch it from.
    """
    build = _load_done_instructions()["_worker_done_instructions"]
    text = build(False)

    assert "<repo>:<branch-name>" in text
    assert "skcapstone:fix/abc123" in text
    assert "coord link <card> commit_sha" in text
