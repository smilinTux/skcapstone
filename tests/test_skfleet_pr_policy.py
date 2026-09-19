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
    core = {"title": "fix a typo in the README"}
    assert pr_required(core, ["migration"]) is True


def test_pr_required_reads_the_labels_argument_not_raw_initial_labels() -> None:
    """A label added after creation via `coord label` never appears in a
    card's raw initial_labels; it only shows up once folded (initial_labels
    plus every add_label/remove_label event). pr_required must be handed
    that folded set explicitly and judge it, not silently re-derive an
    unfolded answer from core.json on its own. Measured live: reading raw
    initial_labels missed about 0.3 percent of cards where the sensitive
    label was added after creation."""
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    core = {"title": "fix a typo in the README", "initial_labels": []}
    assert pr_required(core, ["deploy"]) is True
    assert pr_required(core) is False


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
    """The launch loop must call _worker_done_instructions(pr_required(core, labels)).

    A unit-tested function nobody calls proves nothing about the assembled
    launch string. This checks the raw source for the call site so a future
    edit cannot silently stop wiring the two together. The labels argument
    must be an already-folded label list (see
    test_pr_required_reads_the_labels_argument_not_raw_initial_labels), never
    a raw core.get("initial_labels") read reintroduced at the call site.

    Fence moved deliberately 2026-09-18, not relaxed. The call now also
    passes card_forbids_push(core), so the exact old one-argument literal can
    no longer appear in the source. The thing this fence actually guards,
    that pr_required is wired in with an already-folded label list, is
    unchanged and still asserted below; the second argument is asserted with
    it so the new handoff gate cannot be silently unwired either. Evidence
    for the second argument's existence: 216 live chi cards whose own
    criteria forbid push were being handed the push mandate anyway (see the
    section at the end of this file).
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "_worker_done_instructions(pr_required(core, _labels)," in source
    assert "card_forbids_push(core))" in source


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


# The genuine-SHA shape check used to be defined in this script
# (_COMMIT_SHA_RE / _valid_commit_sha) and pinned here via the same
# AST-extraction pattern the rest of this file uses, but that stranded the
# one real implementation in a location no production code could reach: this
# script is a launcher, not the gate, and never called it. The
# implementation now lives in skcapstone.coord_completion.commit_sha_is_valid,
# next to complete_coord_task and move_coord_task, the two entrypoints that
# actually enforce it, and its behaviour is pinned in
# tests/test_coord_commit_evidence.py by direct import instead of source
# extraction, because that module is a normal importable part of the
# package.


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

    from skcapstone.coord_completion import commit_sha_is_valid

    assert commit_sha_is_valid(card.links["commit_sha"]) is False


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


# ---- The card's own criteria beat the generic push rail ----
#
# Measured 2026-09-18 on the chi CardStore via CardStore.fold in a fresh
# process: 216 live (non-archived, backlog/ready/doing/review) cards carry
# acceptance criteria or a description forbidding push in the card's own
# words. Every one of them was also handed the standing DEFINITION OF DONE,
# which made pushing mandatory. Both texts land in ONE file: verified in
# ~/.skcapstone/fleet/logs/brief-009ed46e.txt on chiap08, where line 31 says
# "Commit to a feature branch, push it, and open a PR. This is required, not
# optional", line 94 says "Push the branch and open a PR with gh pr create",
# and line 109 says "No deployment, migration, activation, probe, merge, or
# push." That is a worker being given two mutually exclusive orders in one
# prompt.
#
# The ruling: the card wins. Its criteria encode a deliberate per-card safety
# decision (several of these cards audit live hosts, protected content, or
# credentials) and the rail is a generic default. The resolution is NOT to
# drop the push requirement everywhere, and NOT to let a no-push card push.
# It is to give such a card a DIFFERENT, reachable definition of done: commit
# locally, publish the candidate bytes to the replicating evidence store, and
# record the SHA as an evidence link.


def _load_push_detector() -> dict[str, object]:
    """Extract card_forbids_push and its helpers, unmodified, from the source."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted_defs = {"card_forbids_push", "_clause_forbids_push"}
    wanted_names = {
        "_PUSH_NEGATION",
        "_UNQUALIFIED_PUSH",
        "_PUSH_TO_TRUNK",
        "_CLAUSE_SPLIT",
    }
    nodes = [
        item
        for item in tree.body
        if (isinstance(item, ast.FunctionDef) and item.name in wanted_defs)
        or (
            isinstance(item, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted_names
                for target in item.targets
            )
        )
    ]
    namespace: dict[str, object] = {"re": re}
    exec(compile(ast.Module(nodes, []), str(ROTATE), "exec"), namespace)
    return namespace


