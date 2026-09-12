"""Generation-keyed bounded cooldown for classified pre-agent rejections.

Live evidence (review card 6dd138ad): the seat worker exited before any agent
output with a structured ``400 Unable to generate parser for this template``
rejection from the upstream route. The terminal record carried
``transport_failure: null``, so the scheduler's bounded transport hold never
fired and the exact unchanged review generation was reoffered and reclaimed in
the next eligible cycle, burning a review lane slot every cycle.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import glob
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"

LIVE_PARSER_REJECTION = (
    b'400: {"code":400,"message":"Unable to generate parser for this template. '
    b"Automatic parser generation failed: \\n------------\\nWhile executing "
    b"CallExpression at line 106, column 32 in source:\\n...first "
)


def _wrapper():
    spec = importlib.util.spec_from_file_location("skfleet_worker_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scheduler_namespace() -> dict[str, object]:
    wanted = {
        "_latest_transport_failure_epoch",
        "_transport_retry_held",
    }
    constants = {
        "_WORKER_EXIT_DIR",
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
    namespace: dict[str, object] = {
        "glob": glob,
        "json": json,
        "os": os,
        "time": time,
        "datetime": datetime,
        "HOME": "/unused",
        "_ts_epoch": lambda value: datetime.datetime.fromisoformat(str(value or "")).timestamp(),
        "_TRANSPORT_RETRY_COOLDOWN_S": 60.0,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert wanted <= namespace.keys()
    return namespace


def _write_rejection(
    evidence_dir: Path,
    card: str,
    *,
    attempted_at: str,
    generation: str = "",
    failure: str = "upstream_template_rejection",
) -> None:
    record = {
        "card_id": card,
        "attempted_at": attempted_at,
        "transport_failure": failure,
    }
    if generation:
        record["card_generation"] = generation
    digest = hashlib.sha256(f"{card}\0{attempted_at}".encode()).hexdigest()[:16]
    (evidence_dir / f"{card}-{digest}.json").write_text(json.dumps(record), encoding="utf-8")


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


# ---- wrapper classification -------------------------------------------------


def test_live_parser_rejection_stderr_is_classified() -> None:
    module = _wrapper()
    assert (
        module.classify_pre_agent_failure(b"", LIVE_PARSER_REJECTION, 1)
        == "upstream_template_rejection"
    )


def test_parser_rejection_leading_stdout_is_classified() -> None:
    module = _wrapper()
    stdout = b"Unable to generate parser for this template. Automatic parser generation failed."
    assert module.classify_pre_agent_failure(stdout, b"", 1) == "upstream_template_rejection"


def test_agent_output_mentioning_parser_failure_stays_substantive() -> None:
    module = _wrapper()
    stdout_text = b"analysis of unable to generate parser errors in prior attempts"
    assert module.classify_pre_agent_failure(stdout_text, b"", 1) is None
    assert module.classify_pre_agent_failure(b"", b"some agent stderr", 1) is None


def test_zero_exit_never_classified() -> None:
    module = _wrapper()
    assert module.classify_pre_agent_failure(b"", LIVE_PARSER_REJECTION, 0) is None


# ---- wrapper generation stamp ------------------------------------------------


def test_terminal_record_stamps_card_generation(tmp_path: Path, monkeypatch) -> None:
    module = _wrapper()
    monkeypatch.setattr(module, "card_description_generation", lambda card_id: "gen-" + card_id)
    stdout = tmp_path / "deadbeef-20260912T000000Z.log"
    stdout.write_bytes(b"")
    args = argparse.Namespace(
        card="deadbeef",
        owner="owner",
        claim_revision="rev",
        host="host",
        lane="codex",
        model="model",
        stdout=stdout,
        evidence_dir=tmp_path / "evidence",
    )
    module.record_terminal_exit(args, LIVE_PARSER_REJECTION, 1)
    records = list(args.evidence_dir.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text(encoding="utf-8"))
    assert payload["transport_failure"] == "upstream_template_rejection"
    assert payload["card_generation"] == "gen-deadbeef"


# ---- scheduler generation-keyed hold ------------------------------------------


def test_same_generation_rejection_held_then_released_after_cooldown(
    tmp_path: Path,
) -> None:
    namespace = _scheduler_namespace()
    card = "6dd138ad"
    now = time.time()
    evidence = tmp_path / "worker-exits"
    evidence.mkdir()
    _write_rejection(evidence, card, attempted_at=_iso(now - 10), generation="gen-a")
    namespace["_WORKER_EXIT_DIR"] = str(evidence)
    namespace["_card_description_generation"] = lambda _cid: "gen-a"
    assert namespace["_transport_retry_held"](card) is True

    clock = type("Clock", (), {"time": staticmethod(lambda: now + 120)})
    namespace["time"] = clock
    assert namespace["_transport_retry_held"](card) is False


def test_changed_candidate_generation_stays_eligible(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "6dd138ad"
    evidence = tmp_path / "worker-exits"
    evidence.mkdir()
    _write_rejection(evidence, card, attempted_at=_iso(time.time() - 5), generation="gen-stale")
    namespace["_WORKER_EXIT_DIR"] = str(evidence)
    namespace["_card_description_generation"] = lambda _cid: "gen-current"
    assert namespace["_transport_retry_held"](card) is False


def test_unstamped_rejection_keeps_legacy_card_level_hold(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    evidence = tmp_path / "worker-exits"
    evidence.mkdir()
    _write_rejection(evidence, card, attempted_at=_iso(time.time() - 5))
    namespace["_WORKER_EXIT_DIR"] = str(evidence)
    namespace["_card_description_generation"] = lambda _cid: "gen-a"
    assert namespace["_transport_retry_held"](card) is True


def test_failed_probe_restarts_bounded_interval(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "6dd138ad"
    now = time.time()
    evidence = tmp_path / "worker-exits"
    evidence.mkdir()
    _write_rejection(evidence, card, attempted_at=_iso(now - 50), generation="gen-a")
    namespace["_WORKER_EXIT_DIR"] = str(evidence)
    namespace["_card_description_generation"] = lambda _cid: "gen-a"
    assert namespace["_transport_retry_held"](card) is True
    _write_rejection(evidence, card, attempted_at=_iso(now - 5), generation="gen-a")
    assert namespace["_transport_retry_held"](card) is True


def test_non_rejection_evidence_never_holds(tmp_path: Path) -> None:
    namespace = _scheduler_namespace()
    card = "deadbeef"
    evidence = tmp_path / "worker-exits"
    evidence.mkdir()
    record = {
        "card_id": card,
        "attempted_at": _iso(time.time() - 5),
        "transport_failure": None,
        "card_generation": "gen-a",
    }
    (evidence / f"{card}-deadbeef00.json").write_text(json.dumps(record), encoding="utf-8")
    namespace["_WORKER_EXIT_DIR"] = str(evidence)
    namespace["_card_description_generation"] = lambda _cid: "gen-a"
    assert namespace["_transport_retry_held"](card) is False
