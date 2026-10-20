"""Lossless quarantine of malformed legacy skmail records (plan, review, apply, restore).

Drives scripts/fleet/skmail_quarantine.py against the REAL skmail reader (the packaged
script), so the diagnostic format the tool parses is the one production emits.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TOOL = ROOT / "scripts" / "fleet" / "skmail_quarantine.py"
READER_SRC = ROOT / "src" / "skcapstone" / "fleet" / "skmail" / "skmail"

spec = importlib.util.spec_from_file_location("skmail_quarantine_under_test", TOOL)
quarantine = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(quarantine)

GOOD = json.dumps(
    {
        "ts": "2026-09-01T00:00:00+00:00",
        "from": "alice",
        "to": "bob",
        "priority": "normal",
        "re": "ok",
        "body": "fine",
        "host": "h1",
    }
)
BAD_SCHEMA = json.dumps(
    {"sender": "alice", "recipient": "bob", "timestamp": "x", "body": "legacy"}
)
BAD_JSON = "{this is not json"
OLD = time.time() - 86400 * 30


def _reader(tmp_path: Path) -> Path:
    wrapper = tmp_path / "reader.sh"
    wrapper.write_text(f'#!/usr/bin/env bash\nexec bash "{READER_SRC}" "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def _mailbox(tmp_path: Path, files: dict[str, list[str]]) -> Path:
    box = tmp_path / "coord" / "skmail.d"
    box.mkdir(parents=True)
    for name, rows in files.items():
        path = box / name
        path.write_text("".join(row + "\n" for row in rows))
        os.utime(path, (OLD, OLD))
    return box


def _plan_args(tmp_path: Path, box: Path, **overrides) -> argparse.Namespace:
    values = {
        "box": box,
        "archive": tmp_path / "archive",
        "reader": _reader(tmp_path),
        "legacy_before": datetime.now(timezone.utc) - timedelta(days=1),
        "planner": "planner-a",
        "card_id": "5c0a0006",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _review(tmp_path: Path, manifest: Path, **overrides) -> Path:
    raw = manifest.read_bytes()
    body = {
        "verdict": "PASS",
        "manifest_sha256": quarantine.digest(raw),
        "reviewer": "reviewer-b",
    }
    body.update(overrides)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(body))
    return path


def _apply_args(manifest: Path, review: Path) -> argparse.Namespace:
    return argparse.Namespace(manifest=manifest, review=review)


def _snapshot(box: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode), p.stat().st_mtime_ns)
        for p in box.iterdir()
        if p.is_file() and not p.name.startswith(".")
    }


def _planned(tmp_path: Path, files=None):
    files = files or {"alice@h1.jsonl": [GOOD, BAD_SCHEMA, GOOD, BAD_JSON]}
    box = _mailbox(tmp_path, files)
    archive = quarantine.plan(_plan_args(tmp_path, box))
    return box, archive, archive / "manifest.json"


def _reader_stderr(tmp_path: Path, box: Path) -> str:
    env = {**os.environ, "SKMAIL_DIR": str(box.parent)}
    done = subprocess.run(
        [str(_reader(tmp_path)), "tail", "1000"], env=env, capture_output=True, text=True
    )
    return done.stderr


def test_plan_apply_restore_round_trip_is_lossless(tmp_path: Path) -> None:
    box, archive, manifest = _planned(tmp_path)
    before = _snapshot(box)
    assert "INVALID_" in _reader_stderr(tmp_path, box)

    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))

    after = _snapshot(box)
    assert after["alice@h1.jsonl"][0] == (GOOD + "\n" + GOOD + "\n").encode()
    assert "INVALID_" not in _reader_stderr(tmp_path, box)
    assert after["alice@h1.jsonl"][1:] == before["alice@h1.jsonl"][1:], "mode and mtime kept"

    quarantine.restore(argparse.Namespace(manifest=manifest))
    assert _snapshot(box) == before


def test_whole_file_of_malformed_records_is_removed_and_restored(tmp_path: Path) -> None:
    box, _, manifest = _planned(
        tmp_path,
        {"alice@h1.jsonl": [GOOD], "legacy.jsonl": [BAD_JSON, BAD_SCHEMA]},
    )
    before = _snapshot(box)
    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))
    assert not (box / "legacy.jsonl").exists()
    quarantine.restore(argparse.Namespace(manifest=manifest))
    assert _snapshot(box) == before


def test_untouched_valid_mailboxes_are_never_rewritten(tmp_path: Path) -> None:
    box, _, manifest = _planned(tmp_path, {"alice@h1.jsonl": [GOOD], "mixed@h1.jsonl": [BAD_JSON]})
    untouched = (box / "alice@h1.jsonl").stat().st_ino
    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))
    assert (box / "alice@h1.jsonl").stat().st_ino == untouched


def test_plan_refuses_when_nothing_is_rejected(tmp_path: Path) -> None:
    box = _mailbox(tmp_path, {"alice@h1.jsonl": [GOOD]})
    with pytest.raises(SystemExit, match="rejected no envelopes"):
        quarantine.plan(_plan_args(tmp_path, box))


def test_plan_refuses_a_malformed_file_a_current_writer_may_still_be_producing(
    tmp_path: Path,
) -> None:
    box = _mailbox(tmp_path, {"alice@h1.jsonl": [GOOD, BAD_JSON]})
    fresh = time.time()
    os.utime(box / "alice@h1.jsonl", (fresh, fresh))
    with pytest.raises(SystemExit, match="current writer may be malformed"):
        quarantine.plan(_plan_args(tmp_path, box))


def test_plan_refuses_a_box_not_named_skmail_d(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(SystemExit, match="named skmail.d"):
        quarantine.plan(_plan_args(tmp_path, other))


@pytest.mark.parametrize(
    "overrides",
    [
        {"verdict": "pass"},
        {"verdict": True},
        {"verdict": "FAIL"},
        {"verdict": None},
        {"manifest_sha256": "0" * 64},
        {"manifest_sha256": None},
        {"reviewer": "planner-a"},
        {"reviewer": ""},
        {"reviewer": 7},
        {"reviewer": None},
    ],
)
def test_apply_rejects_bad_reviews_and_changes_nothing(tmp_path: Path, overrides) -> None:
    box, _, manifest = _planned(tmp_path)
    before = _snapshot(box)
    with pytest.raises(SystemExit):
        quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest, **overrides)))
    assert _snapshot(box) == before


@pytest.mark.parametrize("payload", ["[]", "null", '"PASS"', "7"])
def test_apply_rejects_non_object_review(tmp_path: Path, payload: str) -> None:
    box, _, manifest = _planned(tmp_path)
    review = tmp_path / "review.json"
    review.write_text(payload)
    with pytest.raises(SystemExit, match="review must be a JSON object"):
        quarantine.apply(_apply_args(manifest, review))


def test_review_of_another_plan_does_not_authorize_this_one(tmp_path: Path) -> None:
    box, _, manifest = _planned(tmp_path)
    other_tmp = tmp_path / "second"
    other_tmp.mkdir()
    _, _, other_manifest = _planned(other_tmp, {"alice@h1.jsonl": [GOOD, BAD_JSON, BAD_JSON]})
    foreign_review = _review(tmp_path, other_manifest)
    before = _snapshot(box)
    with pytest.raises(SystemExit, match="does not match manifest"):
        quarantine.apply(_apply_args(manifest, foreign_review))
    assert _snapshot(box) == before


def test_apply_refuses_when_mailbox_changed_after_review(tmp_path: Path) -> None:
    box, _, manifest = _planned(tmp_path)
    review = _review(tmp_path, manifest)
    with (box / "alice@h1.jsonl").open("a") as handle:
        handle.write(GOOD + "\n")
    before = _snapshot(box)
    with pytest.raises(SystemExit, match="drifted after review"):
        quarantine.apply(_apply_args(manifest, review))
    assert _snapshot(box) == before


def test_apply_refuses_before_touching_anything_if_archive_cannot_restore(
    tmp_path: Path,
) -> None:
    box, archive, manifest = _planned(tmp_path)
    review = _review(tmp_path, manifest)
    for blob in (archive / "blobs" / "sha256").iterdir():
        blob.write_bytes(b"corrupted")
    before = _snapshot(box)
    with pytest.raises(SystemExit, match="archive blob missing or corrupt"):
        quarantine.apply(_apply_args(manifest, review))
    assert _snapshot(box) == before


def test_apply_refuses_a_missing_blob(tmp_path: Path) -> None:
    box, archive, manifest = _planned(tmp_path)
    review = _review(tmp_path, manifest)
    next((archive / "blobs" / "sha256").iterdir()).unlink()
    with pytest.raises(SystemExit, match="archive blob missing or corrupt"):
        quarantine.apply(_apply_args(manifest, review))


def test_manifest_must_live_under_its_own_hash(tmp_path: Path) -> None:
    box, archive, manifest = _planned(tmp_path)
    review = _review(tmp_path, manifest)
    moved = archive.parent / ("0" * 64)
    archive.rename(moved)
    with pytest.raises(SystemExit, match="own sha256"):
        quarantine.apply(_apply_args(moved / "manifest.json", review))


def test_non_canonical_manifest_is_rejected(tmp_path: Path) -> None:
    _, _, manifest = _planned(tmp_path)
    manifest.write_text(json.dumps(json.loads(manifest.read_text()), indent=4))
    with pytest.raises(SystemExit, match="not canonical"):
        quarantine.load_manifest(manifest)


@pytest.mark.parametrize("bad", ["../escape.jsonl", "sub/x.jsonl", "..", "/etc/passwd"])
def test_manifest_source_outside_the_mailbox_is_refused(tmp_path: Path, bad: str) -> None:
    box = _mailbox(tmp_path, {"alice@h1.jsonl": [GOOD]})
    manifest = {"box": str(box), "sources": [{"path": str(box / bad)}]}
    if bad.startswith("/"):
        manifest["sources"] = [{"path": bad}]
    with pytest.raises(SystemExit, match="escapes the mailbox"):
        quarantine.checked_box(manifest)


def test_manifest_box_must_exist_and_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="existing absolute directory"):
        quarantine.checked_box({"box": "relative/skmail.d", "sources": []})
    with pytest.raises(SystemExit, match="existing absolute directory"):
        quarantine.checked_box({"box": str(tmp_path / "missing"), "sources": []})


def test_apply_waits_for_the_canonical_writers_lock(tmp_path: Path) -> None:
    """A concurrent skmail_writer holds <box>/.<name>.lock; apply must queue behind it."""
    box, _, manifest = _planned(tmp_path)
    review = _review(tmp_path, manifest)
    lock = (box / ".alice@h1.jsonl.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    runner = tmp_path / "run_apply.py"
    runner.write_text(
        "import argparse, importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('q', {str(TOOL)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m.apply(argparse.Namespace(manifest=__import__('pathlib').Path({str(manifest)!r}),"
        f" review=__import__('pathlib').Path({str(review)!r})))\n"
    )
    proc = subprocess.Popen([sys.executable, str(runner)], stderr=subprocess.PIPE)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=1.5)
        assert BAD_JSON.encode() in (box / "alice@h1.jsonl").read_bytes(), "not yet applied"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    assert proc.wait(timeout=30) == 0, proc.stderr.read()
    assert BAD_JSON.encode() not in (box / "alice@h1.jsonl").read_bytes()


def test_restore_keeps_mail_appended_after_apply(tmp_path: Path) -> None:
    box, _, manifest = _planned(tmp_path)
    original = (box / "alice@h1.jsonl").read_bytes()
    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))
    late = GOOD.replace("fine", "arrived later") + "\n"
    with (box / "alice@h1.jsonl").open("a") as handle:
        handle.write(late)
    quarantine.restore(argparse.Namespace(manifest=manifest))
    assert (box / "alice@h1.jsonl").read_bytes() == original + late.encode()


def test_restore_refuses_a_mailbox_someone_else_edited(tmp_path: Path) -> None:
    box, _, manifest = _planned(tmp_path)
    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))
    (box / "alice@h1.jsonl").write_text("totally different\n")
    with pytest.raises(SystemExit, match="changed since quarantine"):
        quarantine.restore(argparse.Namespace(manifest=manifest))
    assert (box / "alice@h1.jsonl").read_text() == "totally different\n"


def test_restore_recreates_a_removed_file_and_keeps_new_mail_in_its_place(
    tmp_path: Path,
) -> None:
    box, _, manifest = _planned(tmp_path, {"legacy.jsonl": [BAD_JSON, BAD_SCHEMA]})
    original = (box / "legacy.jsonl").read_bytes()
    quarantine.apply(_apply_args(manifest, _review(tmp_path, manifest)))
    (box / "legacy.jsonl").write_text(GOOD + "\n")
    quarantine.restore(argparse.Namespace(manifest=manifest))
    assert (box / "legacy.jsonl").read_bytes() == original + (GOOD + "\n").encode()