def test_card_forbids_push_detects_real_live_card_text() -> None:
    """Every string here is copied verbatim from a live chi card, via fold."""
    forbids = _load_push_detector()["card_forbids_push"]
    for description in (
        # brief-009ed46e.txt line 109, the card in the proof above.
        "Independent review of exact source candidate fa0a050f from base"
        " 4a471b1d. Evidence only. No deployment, migration, activation,"
        " probe, merge, or push.",
        "Source-only. No live database write, provider, Inbox, mailing,"
        " deployment, push, or external action.",
        "No edits, commits, pushes, protected paths, HammerTime Inbox"
        " access, or external actions.",
        "Do not deploy, push, invoke a provider on Matter content, access"
        " Inbox, dispatch mail, or perform external action.",
        "No runtime installation, live board migration, merge, or push" " occurs.",
        "Review without repair, merge, push, runtime mutation, provider"
        " traffic, or credential access.",
    ):
        assert forbids({"description": description}) is True, description


def test_card_forbids_push_reads_acceptance_criteria_not_only_description() -> None:
    """The prohibition lives in acceptance_criteria at least as often as in prose."""
    forbids = _load_push_detector()["card_forbids_push"]
    card = {
        "description": "Audit the named boundary against current contracts.",
        "acceptance_criteria": [
            "Record PASS or FAIL with limitations.",
            "No deploy, push, Inbox, protected-data access, or external action.",
        ],
    }
    assert forbids(card) is True


def test_card_forbids_push_ignores_a_force_push_only_prohibition() -> None:
    """4 live cards forbid a force push while requiring one normal push.

    Flipping those to the no-push handoff would take away the push their own
    criteria demand, so the qualifier has to be honoured.
    """
    forbids = _load_push_detector()["card_forbids_push"]
    card = {
        "description": "No force push, rebase, other tag, manual upload.",
        "acceptance_criteria": [
            "Perform one normal non-force fast-forward push only.",
        ],
    }
    assert forbids(card) is False


def test_card_forbids_push_ignores_a_trunk_only_prohibition() -> None:
    """ "No push to main" is the standing branch policy, not a no-push card."""
    forbids = _load_push_detector()["card_forbids_push"]
    assert (
        forbids({"description": "No board mutation, deployment, release, merge, or push to main."})
        is False
    )


def test_card_forbids_push_needs_the_negation_before_the_push_token() -> None:
    """Verbatim live text where push precedes an unrelated negation."""
    forbids = _load_push_detector()["card_forbids_push"]
    assert (
        forbids(
            {
                "description": "Tags are only cut from commits that are ancestors"
                " of main; a push to a feature branch produces no tag and no PyPI"
                " release"
            }
        )
        is False
    )


def test_card_forbids_push_leaves_ordinary_cards_alone() -> None:
    forbids = _load_push_detector()["card_forbids_push"]
    assert forbids({"description": "Push the refreshed branch."}) is False
    assert forbids({"description": "Fix the flaky test."}) is False
    assert forbids({}) is False
    assert forbids(None) is False
    assert forbids({"description": None, "acceptance_criteria": None}) is False


def test_no_push_card_never_receives_the_push_mandate() -> None:
    """The whole point: the two orders must never arrive in one prompt."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    for pr_flag in (False, True):
        text = done_instructions(pr_flag, True)
        assert "Push the branch" not in text
        assert "pushing is mandatory" not in text
        assert "gh pr create" not in text
        assert "the pushed branch plus" not in text


def test_no_push_definition_of_done_is_actually_reachable() -> None:
    """Removing the push mandate is not enough; DONE must still be attainable.

    Everything the push rail protected has to survive the substitution: the
    work outlives the worktree, another host can verify it, and the evidence
    store can be queried for it. Only the transport changes.
    """
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False, True)
    assert "DEFINITION OF DONE" in text
    assert "Branch first" in text
    assert "Commit as soon as the code is written" in text
    assert "~/.skcapstone/evidence/work/<card_id>/" in text
    assert "sha256" in text
    assert "coord link <card> commit_sha" in text
    assert "none" in text


def test_no_push_definition_of_done_says_the_card_outranks_the_rail() -> None:
    """A worker has to be told WHY, or it reads as an arbitrary exception."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False, True)
    assert "Do not push" in text
    assert "blocked_on=card" in text


def test_no_push_definition_of_done_protects_the_only_copy() -> None:
    """With no push, the worktree holds the sole copy of the commit."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False, True)
    assert "do NOT delete your own workspace" in text


def test_no_push_definition_of_done_has_no_em_dash_or_en_dash() -> None:
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    for pr_flag in (False, True):
        text = done_instructions(pr_flag, True)
        assert "—" not in text
        assert "–" not in text


def test_default_path_still_mandates_the_push() -> None:
    """The fix must not weaken the default. Only a no-push card is exempted."""
    done_instructions = _load_done_instructions()["_worker_done_instructions"]
    text = done_instructions(False, False)
    assert "Push the branch" in text
    assert "pushing is mandatory" in text


def test_invariant_rails_do_not_assert_the_push_mandate_themselves() -> None:
    """The standing rails sit ABOVE the card-aware DEFINITION OF DONE.

    They are also the vLLM prefix-cache prefix, so they cannot be made
    card-dependent. The old bullet asserted "Commit to a feature branch and
    push it. This is required, not optional" up there, which re-delivered the
    contradiction to every no-push card no matter what the DONE section said.
    It must defer to DEFINITION OF DONE instead of pre-empting it.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "Commit to a feature branch and push it. This is required" not in source
    assert "Clean up a worktree or branch ONLY after its work is pushed" not in source
