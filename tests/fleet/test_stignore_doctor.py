"""A missing ignore ruleset must be DETECTED (card 20a1d4d3, epic 3bbf39ea).

Every test here is written as a negative control first: the point of this
module is not that the check passes on a good node, it is that it FAILS on a
bad one. A check nobody has seen fail is not known to work.

Ground truth these tests encode, verified read-only on 2026-08-16 across all
three member nodes of the `skcapstone-sync` folder (.158 noroc2027, .41
cbrd21-laptop12thgenintelcore, .100 ollama): all three carry `*.key`,
`*.pem` and `**/private.*` at the top of ~/.skcapstone/.stignore, and .100
holds zero PGP PRIVATE KEY BLOCKs while .158 holds eleven private.asc files
in the same sendreceive folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.fleet import stignore_doctor as sd

REAL_TEMPLATE = (
    Path(__import__("skcapstone").__file__).parent / "defaults" / ".stignore"
).read_text(encoding="utf-8")

GOOD = """\
// SKCapstone Sovereign Singularity - Syncthing ignore rules
// Private key material must never leave this node
*.key
*.pem
**/private.*
**/telegram.session
skcomms/cot-pki/*.key
skcomms/cot-pki/devices
skcomms/cot-pki/packages
capauth/security/tokens
"""

#: The failure mode this card exists for: a node inside the sovereign folder
#: whose private-key rules were removed some way `_write_stignore` cannot see.
STRIPPED = """\
// somebody rewrote this by hand and kept only the noise rules
__pycache__
*.pyc
sessions
"""


@pytest.fixture
def ruleset():
    return sd.DEFAULT_RULESETS["skcapstone-sync"]


# ------------------------------------------------- negative control, text ---


def test_stripped_private_key_rules_are_flagged(ruleset) -> None:
    """THE negative control. Remove the three rules, the check must scream."""
    report = sd.check_text(ruleset, STRIPPED)
    assert report.severity == "error"
    assert not report.clean
    assert report.missing_required == ["**/private.*", "*.key", "*.pem"]


def test_intact_ruleset_is_clean(ruleset) -> None:
    report = sd.check_text(ruleset, GOOD)
    assert report.clean
    assert report.severity == "ok"
    assert report.findings() == []


def test_the_shipped_template_satisfies_its_own_folder(ruleset) -> None:
    """A template that cannot pass the check would install a failing node."""
    report = sd.check_text(ruleset, REAL_TEMPLATE)
    assert report.clean, report.as_dict()


@pytest.mark.parametrize("rule", ["*.key", "*.pem", "**/private.*"])
def test_dropping_any_single_secret_rule_is_an_error(ruleset, rule: str) -> None:
    text = "\n".join(line for line in GOOD.splitlines() if line.strip() != rule)
    report = sd.check_text(ruleset, text)
    assert report.severity == "error"
    assert report.missing_required == [rule]


def test_a_near_miss_pattern_does_not_count_as_present(ruleset) -> None:
    """Syncthing matches exactly, so a fuzzy match here would report safety
    that does not exist."""
    report = sd.check_text(ruleset, GOOD.replace("*.key", "*.keys"))
    assert "*.key" in report.missing_required


def test_missing_credential_rules_only_warn(ruleset) -> None:
    """Warn, not error: these cover subsystems a node may not run, and
    grading them error is how a report gets ignored."""
    text = "\n".join(
        line
        for line in GOOD.splitlines()
        if line.strip() not in ("**/telegram.session", "capauth/security/tokens")
    )
    report = sd.check_text(ruleset, text)
    assert report.severity == "warn"
    assert report.missing_required == []
    assert report.missing_recommended == ["**/telegram.session", "capauth/security/tokens"]


def test_comments_never_satisfy_a_rule(ruleset) -> None:
    """A rule mentioned only in a comment protects nothing."""
    report = sd.check_text(ruleset, "// *.key\n// *.pem\n// **/private.*\n")
    assert len(report.missing_required) == 3


