"""Ignore-semantics unit tests for the supported .stignore subset.

Every rule the bundled template uses must evaluate with confidence, and
everything outside the documented subset must report UNCERTAIN, which the
audit treats as uncovered (fail closed). The canonical template itself is
the first fixture: if a template line ever evaluates unsupported, the audit
would poison every folder report with false uncertainty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.sync_policy import Coverage, evaluate, load_ruleset

REAL_TEMPLATE = (
    Path(__import__("skcapstone").__file__).parent / "defaults" / ".stignore"
).read_text(encoding="utf-8")


def _cov(text: str, path: str, *, is_dir: bool = False) -> Coverage:
    return evaluate(load_ruleset(text), path, is_dir=is_dir)


# ------------------------------------------------------- positive matching ---


def test_plain_suffix_glob_matches_any_depth() -> None:
    assert _cov("*.key", "agent.key") is Coverage.IGNORED
    assert _cov("*.key", "a/b/c.key") is Coverage.IGNORED
    assert _cov("*.key", "agent.keys") is Coverage.EXPOSED


def test_double_star_prefix_matches_any_depth() -> None:
    assert _cov("**/private.*", "private.asc") is Coverage.IGNORED
    assert _cov("**/private.*", "agents/lumina/capauth/identity/private.asc") is Coverage.IGNORED
    assert _cov("**/private.*", "agents/lumina/public.asc") is Coverage.EXPOSED


def test_leading_slash_anchors_at_root() -> None:
    text = "/heartbeats"
    assert _cov(text, "heartbeats") is Coverage.IGNORED
    assert _cov(text, "nested/heartbeats") is Coverage.EXPOSED


def test_interior_slash_anchors_at_root() -> None:
    text = "skcomms/cot-pki/*.key"
    assert _cov(text, "skcomms/cot-pki/device.key") is Coverage.IGNORED
    assert _cov(text, "other/cot-pki/device.key") is Coverage.EXPOSED
    assert _cov(text, "skcomms/cot-pki/nested/device.key") is Coverage.EXPOSED


def test_directory_rule_covers_contents() -> None:
    text = "capauth/security/tokens"
    assert _cov(text, "capauth/security/tokens", is_dir=True) is Coverage.IGNORED
    assert _cov(text, "capauth/security/tokens/tok.token") is Coverage.IGNORED
    assert _cov(text, "capauth/security/tokens/deep/tok.token") is Coverage.IGNORED


def test_trailing_slash_rule_matches_directories_only() -> None:
    text = "logs/"
    assert _cov(text, "logs/daemon.log") is Coverage.IGNORED
    assert _cov(text, "logs", is_dir=False) is Coverage.EXPOSED


def test_double_star_middle_component() -> None:
    text = "a/**/b.key"
    assert _cov(text, "a/b.key") is Coverage.IGNORED
    assert _cov(text, "a/x/y/b.key") is Coverage.IGNORED
    assert _cov(text, "z/x/b.key") is Coverage.EXPOSED


def test_question_mark_and_class() -> None:
    assert _cov("tok?.pass", "tok1.pass") is Coverage.IGNORED
    assert _cov("tok?.pass", "tok12.pass") is Coverage.EXPOSED
    assert _cov("cert.r[eo]v", "cert.rev") is Coverage.IGNORED
    assert _cov("cert.r[eo]v", "cert.rov") is Coverage.IGNORED
    assert _cov("cert.r[eo]v", "cert.riv") is Coverage.EXPOSED


def test_comments_and_blank_lines_are_skipped() -> None:
    text = "// a comment\n# another\n\n*.key\n"
    assert _cov(text, "a.key") is Coverage.IGNORED
    assert _cov(text, "a comment") is Coverage.EXPOSED


def test_case_insensitive_prefix() -> None:
    assert _cov("(?i)*.KEY", "agent.key") is Coverage.IGNORED


def test_deletable_prefix_does_not_change_matching() -> None:
    assert _cov("(?d)**/*.tmp", "x/y/z.tmp") is Coverage.IGNORED


def test_combined_prefixes() -> None:
    assert _cov("(?d)(?i)*.TMP", "x/y/z.tmp") is Coverage.IGNORED


# ------------------------------------------------------- fail-closed paths ---


def test_negation_makes_coverage_uncertain() -> None:
    text = "*.key\n!agent.key\n"
    # A re-inclusion may or may not take effect depending on traversal, so
    # the audit must refuse to vouch for the path it names.
    assert _cov(text, "agent.key") is Coverage.UNCERTAIN
    # A path the negation never touches stays confidently covered.
    assert _cov(text, "other.key") is Coverage.IGNORED


def test_negation_inside_ignored_directory_is_uncertain_not_ignored() -> None:
    text = "agents\n!agents/keep\n"
    # Syncthing never descends into agents/, so the negation never fires;
    # the safe audit answer is still "cannot prove", never "covered".
    assert _cov(text, "agents/keep/private.asc") is Coverage.UNCERTAIN


def test_unknown_prefix_is_uncertain() -> None:
    assert _cov("(?x)*.key", "agent.key") is Coverage.UNCERTAIN
    # And an unsupported rule anywhere poisons confident claims (fail closed).
    assert _cov("(?x)nothing\n*.key", "agent.key") is Coverage.UNCERTAIN


def test_double_negation_is_uncertain() -> None:
    assert _cov("!!*.key", "agent.key") is Coverage.UNCERTAIN


def test_double_star_inside_component_is_uncertain() -> None:
    assert _cov("pri**vate.asc", "private.asc") is Coverage.UNCERTAIN


def test_unbalanced_character_class_is_uncertain() -> None:
    assert _cov("cert.r[ev", "cert.rev") is Coverage.UNCERTAIN


def test_no_match_is_exposed() -> None:
    assert _cov("*.key", "document.md") is Coverage.EXPOSED
    assert _cov("", "anything.key") is Coverage.EXPOSED


def test_empty_path_is_exposed() -> None:
    assert _cov("*.key", "") is Coverage.EXPOSED


def test_later_positive_rule_decides_when_no_negation() -> None:
    text = "logs\n*.log\n"
    assert _cov(text, "logs/daemon.log") is Coverage.IGNORED


# ------------------------------------------------- the canonical template ---


def test_every_template_pattern_evaluates_supported() -> None:
    """The bundled template must never trigger the uncertain branch."""
    rules = load_ruleset(REAL_TEMPLATE)
    assert rules, "template must contain patterns"
    assert all(rule.supported for rule in rules)


@pytest.mark.parametrize(
    "probe",
    [
        "agents/lumina/capauth/identity/private.asc",
        "node/agent.key",
        "certs/agent.pem",
        "deep/nested/file.tmp",
        "heartbeats/node.json",
        "capauth/security/tokens/op.token",
    ],
)
def test_template_covers_canonical_private_and_runtime_paths(probe: str) -> None:
    assert _cov(REAL_TEMPLATE, probe) is Coverage.IGNORED


def test_template_does_not_cover_public_material() -> None:
    assert _cov(REAL_TEMPLATE, "agents/lumina/capauth/identity/public.asc") is Coverage.EXPOSED
    assert _cov(REAL_TEMPLATE, "memory/mid-term/x.json") is Coverage.EXPOSED
