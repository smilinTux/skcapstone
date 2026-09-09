"""Isolated subprocess proof for canonical Seraph review dispatch."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import skcoord.lifecycle_reassessment as lifecycle_reassessment
from skcoord.card_store import CardCore, CardStore

from skcapstone.link_review_work import card_generation, reconcile_review_work

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
GATEWAY_REVISION = "d" * 40


class _HealthyGateway(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        now = datetime.now(UTC)
        if self.path == "/health":
            body = {
                "status": "ok",
                "backends": {
                    "codex": {
                        "status": "up",
                        "observed": True,
                        "quarantined": False,
                        "lastCheck": int(now.timestamp() * 1000),
                    }
                },
            }
        elif self.path == "/queue":
            body = {
                "timestamp": now.isoformat(),
                "pool": {},
                "backends": {"codex": {"capacityDomain": "codex", "max": 3}},
            }
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _canonical_review(home: Path) -> tuple[CardStore, str]:
    card_home = home / ".skcapstone"
    card_home.mkdir()
    store = CardStore(card_home)
    store.create(
        CardCore(
            id="9c71f24a",
            title="Source",
            created_by="mero",
            initial_labels=["do-not-claim"],
        )
    )
    store.append_event(
        "9c71f24a",
        "link",
        "mero",
        link_key="repository",
        link_value="https://github.com/smilinTux/skcapstone",
    )
    store.append_event("9c71f24a", "link", "mero", link_key="base_ref", link_value="main")
    result = reconcile_review_work(
        card_home,
        {
            "kind": "review-work",
            "reason": "missing_terminal_review",
            "repository": "smilinTux/skcapstone",
            "workspace_repository": "https://github.com/smilinTux/skcapstone",
            "base_ref": "main",
            "pr": 548,
            "head_revision": "a" * 40,
            "base_revision": "b" * 40,
            "source_card": "9c71f24a",
            "card_generation": card_generation(store.fold("9c71f24a")),
            "source_owner": "mero",
            "reviewer_candidates": [
                {
                    "name": "Seraph",
                    "seat": "seraph",
                    "identity": "pi-seraph-chiap08-review",
                    "eligible": True,
                }
            ],
        },
        evidence_sha256="c" * 64,
    )
    assert result.created is True
    assert result.launchable is True
    return store, result.review_card_id


def test_real_selector_claims_and_launches_one_canonical_seraph_review(
    tmp_path: Path,
) -> None:
    repository = "https://github.com/smilinTux/skcapstone"
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(seed)], check=True, capture_output=True
    )
    (seed / "REAL-GIT-WORKSPACE.txt").write_text(
        "real Link to Seraph workspace\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.name=SKCapstone Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "seed real workspace",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True
    )
    expected_revision = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    home = tmp_path / "home"
    home.mkdir()
    store, card_id = _canonical_review(home)
    placement = home / ".skcapstone" / "coordination" / "seat-placement.json"
    placement.write_text(
        json.dumps({"schema_version": 1, "seats": {"seraph": ["chiap08"]}}),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launch_argv = tmp_path / "systemd-run.argv"
    unit_state = tmp_path / "active-unit"
    _executable(fake_bin / "tmux", "exit 0")
    _executable(
        fake_bin / "python3",
        f'if [ "${{1:-}}" = "-" ]; then printf "%s\\n" {GATEWAY_REVISION}; exit 0; fi\n'
        f'exec "{sys.executable}" "$@"',
    )
    _executable(
        fake_bin / "systemctl",
        f'if [ "$1" = "--user" ] && [ "$2" = "list-units" ]; then '
        f'[ ! -s {unit_state} ] || printf "%s loaded active running\\n" "$(cat {unit_state})"; '
        "exit 0; fi\n"
        f'if [ "$1" = "--user" ] && [ "$2" = "is-active" ]; then '
        f'[ -s {unit_state} ] && [ "$(cat {unit_state})" = "$4" ]; exit $?; fi\n'
        "exit 1",
    )
    _executable(
        fake_bin / "systemd-run",
        f'printf "%s\\n" "$@" >> {launch_argv}\n'
        f'while [ "$#" -gt 0 ]; do '
        f'if [ "$1" = "--unit" ]; then shift; printf "%s" "$1" > {unit_state}; break; fi; '
        "shift; done",
    )
    skc = home / ".skenv" / "bin" / "skcapstone"
    skc.parent.mkdir(parents=True)
    _executable(skc, 'exec "$SKFLEET_TEST_PYTHON" -m skcapstone "$@"')

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    verified_path = tmp_path / "verified.json"
    harness = """
import contextlib
import fcntl
import io
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from skcapstone.seat_cycle_entrypoint import verify_seraph_dispatch

os.uname = lambda: SimpleNamespace(nodename="chiap08")
fcntl.flock = lambda *_args: None
script, output_dir, verified_path = sys.argv[1:]