def test_findings_are_gradeable_rows(ruleset) -> None:
    rows = sd.check_text(ruleset, STRIPPED).findings()
    assert ("error", "missing_required_ignore", "*.key") in rows
    assert all(grade in ("error", "warn") for grade, _, _ in rows)


def test_as_dict_is_stable_and_carries_severity(ruleset) -> None:
    first = sd.check_text(ruleset, GOOD).as_dict()
    second = sd.check_text(ruleset, GOOD).as_dict()
    assert first == second
    assert first["severity"] == "ok"
    assert first["folder"] == "skcapstone-sync"


# ---------------------------------------------- negative control, on disk ---


def test_folder_with_no_stignore_at_all_is_an_error(ruleset, tmp_path) -> None:
    """No file is worse than a stripped file, and must not read as clean."""
    report = sd.check_folder(ruleset, root=tmp_path)
    assert report is not None
    assert report.present is False
    assert report.severity == "error"
    assert ("error", "no_stignore", str(tmp_path)) in report.findings()


def test_folder_on_disk_flags_a_stripped_file(ruleset, tmp_path) -> None:
    (tmp_path / ".stignore").write_text(STRIPPED, encoding="utf-8")
    report = sd.check_folder(ruleset, root=tmp_path)
    assert report.severity == "error"
    assert "*.key" in report.missing_required


def test_folder_on_disk_is_clean_when_rules_are_present(ruleset, tmp_path) -> None:
    (tmp_path / ".stignore").write_text(GOOD, encoding="utf-8")
    report = sd.check_folder(ruleset, root=tmp_path)
    assert report.clean
    assert report.root == str(tmp_path)


def test_a_folder_this_node_does_not_hold_is_skipped(ruleset, tmp_path) -> None:
    """None and clean are different answers: 'not here' is not 'safe'."""
    assert sd.check_folder(ruleset, root=tmp_path / "absent") is None


def test_unknown_folder_with_no_definition_is_skipped() -> None:
    empty = sd.ruleset_from_spec("some-other-folder", None)
    assert empty.root == ""
    assert sd.check_folder(empty) is None


# ------------------------------------------------------ folder-keyed spec ---


def test_a_folder_object_may_add_rules(ruleset, tmp_path) -> None:
    merged = sd.ruleset_from_spec(
        "skcapstone-sync",
        {"root": str(tmp_path), "requiredIgnores": ["**/*.gpg"]},
    )
    assert "**/*.gpg" in merged.required
    for rule in ruleset.required:
        assert rule in merged.required


def test_a_folder_object_can_never_drop_a_built_in_rule() -> None:
    """The object is exactly as capable of a typo as a hand edit, so it gets
    the same union-only treatment `_write_stignore` gets."""
    merged = sd.ruleset_from_spec(
        "skcapstone-sync", {"requiredIgnores": [], "recommendedIgnores": []}
    )
    assert merged.required == sd.DEFAULT_RULESETS["skcapstone-sync"].required
    assert merged.recommended == sd.DEFAULT_RULESETS["skcapstone-sync"].recommended


def test_the_ruleset_is_keyed_by_folder_not_by_role() -> None:
    """Two roles joining one folder must resolve to identical rules, or the
    no-secrets invariant becomes per-node."""
    assert set(sd.DEFAULT_RULESETS) == {"skcapstone-sync"}
    assert sd.DEFAULT_RULESETS["skcapstone-sync"].folder_id == "skcapstone-sync"
    for role_spec in ({"role": "control"}, {"role": "worker"}):
        # A role key is simply not part of the resolution path at all.
        assert sd.ruleset_from_spec("skcapstone-sync", role_spec).required == (
            sd.DEFAULT_RULESETS["skcapstone-sync"].required
        )


def test_shipped_folder_object_parses_and_weakens_nothing() -> None:
    import json

    root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (root / "deploy/fleet-objects/syncfolder/skcapstone-sync.json").read_text(encoding="utf-8")
    )
    assert payload["name"] == "skcapstone-sync"
    merged = sd.ruleset_from_spec(payload["name"], payload["spec"])
    for rule in sd.DEFAULT_RULESETS["skcapstone-sync"].required:
        assert rule in merged.required
