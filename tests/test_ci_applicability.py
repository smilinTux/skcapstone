"""Offline repository policy binding and immutable completion receipts."""

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.ci_applicability import bind_ci_profile, validate_profile_completion

REPOSITORY = "https://example.test/team/project.git"
LEGACY = (
    "ci_check_docs",
    "ci_check_gitleaks",
    "ci_check_lint",
    "ci_check_shim_imports",
    "ci_check_python311",
    "ci_check_python312",
)


def manifest(kind="node"):
    """Build a repository-wide language policy."""
    checks = {key: {"expected": "SUCCESS", "reason": ""} for key in LEGACY}
    if kind == "node":
        for key in LEGACY[3:]:
            checks[key] = {
                "expected": "NOT_APPLICABLE",
                "reason": "Node repository has no Python package or shims.",
            }
    if kind != "python":
        checks["ci_check_node_test"] = {"expected": "SUCCESS", "reason": ""}
    return {"schema_version": 1, "repository": REPOSITORY, "checks": checks}


def git(repo, *args):
    """Run local fixture Git commands with no network."""
    return (
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def repository_fixture(tmp_path, data=None, kind="node"):
    """Commit a policy and an unchanged-policy descendant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.test")
    git(repo, "remote", "add", "origin", REPOSITORY)
    policy = repo / ".skcapstone" / "ci-profile.json"
    policy.parent.mkdir()
    data = json.dumps(manifest(kind)).encode() if data is None else data
    policy.write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-qm", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    meta = {"repository": REPOSITORY, "base_ref": "main", "base_revision": base}
    request = {
        "repository_path": str(repo),
        "candidate_revision": candidate,
        "profile_sha256": hashlib.sha256(data).hexdigest(),
    }
    return repo, policy, meta, request


@pytest.mark.parametrize("kind", ["node", "python", "mixed"])
def test_bind_exact_git_objects(tmp_path, kind):
    """All repository language combinations retain exact bytes and pins."""
    repo, policy, meta, request = repository_fixture(tmp_path, kind=kind)
    capsule = bind_ci_profile(meta, request)
    assert set(capsule) == {
        "schema_version",
        "repository",
        "base_revision",
        "candidate_revision",
        "profile_sha256",
        "manifest_text",
    }
    assert capsule["manifest_text"].encode() == policy.read_bytes()
    assert capsule["candidate_revision"] == request["candidate_revision"]
    assert capsule["profile_sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    policy.write_text("mutable checkout is not trusted")
    assert bind_ci_profile(meta, request) == capsule


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("ambient_override", [False, True])
def test_binding_ignores_mutable_graft_ancestry(tmp_path, monkeypatch, orphan, ambient_override):
    """Only immutable parent headers determine ancestry, not local graft files."""
    repo, _, meta, request = repository_fixture(tmp_path)
    if orphan:
        git(repo, "checkout", "--orphan", "unrelated")
        git(repo, "commit", "-qm", "unrelated root")
        request["candidate_revision"] = git(repo, "rev-parse", "HEAD")
    candidate = request["candidate_revision"]
    header = git(repo, "cat-file", "commit", candidate).split("\n\n", 1)[0]
    assert any(line.startswith("parent ") for line in header.splitlines()) is not orphan
    if orphan:
        with pytest.raises(ValueError):
            bind_ci_profile(meta, request)
    else:
        original = bind_ci_profile(meta, request)

    # Forge a parent for the orphan, or remove a real descendant's parent.
    graft = candidate + (" " + meta["base_revision"] if orphan else "") + "\n"
    (repo / ".git/info/grafts").write_text(graft)
    if ambient_override:
        ambient = tmp_path / "ambient-grafts"
        ambient.write_text(graft)
        monkeypatch.setenv("GIT_GRAFT_FILE", str(ambient))
    else:
        monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    with patch("skcapstone.ci_applicability.subprocess.run", wraps=subprocess.run) as run:
        if orphan:
            with pytest.raises(ValueError):
                bind_ci_profile(meta, request)
        else:
            assert bind_ci_profile(meta, request) == original
    assert run.call_count
    for call in run.call_args_list:
        assert call.kwargs["env"]["GIT_GRAFT_FILE"] == os.devnull
        assert call.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert call.kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert call.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""


@pytest.mark.parametrize(
    "case",
    [
        "invalid_json",
        "invalid_utf8",
        "duplicate",
        "unknown_field",
        "version",
        "bool_version",
        "float_version",
        "wrong_repository",
        "credentials",
        "empty_checks",
        "all_na",
        "missing_legacy",
        "missing_reason",
        "unknown_state",
        "bad_key",
        "extra_check_field",
        "success_reason",
        "empty_repository",
        "oversized",
    ],
)
def test_invalid_manifest_rejected(tmp_path, case):
    """Reject each malformed repository policy independently."""
    policy = manifest()
    if case == "unknown_field":
        policy["extra"] = True
    if case == "version":
        policy["schema_version"] = 2
    if case == "bool_version":
        policy["schema_version"] = True
    if case == "float_version":
        policy["schema_version"] = 1.0
    if case == "wrong_repository":
        policy["repository"] = REPOSITORY + "other"
    if case == "credentials":
        policy["repository"] = "https://user:secret@example.test/project"
    if case == "empty_repository":
        policy["repository"] = ""
    if case == "empty_checks":
        policy["checks"] = {}
    if case == "all_na":
        policy["checks"] = {
            key: {"expected": "NOT_APPLICABLE", "reason": "No relevant source."} for key in LEGACY
        }
    if case == "missing_legacy":
        del policy["checks"][LEGACY[0]]
    if case == "missing_reason":
        policy["checks"][LEGACY[3]]["reason"] = "  "
    if case == "unknown_state":
        policy["checks"][LEGACY[0]]["expected"] = "SKIPPED"
    if case == "bad_key":
        policy["checks"]["ci_check_X"] = {"expected": "SUCCESS", "reason": ""}
    if case == "extra_check_field":
        policy["checks"][LEGACY[0]]["override"] = True
    if case == "success_reason":
        policy["checks"][LEGACY[0]]["reason"] = "optional"
    data = json.dumps(policy).encode()
    if case == "invalid_json":
        data = b"{"
    if case == "invalid_utf8":
        data = b"\xff"
    if case == "duplicate":
        data = data.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    if case == "oversized":
        data = b" " * 65537
    _, _, meta, request = repository_fixture(tmp_path, data)
    with pytest.raises(ValueError):
        bind_ci_profile(meta, request)


@pytest.mark.parametrize(
    "case",
    [
        "missing_manifest",
        "symlink",
        "changed",
        "origin",
        "multiple_origins",
        "subdirectory",
        "relative_path",
        "missing_path",
        "short_candidate",
        "unknown_candidate",
        "short_base",
        "unknown_base",
        "nonancestor",
        "digest",
        "missing_binding",
        "review_head",
        "extra_request",
        "blob_candidate",
        "tag_candidate",
    ],
)
def test_invalid_binding_rejected(tmp_path, case):
    """Reject unverifiable or inconsistent immutable source bindings."""
    repo, policy, meta, request = repository_fixture(tmp_path)
    if case in {"missing_manifest", "symlink", "changed"}:
        policy.unlink()
        if case == "symlink":
            policy.symlink_to("../target")
        if case == "changed":
            policy.write_text(json.dumps(manifest("mixed")))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "changed policy")
        request["candidate_revision"] = git(repo, "rev-parse", "HEAD")
    if case == "origin":
        git(repo, "remote", "set-url", "origin", "https://example.test/other")
    if case == "multiple_origins":
        git(repo, "config", "--add", "remote.origin.url", REPOSITORY)
    if case == "subdirectory":
        request["repository_path"] = str(policy.parent)
    if case == "relative_path":
        request["repository_path"] = "."
    if case == "missing_path":
        request["repository_path"] = str(repo / "absent")
    if case == "short_candidate":
        request["candidate_revision"] = request["candidate_revision"][:8]
    if case == "unknown_candidate":
        request["candidate_revision"] = "f" * 40
    if case == "short_base":
        meta["base_revision"] = meta["base_revision"][:8]
    if case == "unknown_base":
        meta["base_revision"] = "e" * 40
    if case == "nonancestor":
        meta["base_revision"], request["candidate_revision"] = (
            request["candidate_revision"],
            meta["base_revision"],
        )
    if case == "digest":
        request["profile_sha256"] = "f" * 64
    if case == "missing_binding":
        del meta["base_ref"]
    if case == "review_head":
        meta["link_head_revision"] = "a" * 40
    if case == "extra_request":
        request["checks"] = {}
    if case == "blob_candidate":
        request["candidate_revision"] = git(repo, "rev-parse", "HEAD:.skcapstone/ci-profile.json")
    if case == "tag_candidate":
        git(repo, "tag", "-am", "tag", "annotated")
        request["candidate_revision"] = git(repo, "rev-parse", "annotated")
    with pytest.raises(ValueError):
        bind_ci_profile(meta, request)


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("git", 10),
        OSError("read failed"),
        subprocess.CalledProcessError(1, "git"),
    ],
)
def test_git_failures_are_value_errors(tmp_path, error):
    """OS, command and timeout failures are safely normalized."""
    _, _, meta, request = repository_fixture(tmp_path)
    with patch("skcapstone.ci_applicability.subprocess.run", side_effect=error):
        with pytest.raises(ValueError):
            bind_ci_profile(meta, request)


def completion_fixture(tmp_path, kind="node", card_id="bbbbbbbb"):
    """Create isolated capsule and attributed whole-receipt evidence."""
    policy = manifest(kind)
    data = json.dumps(policy)
    digest = hashlib.sha256(data.encode()).hexdigest()
    capsule = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "base_revision": "a" * 40,
        "candidate_revision": "b" * 40,
        "profile_sha256": digest,
        "manifest_text": data,
    }
    core = {
        "meta": {
            "repository": REPOSITORY,
            "base_revision": "a" * 40,
            "link_head_revision": "b" * 40,
            "ci_profile": capsule,
        }
    }
    receipt = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "candidate_revision": "b" * 40,
        "profile_sha256": digest,
        "checks": {
            key: {
                "state": value["expected"],
                "reason": value["reason"],
                "evidence": (
                    "sha256:" + digest
                    if value["expected"] == "NOT_APPLICABLE"
                    else "https://ci.example.test/runs/123/jobs/456"
                ),
            }
            for key, value in policy["checks"].items()
        },
    }
    store = CardStore(tmp_path)
    if not (tmp_path / "cards" / card_id / "core.json").exists():
        store.create(CardCore(id=card_id, title="synthetic profile receipt"))

    def append(receipt_value=None, ts="2026-09-11T10:00:00Z", **fields):
        """Append a synthetic receipt under the production evidence layout."""
        row = {
            "link_key": "ci_applicability",
            "link_value": json.dumps(receipt if receipt_value is None else receipt_value),
            "ts": ts,
        }
        row.update(fields)
        store.append_event(card_id, "link", row.pop("writer", "tester"), **row)

    return core, receipt, append


@pytest.mark.parametrize("kind", ["node", "python", "mixed"])
def test_profile_completion_accepts_exact_policy(tmp_path, kind):
    """Accept each exact profile without requiring obsolete legacy links."""
    core, _, append = completion_fixture(tmp_path, kind)
    append()
    validate_profile_completion("bbbbbbbb", tmp_path, core)


@pytest.mark.parametrize(
    "state",
    [
        "SKIPPED",
        "PENDING",
        "FAILURE",
        "CANCELLED",
        "UNKNOWN",
        "success",
        "SUCCESS extra",
        "",
        "NOT_APPLICABLE",
    ],
)
def test_required_check_rejects_non_success(tmp_path, state):
    """Required checks accept only literal SUCCESS."""
    core, receipt, append = completion_fixture(tmp_path)
    receipt["checks"][LEGACY[0]]["state"] = state
    append()
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


@pytest.mark.parametrize(
    "case",
    [
        "na_success",
        "na_reason",
        "na_reference",
        "missing_check",
        "extra_check",
        "missing_evidence",
        "mutable_evidence",
        "malformed",
        "repository",
        "head",
        "digest",
        "extra_field",
        "bool_version",
        "newer_failure",
        "conflict",
        "missing_ts",
        "naive_ts",
        "malformed_row",
        "unreadable",
        "null_capsule",
        "tampered_capsule",
        "capsule_extra",
        "capsule_base",
        "meta_head",
        "meta_repository",
        "missing_receipt",
        "string_success",
        "bad_meta",
        "capsule_schema",
        "missing_capsule_field",
        "typed_conflict",
    ],
)
def test_profile_completion_fail_closed(tmp_path, case):
    """Tampering, ambiguity, stale pins and malformed evidence never revive green."""
    core, receipt, append = completion_fixture(tmp_path)
    if case == "typed_conflict":
        append()
        receipt["schema_version"] = True
    if case in {"newer_failure", "conflict"}:
        append()
    if case == "na_success":
        receipt["checks"][LEGACY[3]]["state"] = "SUCCESS"
    if case == "na_reason":
        receipt["checks"][LEGACY[3]]["reason"] = "different"
    if case == "na_reference":
        receipt["checks"][LEGACY[3]]["evidence"] = "sha256:" + "d" * 64
    if case == "missing_check":
        del receipt["checks"][LEGACY[0]]
    if case == "extra_check":
        receipt["checks"]["ci_check_extra"] = receipt["checks"][LEGACY[0]]
    if case == "missing_evidence":
        receipt["checks"][LEGACY[0]]["evidence"] = " "
    if case == "mutable_evidence":
        receipt["checks"][LEGACY[0]]["evidence"] = "/tmp/latest.txt"
    if case == "repository":
        receipt["repository"] = REPOSITORY + "other"
    if case == "head":
        receipt["candidate_revision"] = "c" * 40
    if case == "digest":
        receipt["profile_sha256"] = "d" * 64
    if case == "extra_field":
        receipt["extra"] = True
    if case == "bool_version":
        receipt["schema_version"] = True
    if case in {"newer_failure", "conflict"}:
        receipt["checks"][LEGACY[0]]["state"] = "FAILURE"
    if case == "null_capsule":
        core["meta"]["ci_profile"] = None
    if case == "tampered_capsule":
        core["meta"]["ci_profile"]["manifest_text"] += " "
    if case == "capsule_extra":
        core["meta"]["ci_profile"]["extra"] = True
    if case == "capsule_base":
        core["meta"]["ci_profile"]["base_revision"] = "c" * 40
    if case == "meta_head":
        core["meta"]["link_head_revision"] = "c" * 40
    if case == "meta_repository":
        core["meta"]["repository"] = REPOSITORY + "other"
    if case == "capsule_schema":
        core["meta"]["ci_profile"]["schema_version"] = True
    if case == "missing_capsule_field":
        del core["meta"]["ci_profile"]["base_revision"]
    if case == "bad_meta":
        core["meta"] = []
    if case == "missing_receipt":
        with pytest.raises(ValueError):
            validate_profile_completion("bbbbbbbb", tmp_path, core)
        return
    fields = {}
    if case == "malformed":
        fields["link_value"] = "{"
    if case == "string_success":
        fields["link_value"] = "SUCCESS"
    stamp = "2026-09-11T10:01:00Z" if case == "newer_failure" else "2026-09-11T10:00:00Z"
    if case == "missing_ts":
        stamp = None
    if case == "naive_ts":
        stamp = "2026-09-11T10:00:00"
    append(ts=stamp, **fields)
    if case == "malformed_row":
        path = next((tmp_path / "cards/bbbbbbbb/events").glob("*.jsonl"))
        with path.open("a") as handle:
            handle.write('{"card_id":"bbbbbbbb","link_key":"ci_applicability",\n')
    if case == "unreadable":
        with patch("os.open", side_effect=OSError("unreadable")):
            with pytest.raises(ValueError):
                validate_profile_completion("bbbbbbbb", tmp_path, core)
        return
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_latest_instant_and_identical_duplicates(tmp_path):
    """Use timezone-aware instants and accept identical repeated receipts."""
    core, receipt, append = completion_fixture(tmp_path)
    bad = copy.deepcopy(receipt)
    bad["checks"][LEGACY[0]]["state"] = "FAILURE"
    append(bad, ts="2026-09-11T11:00:00+02:00")
    append(ts="2026-09-11T10:00:00Z")
    append(ts="2026-09-11T05:00:00-05:00")
    validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_normalized_evidence_aliases(tmp_path):
    """Retain normalized key/value and raw_key evidence aliases."""
    core, receipt, append = completion_fixture(tmp_path)
    append(link_key=None, link_value=None, raw_key="ci_applicability", value=json.dumps(receipt))
    validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_git_calls_are_offline_bounded_and_ignore_ambient_selection(tmp_path, monkeypatch):
    """Repository selection cannot be replaced through ambient Git variables."""
    _, _, meta, request = repository_fixture(tmp_path)
    monkeypatch.setenv("GIT_DIR", "/nonexistent/ambient/git")
    with patch("skcapstone.ci_applicability.subprocess.run", wraps=subprocess.run) as run:
        bind_ci_profile(meta, request)
    for call in run.call_args_list:
        assert isinstance(call.args[0], list)
        assert call.kwargs["timeout"] == 10 and not call.kwargs.get("shell", False)
        assert "GIT_DIR" not in call.kwargs["env"]
        assert not {"fetch", "pull", "push", "clone"}.intersection(call.args[0])


def enrollment_fixture(tmp_path):
    """Create an approved-base repository before its first policy addition."""
    repo, policy, meta, request = repository_fixture(tmp_path)
    data = policy.read_bytes()
    git(repo, "rm", ".skcapstone/ci-profile.json")
    (repo / "source.txt").write_text("unchanged application source\n")
    git(repo, "add", "source.txt")
    git(repo, "commit", "-qm", "pre-enrollment base")
    meta["base_revision"] = git(repo, "rev-parse", "HEAD")
    policy.parent.mkdir(exist_ok=True)
    policy.write_bytes(data)
    git(repo, "add", ".skcapstone/ci-profile.json")
    git(repo, "commit", "-qm", "initial policy only")
    request["candidate_revision"] = git(repo, "rev-parse", "HEAD")
    return repo, policy, meta, request


@pytest.mark.parametrize(
    "case", ["valid", "unregistered", "digest", "code", "delete", "rename", "symlink", "submodule"]
)
def test_initial_enrollment_requires_registered_manifest_only(tmp_path, monkeypatch, case):
    """Initial policy cannot self-authorize a digest or unrelated source changes."""
    from skcapstone import ci_applicability

    repo, policy, meta, request = enrollment_fixture(tmp_path)
    allowed = {REPOSITORY.removesuffix(".git"): request["profile_sha256"]}
    if case == "unregistered":
        allowed = {}
    if case == "digest":
        allowed[REPOSITORY.removesuffix(".git")] = "e" * 64
    monkeypatch.setattr(ci_applicability, "INITIAL_PROFILE_DIGESTS", allowed, raising=False)
    if case == "code":
        (repo / "source.txt").write_text("changed source\n")
    if case == "delete":
        (repo / "source.txt").unlink()
    if case == "rename":
        git(repo, "mv", "source.txt", "renamed.txt")
    if case == "symlink":
        policy.unlink()
        policy.symlink_to("../source.txt")
    if case == "submodule":
        git(repo, "config", "diff.ignoreSubmodules", "all")
        git(
            repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000," + meta["base_revision"] + ",vendor",
        )
    if case in {"code", "delete", "rename", "symlink", "submodule"}:
        if case != "submodule":
            git(repo, "add", "-A")
        git(repo, "commit", "-qm", "invalid enrollment")
        request["candidate_revision"] = git(repo, "rev-parse", "HEAD")
    if case == "valid":
        capsule = bind_ci_profile(meta, request)
        assert capsule["manifest_text"].encode() == policy.read_bytes()
    else:
        with pytest.raises(ValueError):
            bind_ci_profile(meta, request)


def test_exact_card_receipts_ignore_global_and_unrelated_corruption(tmp_path):
    """Unrelated/global evidence is not authority and cannot block this card."""
    core, _, append = completion_fixture(tmp_path)
    append()
    other = tmp_path / "cards/other/events"
    other.mkdir(parents=True)
    (other / "broken.jsonl").write_bytes(b"\xff{")
    global_events = tmp_path / "coordination/card_events"
    global_events.mkdir(parents=True)
    (global_events / "broken.jsonl").write_bytes(b"\xff{")
    validate_profile_completion("bbbbbbbb", tmp_path, core)


@pytest.mark.parametrize(
    "case",
    [
        "malformed",
        "escaped_card",
        "escaped_key",
        "duplicate_receipt",
        "chain",
        "sync_conflict",
        "symlink",
        "hardlink",
        "fifo",
    ],
)
def test_exact_card_receipts_reject_unsafe_newer_evidence(tmp_path, case):
    """No malformed or unsafe exact-card evidence can revive older green."""
    core, receipt, append = completion_fixture(tmp_path)
    append()
    overlay = tmp_path / "coordination/card_events"
    overlay.mkdir(parents=True)
    (overlay / "old.jsonl").write_text(
        json.dumps(
            {
                "card_id": "bbbbbbbb",
                "action": "link",
                "link_key": "ci_applicability",
                "link_value": json.dumps(receipt),
                "ts": "2026-09-11T10:00:00Z",
            }
        )
        + "\n"
    )
    events = tmp_path / "cards/bbbbbbbb/events"
    path = events / "new-writer.jsonl"
    receipt["checks"][LEGACY[0]]["state"] = "FAILURE"
    row = {
        "card_id": "bbbbbbbb",
        "action": "link",
        "link_key": "ci_applicability",
        "link_value": json.dumps(receipt),
        "ts": "2026-09-11T11:00:00Z",
    }
    raw = json.dumps(row)
    if case == "malformed":
        raw = "{"
    if case in {"escaped_card", "escaped_key"}:
        raw = raw.replace('"action": "link"', '"action": "link", "action": "label"')
        raw = (
            raw.replace('"bbbbbbbb"', '"\\u0062bbbbbbb"')
            if case == "escaped_card"
            else raw.replace('"ci_applicability"', '"ci_\\u0061pplicability"')
        )
    if case == "duplicate_receipt":
        receipt["checks"][LEGACY[0]]["state"] = "SUCCESS"
        row["link_value"] = json.dumps(receipt).replace(
            '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
        )
        raw = json.dumps(row)
    if case == "chain":
        row["prev_hash"] = "f" * 64
        raw = json.dumps(row)
    if case == "sync_conflict":
        path = events / "writer.sync-conflict-20260911"
        raw = ""
    if case == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif case == "hardlink":
        os.link(next(events.glob("*.jsonl")), path)
    elif case == "fifo":
        os.mkfifo(path)
    else:
        path.write_text(raw + "\n")
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_cross_writer_receipts_conflict_at_same_instant(tmp_path):
    core, receipt, append = completion_fixture(tmp_path)
    append(writer="first")
    receipt["checks"][LEGACY[0]]["state"] = "FAILURE"
    append(writer="second")
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


@pytest.mark.parametrize("limit", ["_EVENT_BYTES", "_EVENT_ROWS", "_EVENT_FILES"])
def test_exact_card_evidence_limits_fail_closed(tmp_path, monkeypatch, limit):
    from skcapstone import ci_applicability

    core, _, append = completion_fixture(tmp_path)
    append()
    monkeypatch.setattr(ci_applicability, limit, 0)
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


@pytest.mark.parametrize(
    "identifier", ["../bbbbbbbb", "bbbbbbbb/events", "/absolute", "", ".", "a" * 129]
)
def test_exact_card_identifier_cannot_escape_home(tmp_path, identifier):
    from skcapstone.ci_applicability import _card_event_rows

    with pytest.raises(ValueError):
        _card_event_rows(identifier, tmp_path)


@pytest.mark.parametrize("segment", ["cards", "bbbbbbbb", "events"])
def test_exact_card_evidence_rejects_symlink_directories(tmp_path, segment):
    core, _, append = completion_fixture(tmp_path)
    append()
    path = tmp_path / "cards"
    if segment in {"bbbbbbbb", "events"}:
        path /= "bbbbbbbb"
    if segment == "events":
        path /= "events"
    target = tmp_path / ("moved-" + segment)
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_global_receipt_cannot_enroll_or_replace_exact_card_evidence(tmp_path):
    core, receipt, _ = completion_fixture(tmp_path)
    events = tmp_path / "coordination/card_events"
    events.mkdir(parents=True)
    (events / "old.jsonl").write_text(
        json.dumps(
            {
                "card_id": "bbbbbbbb",
                "action": "link",
                "link_key": "ci_applicability",
                "link_value": json.dumps(receipt),
                "ts": "2026-09-11T10:00:00Z",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", tmp_path, core)


def test_authorized_gateway_registry_digest_is_exact_and_immutable():
    from skcapstone.ci_profile_registry import INITIAL_PROFILE_DIGESTS

    policy = {
        "schema_version": 1,
        "repository": "https://github.com/smilinTux/skgateway",
        "checks": {
            "ci_check_docs": {"expected": "SUCCESS", "reason": ""},
            "ci_check_gitleaks": {"expected": "SUCCESS", "reason": ""},
            "ci_check_lint": {
                "expected": "NOT_APPLICABLE",
                "reason": "SKGateway has no separate lint command; its Node test suite is the code-quality gate.",
            },
            "ci_check_shim_imports": {
                "expected": "NOT_APPLICABLE",
                "reason": "SKGateway is a Node.js repository and has no Python shim import boundary.",
            },
            "ci_check_python311": {
                "expected": "NOT_APPLICABLE",
                "reason": "SKGateway does not support or execute Python 3.11.",
            },
            "ci_check_python312": {
                "expected": "NOT_APPLICABLE",
                "reason": "SKGateway does not support or execute Python 3.12.",
            },
            "ci_check_node22": {"expected": "SUCCESS", "reason": ""},
        },
    }
    data = (json.dumps(policy, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "f48fc610962a8da11d1d673665ac4d369dc6f28d43c745c9cc52f6b122bdb24b"
    assert INITIAL_PROFILE_DIGESTS[policy["repository"]] == digest
    with pytest.raises(TypeError):
        INITIAL_PROFILE_DIGESTS[policy["repository"]] = "f" * 64
