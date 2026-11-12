"""Installed mail commands and old-path compatibility contracts."""

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installed_entry_point_and_shim_have_identical_behavior(skmail_installation, tmp_path):
    """Compare exact output, status and mailbox bytes at the same fixture path."""
    coord = tmp_path / "coord"
    record = {
        "ts": "2026-10-04T00:00:00+00:00",
        "from": "alice",
        "to": "fleet",
        "priority": "normal",
        "re": "quoted ' subject",
        "body": "line one\nline two",
        "host": "test-host",
    }
    env = dict(os.environ, SKMAIL_DIR=str(coord))

    def exercise(command):
        shutil.rmtree(coord, ignore_errors=True)
        box = coord / "skmail.d/alice@test-host.jsonl"
        box.parent.mkdir(parents=True)
        box.write_text(json.dumps(record) + "\n")
        results = []
        for args in (
            ["ack", "worker"],
            ["read", "worker", "--summary"],
            ["read", "worker", "--urgent"],
            ["read", "worker"],
            ["ack", "worker"],
            ["read", "worker"],
            ["tail", "10"],
            ["unknown"],
            ["send", "alice", "worker", "invalid", "subject", "body"],
            ["--version"],
        ):
            result = subprocess.run([str(command), *args], env=env, capture_output=True)
            state = {
                str(p.relative_to(coord)): p.read_bytes() for p in coord.rglob("*") if p.is_file()
            }
            results.append((result.returncode, result.stdout, result.stderr, state))
        return results

    assert exercise(skmail_installation["bin"] / "skmail") == exercise(
        ROOT / "scripts/fleet/skmail"
    )


def test_installed_version_comes_from_distribution(skmail_installation, tmp_path):
    """A version query identifies the installed wheel without creating a mailbox."""
    env = dict(os.environ, SKMAIL_DIR=str(tmp_path / "unused"))
    result = subprocess.run(
        [str(skmail_installation["bin"] / "skmail"), "--version"],
        env=env,
        capture_output=True,
        check=True,
    )
    assert result.stdout == b"skmail 0.0.0+skmailtest\n"
    assert result.stderr == b""
    assert not (tmp_path / "unused").exists()


def test_installed_send_and_writer_compatibility(skmail_installation, tmp_path):
    """Installed mail and both writer names preserve argv and mailbox location."""
    coord = tmp_path / "coord with spaces"
    env = dict(os.environ, SKMAIL_DIR=str(coord))
    body = "spaces ' quotes \" $HOME\nsecond line"
    result = subprocess.run(
        [
            str(skmail_installation["bin"] / "skmail"),
            "send",
            "alice",
            "worker",
            "normal",
            "subject with spaces",
            body,
        ],
        env=env,
        capture_output=True,
        check=True,
    )
    assert result.stdout == b"sent to worker [normal] re subject with spaces\n"
    record = json.loads(next((coord / "skmail.d").glob("alice@*.jsonl")).read_text())
    assert record["body"] == body
    for name in ("skmail_writer", "skmail_writer.py"):
        subprocess.run(
            [
                str(skmail_installation["bin"] / name),
                "writer",
                "worker",
                "fyi",
                "subject",
                body,
                "--boxdir",
                str(tmp_path / name),
                "--host",
                "test-host",
            ],
            env=env,
            capture_output=True,
            check=True,
        )
        saved = json.loads((tmp_path / name / "writer@test-host.jsonl").read_text())
        assert saved["body"] == body
