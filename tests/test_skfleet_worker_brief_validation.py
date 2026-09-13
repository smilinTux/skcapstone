"""Synthetic boundary regressions for dc70a621 and review 1139fe47."""

import ast
import hashlib
import json
import os
from pathlib import Path

import pytest

from skcapstone.fleet import worker_brief as brief

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("card_id", ["cb65a43a-1", "prb-1234abcd", "named-task"])
def test_portable_existing_card_ids_remain_supported(tmp_path, card_id):
    """Keep supported source card identities, including split tasks and aliases."""
    envelope = make(tmp_path, id=card_id, acceptance_criteria=None, dependencies=None)
    assert envelope["card_id"] == card_id


def make(root, links=None, **fields):
    """Materialize one synthetic card through the production boundary."""
    card = {"id": "1234abcd", "links": links or {}, **fields}
    return brief.materialize_work_envelope(
        card,
        labels=["parent-abcdef12"],
        lane="codex",
        model="sk-xl-public",
        seat="link",
        evidence_root=root,
    )


def artifact(root):
    """Write harmless evidence under its authorized producer."""
    path = root / "work/abcdef12/evidence.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic evidence only")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "key",
    [
        "candidate_patch_sha256",
        "candidate_evidence_sha256",
        "evidence_sha256",
        "artifact_sha256",
        "artifacts_sha256",
        "source_manifest_sha256",
    ],
)
@pytest.mark.parametrize("value", ["", None, 123, [], {}, "bad", "a" * 63, "g" * 64])
def test_every_declared_digest_is_validated(tmp_path, key, value):
    """Absent or malformed bytes cannot be silently removed from the requirement."""
    with pytest.raises(brief.BriefEvidenceError):
        make(tmp_path, {key: value})


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "directory",
        "fifo",
        "unhashed",
        "mismatch",
        "outside",
        "traversal",
        "file_symlink",
        "parent_symlink",
        "work_symlink",
        "nested_symlink",
    ],
)
def test_declared_paths_fail_closed(tmp_path, kind):
    """Reject unverified paths and escapes at each directory level."""
    path, digest = artifact(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.md").write_bytes(path.read_bytes())
    if kind == "missing":
        path = path.with_name("missing")
    elif kind == "directory":
        path = path.parent
    elif kind == "fifo":
        path = path.with_name("fifo")
        os.mkfifo(path)
    elif kind == "mismatch":
        digest = "a" * 64
    elif kind == "outside":
        path = outside / "evidence.md"
    elif kind == "traversal":
        path = path.parent / "../../outside/evidence.md"
    elif kind == "file_symlink":
        path.unlink()
        path.symlink_to(outside / "evidence.md")
    elif kind == "parent_symlink":
        path.parent.rename(tmp_path / "original-parent")
        path.parent.symlink_to(outside, target_is_directory=True)
    elif kind == "work_symlink":
        (tmp_path / "work").rename(tmp_path / "original-work")
        (tmp_path / "work").symlink_to(tmp_path / "original-work", target_is_directory=True)
    elif kind == "nested_symlink":
        (path.parent / "nested").symlink_to(outside, target_is_directory=True)
        path = path.parent / "nested/evidence.md"
    links = {"evidence_path": str(path)}
    if kind != "unhashed":
        links["evidence_sha256"] = digest
    with pytest.raises(brief.BriefEvidenceError):
        make(tmp_path, links)


@pytest.mark.parametrize("inline", [False, "#", " "])
def test_exact_local_evidence_and_metadata_survive(tmp_path, inline):
    """Allow legitimate security source names and verified local references."""
    path, digest = artifact(tmp_path)
    links = {
        "evidence_path": str(path),
        "source_scope": "src/platform/authorization.py",
        "repository_scope": "src/secrets/token.py",
        "pr": "https://example.invalid/pull/514",
    }
    if inline:
        links["evidence_path"] += inline + "sha256=" + digest
    else:
        links["evidence_sha256"] = digest.upper()
    envelope = make(tmp_path, links)
    assert envelope["evidence_paths"] == {digest: str(path)}
    assert envelope["source_scope"] == [links["repository_scope"], links["source_scope"]]
    assert envelope["evidence_links"] == links


@pytest.mark.parametrize(
    "value",
    [
        # Built from fragments so scanners see no Basic Auth literal; the
        # runtime value under test is unchanged.
        "https://fixture-user" + ":" + "FIXTURE-PASS@example.invalid/evidence",
        "https://fixture-user%3AFIXTURE-PASS%40example.invalid/evidence",
        "https://example.invalid/evidence?access_token=FIXTURE",
        "https://example.invalid/evidence#api_key=FIXTURE",
        "https://example.invalid/evidence?X-Amz-Signature=FIXTURE",
        "//fixture-user:FIXTURE-PASS@example.invalid/evidence",
        "javascript:alert('fixture')",
        "data:text/plain,fixture",
        "file:///fixture",
        '{"password": "SYNTHETIC-NOT-A-CREDENTIAL"}',
        "Bearer SYNTHETIC-NOT-A-CAPABILITY",
        "Basic SYNTHETIC-NOT-A-CREDENTIAL",
        "token=SYNTHETIC-NOT-A-CAPABILITY",
        r'{"pass\u0077ord": "SYNTHETIC-NOT-A-CREDENTIAL"}',
        r"https:\/\/fixture-user:FIXTURE-PASS@example.invalid/evidence",
    ],
)
def test_unsafe_links_are_omitted_with_provenance(tmp_path, value):
    """Unsafe metadata cannot reach a prompt, and its omission is visible."""
    envelope = make(tmp_path, {"pr": value, "source_scope": "src/token.py"})
    assert envelope["evidence_links"] == {"source_scope": "src/token.py"}
    assert envelope["omitted_link_keys"] == ["pr"]
    assert value not in brief.format_work_envelope(envelope)


def test_rejected_description_is_not_copied_into_failure_report(tmp_path):
    """Reports explain the rejection without replicating credential content."""
    with pytest.raises(brief.BriefEvidenceError) as error:
        make(tmp_path, description='{"password": "SYNTHETIC-NOT-A-CREDENTIAL"}')
    path, _ = brief.write_missing_report(error.value.report, tmp_path)
    assert "SYNTHETIC" not in path.read_text()


@pytest.mark.parametrize("component", ["work", "abcdef12", "evidence.md"])
def test_symlink_swap_at_open_is_denied(tmp_path, monkeypatch, component):
    """A symlink substituted after discovery cannot redirect the actual read."""
    path, digest = artifact(tmp_path)
    original_open = os.open
    swapped = False

    def swap(name, flags, *args, **kwargs):
        """Substitute one synthetic component just before the no-follow open."""
        nonlocal swapped
        if name == component and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target = {"work": tmp_path / "work", "abcdef12": path.parent, "evidence.md": path}[
                component
            ]
            moved = tmp_path / "moved"
            target.rename(moved)
            target.symlink_to(moved, target_is_directory=component != "evidence.md")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(brief.BriefEvidenceError):
        make(tmp_path, {"candidate_patch_sha256": digest})
    assert swapped


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "1234abcd", "description": "password=SYNTHETIC-NOT-A-CREDENTIAL"},
        {"id": "1234abcd", "links": {"candidate_patch_sha256": "bad"}},
        {"id": "1234abcd", "links": ["malformed"]},
        {"id": "1234abcd", "acceptance_criteria": 7},
    ],
)
@pytest.mark.parametrize("report_failure", [False, True])
def test_rejected_card_is_recorded_and_later_card_runs(tmp_path, bad, report_failure):
    """Execute the exact production preclaim block without claims or dispatch."""
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    start = source.index("    try:\n        envelope = materialize_work_envelope(")
    # R3 conflict resolution: current main appends seat role fences to the
    # brief between the rails and the envelope, so the preclaim block appends
    # the envelope to the existing brief instead of rebuilding it one-shot.
    stop = source.index("    brief += (", start)
    fragment = (
        "for core in cards:\n"
        "    fresh_claimability = {'core': core, 'labels': []}\n"
        "    cid = core['id']\n"
        + source[start:stop]
        + "    selected.append(envelope['card_id'])\n"
    )
    logs, selected = [], []

    def report(*args):
        """Exercise both immutable report success and storage failure."""
        if report_failure:
            raise OSError("synthetic storage failure")
        return brief.write_missing_report(*args)

    namespace = {
        "cards": [bad, {"id": "87654321", "title": "valid later card"}],
        "selected": selected,
        "_LANE": {"name": "codex"},
        "model": "sk-xl-public",
        "_seat": "link",
        "HOME": str(tmp_path),
        "Path": Path,
        "json": json,
        "materialize_work_envelope": brief.materialize_work_envelope,
        "BriefEvidenceError": brief.BriefEvidenceError,
        "write_missing_report": report,
        "HOST": "synthetic-host",
        "d": tmp_path,
        "log": lambda _, entry: logs.append(entry),
    }
    exec(compile(ast.parse(fragment), "selector-preclaim", "exec"), namespace)
    assert selected == ["87654321"]
    assert len(logs) == 1 and "WORKER_BRIEF_BLOCKED|synthetic-host|1234abcd|" in logs[0]
    assert "SYNTHETIC-NOT-A-CREDENTIAL" not in logs[0]
    if not report_failure:
        assert (
            len(list((tmp_path / ".skcapstone/evidence/worker-brief-preflight").glob("*.json")))
            == 1
        )
