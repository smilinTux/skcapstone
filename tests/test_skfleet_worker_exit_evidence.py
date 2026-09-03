"""Fleet worker terminal evidence and transport backoff tests."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def _wrapper():
    spec = importlib.util.spec_from_file_location("skfleet_worker_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scheduler_namespace() -> dict[str, object]:
    wanted = {
        "_latest_transport_failure_epoch",
        "_transport_failure_logs",
        "_transport_failure_claims",
        "_transport_retry_held",
        "_reporting_launches",
        "_shared_launch_attempts",
        "launch_attempts",
        "unclaimable",
    }
    constants = {
        "_LAUNCH_TTL_H",
        "_LOGDIR",
        "_WORKER_EXIT_DIR",
        "_ROTATION_EVID",
        "_TRANSPORT_FAILURE_CLASSES",
        "_TRANSPORT_RETRY_COOLDOWN_S",
    }
    nodes = []
    for node in ast.parse(ROTATE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & constants:
                nodes.append(node)
    namespace = {
        "glob": __import__("glob"),
        "json": json,
        "os": os,
        "time": time,
        "HOME": "/unused",
        "_ts_epoch": lambda value: time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")),
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("diagnostic", "kind"),
    [
        ("HTTP 429", "rate_limited"),
        ("model_owner_backend_down", "model_owner_backend_down"),
        ("backend-claims-quarantined", "backend_claims_quarantined"),
        ("invalid_upstream_tool_calls", "invalid_upstream_tool_calls"),
        ("Connection refused", "connection_failure"),
    ],
)
def test_known_transport_failures_are_classified(diagnostic: str, kind: str) -> None:
    assert _wrapper().classify_transport_failure(diagnostic) == kind


def test_scheduler_transport_classes_match_wrapper_exactly() -> None:
    namespace = _scheduler_namespace()
    assert namespace["_TRANSPORT_FAILURE_CLASSES"] == frozenset(_wrapper().TRANSPORT_PATTERNS)


def test_zero_stdout_exit_records_bounded_redacted_claim_evidence(tmp_path: Path) -> None:
    module = _wrapper()
    stdout = tmp_path / "deadbeef-20260903T170000Z.log"
    stdout.write_bytes(b"")
    evidence = tmp_path / "evidence"
    args = argparse.Namespace(
        card="deadbeef",
        owner="pi-qwen-chiap02-deadbeef",
        claim_revision="rev-7",
        host="chiap02",
        lane="qwen",
        model="qwen-local",
        stdout=stdout,
        evidence_dir=evidence,
    )
    stderr = b"x" * 3000 + b" api_key=supersecretvalue Connection refused"
    module.record_terminal_exit(args, stderr, 17)
    records = list(evidence.glob("*.json"))
    assert len(records) == 1
    assert records[0].stat().st_mode & 0o777 == 0o600
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    expected = {
        "card_id": "deadbeef",
        "owner": "pi-qwen-chiap02-deadbeef",
        "claim_revision": "rev-7",
        "host": "chiap02",
        "lane": "qwen",
        "model": "qwen-local",
        "child_exit_code": 17,
    }
    assert {key: payload[key] for key in expected} == expected
    assert "supersecretvalue" not in payload["stderr"]
    assert len(payload["stderr"]) <= module.STDERR_LIMIT
    assert payload["transport_failure"] == "connection_failure"
    assert payload["attempted_at"]


def test_substantive_stdout_does_not_create_terminal_record(tmp_path: Path) -> None:
    module = _wrapper()
    stdout = tmp_path / "feedface.log"
    stdout.write_text("agent completed substantive work", encoding="utf-8")
    args = argparse.Namespace(
        card="feedface",
        owner="owner",
        claim_revision="rev",
        host="host",
        lane="codex",
        model="model",
        stdout=stdout,
        evidence_dir=tmp_path / "evidence",
    )
    module.record_terminal_exit(args, b"", 0)
    assert not args.evidence_dir.exists()


def test_substantive_failure_mentioning_429_remains_substantive(tmp_path: Path) -> None:
    """A transport phrase inside agent output must still consume an attempt."""
    module = _wrapper()
    stdout = tmp_path / "feedface.log"
    stdout.write_text("analysis found a prior HTTP 429", encoding="utf-8")
    args = argparse.Namespace(
        card="feedface",
        owner="owner",
        claim_revision="rev",
        host="host",
        lane="codex",
        model="model",
        stdout=stdout,
        evidence_dir=tmp_path / "evidence",
    )
    module.record_terminal_exit(args, b"", 1)
    assert not args.evidence_dir.exists()


def test_transport_exit_is_held_then_does_not_consume_attempt(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    logs = tmp_path / "logs"
    evidence = tmp_path / "evidence"
    logs.mkdir()
    evidence.mkdir()
    log = logs / f"{card}-20260903T170000Z.log"
    log.write_text("HTTP 429", encoding="utf-8")
    attempted = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    (evidence / f"{card}-one.json").write_text(
        json.dumps(
            {
                "card_id": card,
                "attempted_at": attempted,
                "stdout_log": log.name,
                "transport_failure": "rate_limited",
            }
        ),
        encoding="utf-8",
    )
    namespace.update(
        {
            "_LOGDIR": str(logs),
            "_WORKER_EXIT_DIR": str(evidence),
            "_LAUNCH_TTL_H": 6,
            "_TRANSPORT_RETRY_COOLDOWN_S": 60,
        }
    )
    assert namespace["_reporting_launches"](card) == 0
    assert namespace["_transport_retry_held"](card) is True


def test_three_shared_transport_failures_do_not_consume_attempts(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    evidence = tmp_path / "worker-exits"
    rotations = tmp_path / "rotations"
    evidence.mkdir()
    rotation = rotations / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rotation.mkdir(parents=True)
    launches = [
        ("chiap01", "owner-a", "revision-a"),
        ("chiap02", "owner-b", "revision-b"),
        ("chiap03", "owner-c", "revision-c"),
    ]
    (rotation / "actions.log").write_text(
        "".join(
            f"LAUNCHED|{host}|codex-auto-{card}|{card}|lane=codex|model=model|"
            f"owner={owner}|claim_revision={revision}\n"
            for host, owner, revision in launches
        ),
        encoding="utf-8",
    )
    for index, (host, owner, revision) in enumerate(launches[:3]):
        (evidence / f"{card}-{index}.json").write_text(
            json.dumps(
                {
                    "card_id": card,
                    "claim_revision": revision,
                    "host": host,
                    "owner": owner,
                    "transport_failure": "rate_limited",
                }
            ),
            encoding="utf-8",
        )
    namespace.update(
        {
            "_WORKER_EXIT_DIR": str(evidence),
            "_LOGDIR": str(tmp_path / "logs"),
            "_ROTATION_EVID": str(rotations),
            "_LAUNCH_TTL_H": 6,
            "_shared_launch_cache": None,
            "acts": lambda _cid: set(),
        }
    )
    assert namespace["launch_attempts"](card) == 0
    assert namespace["unclaimable"](card) is False


@pytest.mark.parametrize(
    "failure",
    ["unknown_transport", "", None, {"rate_limited": True}, ["rate_limited"]],
)
def test_shared_attempts_count_noncanonical_transport_evidence(
    tmp_path: Path, failure: object
) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    evidence = tmp_path / "worker-exits"
    rotations = tmp_path / "rotations"
    evidence.mkdir()
    rotation = rotations / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rotation.mkdir(parents=True)
    (rotation / "actions.log").write_text(
        f"LAUNCHED|chiap01|codex-auto-{card}|{card}|lane=codex|model=model|"
        "owner=owner-a|claim_revision=revision-a\n",
        encoding="utf-8",
    )
    (evidence / f"{card}-wrong.json").write_text(
        json.dumps(
            {
                "card_id": card,
                "claim_revision": "revision-a",
                "host": "chiap01",
                "owner": "owner-a",
                "transport_failure": failure,
            }
        ),
        encoding="utf-8",
    )
    namespace.update(
        {
            "_WORKER_EXIT_DIR": str(evidence),
            "_ROTATION_EVID": str(rotations),
            "_LAUNCH_TTL_H": 6,
            "_shared_launch_cache": None,
        }
    )
    assert namespace["_shared_launch_attempts"](card) == 1


def test_shared_attempts_count_transport_evidence_for_another_claim(
    tmp_path: Path,
) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    evidence = tmp_path / "worker-exits"
    rotations = tmp_path / "rotations"
    evidence.mkdir()
    rotation = rotations / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rotation.mkdir(parents=True)
    (rotation / "actions.log").write_text(
        f"LAUNCHED|chiap01|codex-auto-{card}|{card}|lane=codex|model=model|"
        "owner=owner-a|claim_revision=revision-a\n",
        encoding="utf-8",
    )
    (evidence / f"{card}-wrong.json").write_text(
        json.dumps(
            {
                "card_id": card,
                "claim_revision": "revision-other",
                "host": "chiap01",
                "owner": "owner-a",
                "transport_failure": "rate_limited",
            }
        ),
        encoding="utf-8",
    )
    namespace.update(
        {
            "_WORKER_EXIT_DIR": str(evidence),
            "_ROTATION_EVID": str(rotations),
            "_LAUNCH_TTL_H": 6,
            "_shared_launch_cache": None,
        }
    )
    assert namespace["_shared_launch_attempts"](card) == 1


def test_launcher_routes_every_lane_through_exit_wrapper() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert 'wrapper=os.path.join(os.path.dirname(__file__),"skfleet-worker-wrapper.py")' in source
    assert '"--claim-revision",claimed_revision' in source
    assert "inner=shlex.join([" in source
    assert "subprocess.run(_worker_launch_command(unit,workspace,inner)" in source