def cycle(name, seat):
    os.environ["SKFLEET_ONLY_SEAT"] = seat
    sys.argv = [script, "--go"]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        try:
            runpy.run_path(script, run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    text = output.getvalue()
    (Path(output_dir) / (name + ".out")).write_text(text, encoding="utf-8")
    return text

cycle("generic", "")
seraph = cycle("seraph", "seraph")
verified = verify_seraph_dispatch(
    Path(os.environ["SKCAPSTONE_HOME"]),
    subprocess.CompletedProcess([], 0, stdout=seraph, stderr=""),
)
Path(verified_path).write_text(json.dumps(verified), encoding="utf-8")
cycle("replay", "seraph")
"""
    skcoord_root = Path(lifecycle_reassessment.__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "SKCAPSTONE_HOME": str(home / ".skcapstone"),
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(ROOT / "src")
            + os.pathsep
            + str(skcoord_root)
            + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
            "SKCOORD_SRC": str(skcoord_root),
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{origin}.insteadOf",
            "GIT_CONFIG_VALUE_0": repository,
            "SKFLEET_TEST_PYTHON": sys.executable,
            "SKFLEET_TARGET": "2",
            "SKFLEET_GLM_TARGET": "0",
            "SKFLEET_QWEN_TARGET": "0",
            "SKFLEET_KIMI_TARGET": "0",
            "SKFLEET_ESC_TARGET": "0",
            "SKFLEET_SEAT_TARGET": "1",
            "SKFLEET_CODEX_PHYSICAL_LIMIT": "3",
            "SKFLEET_CODEX_MODEL_S": "sk-codex-mid",
            "SKFLEET_MAX_LAUNCH": "1",
            "SKFLEET_PI_CARDSTORE_GUARD": str(
                ROOT / "scripts" / "fleet" / "pi-cardstore-guard.mjs"
            ),
        }
    )
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyGateway)
    env["SKFLEET_GATEWAY_URL"] = f"http://127.0.0.1:{gateway.server_port}"
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                harness,
                str(ROTATE),
                str(output_dir),
                str(verified_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join()
    assert completed.returncode == 0, completed.stderr

    generic_output = (output_dir / "generic.out").read_text(encoding="utf-8")
    seraph_output = (output_dir / "seraph.out").read_text(encoding="utf-8")
    replay_output = (output_dir / "replay.out").read_text(encoding="utf-8")
    assert "LAUNCHED|" not in generic_output
    assert "NOOP_RECEIPT|chiap08|reason=no_eligible_work|seat=generic" in generic_output
    assert seraph_output.count("LAUNCHED|") == 1
    assert "LAUNCHED|chiap08|codex-auto-" + card_id in seraph_output
    assert "LANE_ADMISSION_BLOCKED|" not in seraph_output
    assert "LAUNCHED|" not in replay_output

    lane_snapshot = json.loads(
        (home / ".skcapstone" / "evidence" / "fleet-lane-health.json").read_text(encoding="utf-8")
    )
    assert lane_snapshot["runtime_revision"] == GATEWAY_REVISION
    assert lane_snapshot["errors"] == []
    codex_lane = next(row for row in lane_snapshot["lanes"] if row["lane"] == "codex")
    assert codex_lane["domains"] == [{"capacity_domain": "codex", "max": 3, "state": "healthy"}]

    folded = store.fold(card_id)
    assert folded is not None
    assert folded.owner == f"pi-seraph-chiap08-{card_id}"
    unit = f"skfleet-worker-codex-{card_id}.service"
    assert unit_state.read_text(encoding="utf-8") == unit
    assert launch_argv.read_text(encoding="utf-8").splitlines().count(unit) == 1
    assert json.loads(verified_path.read_text(encoding="utf-8")) == {
        "cards_examined": 1,
        "recommendations": 1,
        "suppressed": 0,
        "dispatch_succeeded": 1,
        "dispatch_failed": 0,
        "dispatch_retryable": 0,
        "reason": "seraph_dispatch_complete",
    }
    claim_events = [
        event for event in store._read_events(card_id) if event.get("action") == "claim"
    ]
    receipt_events = [
        event
        for event in store._read_events(card_id)
        if event.get("action") == "review_assignment_launch" and event.get("launched") is True
    ]
    assert len(claim_events) == 1
    assert len(receipt_events) == 1
    workspace = home / ".skcapstone" / "fleet" / "workspaces" / folded.owner
    assert (workspace / "REAL-GIT-WORKSPACE.txt").read_text(encoding="utf-8") == (
        "real Link to Seraph workspace\n"
    )
    git_checks = {
        "origin": ["config", "--get", "remote.origin.url"],
        "branch": ["branch", "--show-current"],
        "status": ["status", "--porcelain=v1"],
        "revision": ["rev-parse", "HEAD"],
    }
    observed = {
        name: subprocess.run(
            ["git", "-C", str(workspace), *command],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for name, command in git_checks.items()
    }
    assert observed == {
        "origin": repository,
        "branch": "main",
        "status": "",
        "revision": expected_revision,
    }
