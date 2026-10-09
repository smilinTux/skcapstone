"""Cycle read reuse must give byte-identical answers to the per-card reads it replaced.

The Niobe cycle on chiap08 hit its 270s limit because each card re-read shared
state: one CardStore per worker beat or failed requalification record (each
replaying the whole legacy card_events overlay), one Node artifact hash per
sealed plan, and one directory listing per card. These tests pin that every
reuse returns exactly what the uncached read returns, and that a reuse is
dropped as soon as the state it stands for can have changed.
"""

from __future__ import annotations

import ast
import copy
import glob
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.fleet import production_test_node as node
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import profile_requalification as refresh
from skcapstone.fleet import worker_liveness_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _rotate(names: set[str], constants: set[str], **namespace: object) -> dict:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for item in tree.body:
        if isinstance(item, ast.FunctionDef) and item.name in names:
            nodes.append(item)
        elif isinstance(item, ast.Assign):
            targets = {t.id for t in item.targets if isinstance(t, ast.Name)}
            if targets & constants:
                nodes.append(item)
    values = {"glob": glob, "json": json, "os": os, "re": re, "time": time, "copy": copy}
    values.update(namespace)
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), values)
    assert names <= values.keys()
    return values


def _listing(tmp_path: Path):
    ns = _rotate(
        {"_prefixed_names", "_worker_exit_paths"},
        {"_DIR_LISTINGS", "_RACY_DIR_NS"},
    )
    exits = tmp_path / "exits"
    exits.mkdir()
    ns["_WORKER_EXIT_DIR"] = str(exits)
    return ns, exits


NAMES = (
    "deadbeef-20261009T010203Z.log",
    "deadbeef-.log",
    "deadbeef-a-b-c.json",
    "deadbeef-.json",
    "deadbeef.json",
    "deadbeefx-1.json",
    "deadbee-1.json",
    ".deadbeef-hidden.json",
    "cafef00d-1.json",
    "cafef00d-20261009T010203Z.log",
    "deadbeef-dir.json",
    "notes.txt",
)


def _populate(directory: Path) -> None:
    for name in NAMES:
        if name.endswith("dir.json"):
            (directory / name).mkdir()
        else:
            (directory / name).write_text("{}", encoding="utf-8")


def _age(directory: Path, seconds: int = 3600) -> None:
    old = time.time() - seconds
    os.utime(directory, (old, old))


@pytest.mark.parametrize("aged", [False, True])
@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("deadbeef-", ".log"),
        ("deadbeef-", ".json"),
        ("cafef00d-", ".json"),
        ("dead-beef-", ".json"),
        ("missing-", ".log"),
    ],
)
def test_prefixed_names_matches_listdir_filter(tmp_path, aged, prefix, suffix):
    ns, exits = _listing(tmp_path)
    _populate(exits)
    if aged:
        _age(exits)
    expected = sorted(
        name
        for name in os.listdir(exits)
        if name.startswith(prefix)
        and name.endswith(suffix)
        and len(name) >= len(prefix) + len(suffix)
    )
    for _ in range(2):  # second call is served from the reused listing when aged
        assert sorted(ns["_prefixed_names"](str(exits), prefix, suffix)) == expected
    assert (str(exits) in ns["_DIR_LISTINGS"]) is aged


@pytest.mark.parametrize("aged", [False, True])
@pytest.mark.parametrize("cid", ["deadbeef", "cafef00d", "0badc0de"])
def test_worker_exit_paths_match_glob(tmp_path, aged, cid):
    ns, exits = _listing(tmp_path)
    _populate(exits)
    if aged:
        _age(exits)
    expected = sorted(glob.glob(os.path.join(str(exits), cid + "-*.json")))
    assert sorted(ns["_worker_exit_paths"](cid)) == expected
    assert sorted(ns["_worker_exit_paths"](cid)) == expected


def test_worker_exit_paths_missing_directory_is_empty_like_glob(tmp_path):
    ns, exits = _listing(tmp_path)
    exits.rmdir()
    assert ns["_worker_exit_paths"]("deadbeef") == []
    assert glob.glob(os.path.join(str(exits), "deadbeef-*.json")) == []


def test_prefixed_names_missing_directory_raises_like_listdir(tmp_path):
    ns, _exits = _listing(tmp_path)
    with pytest.raises(OSError):
        ns["_prefixed_names"](str(tmp_path / "absent"), "deadbeef-", ".log")


def test_reused_listing_sees_added_and_removed_entries(tmp_path):
    ns, exits = _listing(tmp_path)
    _populate(exits)
    _age(exits)
    listed = []
    real = os.listdir
    ns["os"] = SimpleNamespace(**{k: getattr(os, k) for k in ("stat", "path")})
    ns["os"].listdir = lambda path: listed.append(path) or real(path)
    first = ns["_worker_exit_paths"]("deadbeef")
    ns["_worker_exit_paths"]("deadbeef")
    ns["_worker_exit_paths"]("cafef00d")
    assert len(listed) == 1, "an unchanged, settled directory is listed once"

    (exits / "deadbeef-new.json").write_text("{}", encoding="utf-8")
    added = ns["_worker_exit_paths"]("deadbeef")
    assert sorted(added) == sorted(first + [str(exits / "deadbeef-new.json")])
    assert len(listed) == 2

    (exits / "deadbeef-new.json").unlink()
    assert sorted(ns["_worker_exit_paths"]("deadbeef")) == sorted(first)


def test_racy_listing_is_never_reused(tmp_path):
    """A directory touched within the coarse timestamp window is re-listed each time."""
    ns, exits = _listing(tmp_path)
    _populate(exits)
    listed = []
    real = os.listdir
    ns["os"] = SimpleNamespace(**{k: getattr(os, k) for k in ("stat", "path")})
    ns["os"].listdir = lambda path: listed.append(path) or real(path)
    ns["_worker_exit_paths"]("deadbeef")
    ns["_worker_exit_paths"]("deadbeef")
    assert len(listed) == 2
    assert str(exits) not in ns["_DIR_LISTINGS"]


def test_launch_evidence_identical_with_and_without_listing_reuse(tmp_path):
    names = {
        "_card_mutated_during_report",
        "_structured_transport_failure",
        "_is_substantive_worker_report",
        "_launch_epoch_from_log",
        "_local_launch_evidence",
        "_reporting_launches",
        "_transport_failure_logs",
        "_latest_transport_failure_epoch",
        "_completion_retry_held",
    }
    from skcapstone.fleet import gateway_failure

    extra = {
        "GATEWAY_ERROR_RE": gateway_failure.GATEWAY_ERROR_RE,
        "TRANSPORT_FAILURE_CLASSES": gateway_failure.TRANSPORT_FAILURE_CLASSES,
        "classify_gateway_failure": gateway_failure.classify_gateway_failure,
        "datetime": __import__("datetime"),
        "event_rows": lambda _cid: [],
        "_ts_epoch": lambda value: float(value or 0),
    }
    constants = {
        "_GATEWAY_ERROR_RE",
        "_COMPLETION_FAILURE_CLASSES",
        "_COMPLETION_RETRY_COOLDOWN_S",
    }
    plain = _rotate(names, constants, **extra)
    reuse = _rotate(
        names | {"_prefixed_names", "_worker_exit_paths"},
        constants | {"_DIR_LISTINGS", "_RACY_DIR_NS"},
        **extra,
    )
    logs = tmp_path / "logs"
    exits = tmp_path / "exits"
    logs.mkdir()
    exits.mkdir()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - 60))
    (logs / f"deadbeef-{stamp}.log").write_text("worker reported real work\n")
    (logs / f"cafef00d-{stamp}.log").write_text(
        '404: {"message":"gateway route unavailable","code":404}\n'
    )
    (logs / f"cafef00d-{stamp}x.log").write_text("")
    (logs / "unrelated.log").write_text("x")
    for cid, extra_fields in (
        ("cafef00d", {"transport_failure": True, "stdout_log": f"cafef00d-{stamp}.log"}),
        ("deadbeef", {"completion_failure": "zero-output"}),
    ):
        (exits / f"{cid}-1.json").write_text(
            json.dumps({"card_id": cid, "attempted_at": time.time() - 30, **extra_fields})
        )
    _age(logs)
    _age(exits)
    for ns in (plain, reuse):
        ns.update(
            _LOGDIR=str(logs),
            _WORKER_EXIT_DIR=str(exits),
            _LAUNCH_TTL_H=6.0,
            _COMPLETION_FAILURE_CLASSES=frozenset({"zero-output"}),
        )
    for cid in ("deadbeef", "cafef00d", "0badc0de"):
        for _ in range(2):
            for fn in (
                "_local_launch_evidence",
                "_reporting_launches",
                "_transport_failure_logs",
                "_latest_transport_failure_epoch",
                "_completion_retry_held",
            ):
                assert reuse[fn](cid) == plain[fn](cid), (fn, cid)
    assert str(logs) in reuse["_DIR_LISTINGS"] and str(exits) in reuse["_DIR_LISTINGS"]


def test_lifecycle_state_reads_cached_fold_without_copy_and_same_answer():
    ns = _rotate({"lifecycle_state"}, set())
    states = {
        "a": {"status": "done", "archived": False, "voided": False, "owner": None},
        "b": {"status": "ready", "archived": True, "voided": False, "owner": None},
        "c": {"status": "doing", "archived": False, "voided": False, "owner": "w"},
        "d": {"status": "backlog", "archived": False, "voided": False, "owner": "w"},
    }
    calls = []

    def fold(cid):
        calls.append(cid)
        if cid == "bad":
            raise ValueError("core identity mismatch")
        return {"id": cid}, copy.deepcopy(states[cid])

    ns["_authoritative_card_state"] = fold
    ns["_selection_snapshots"] = None
    uncached = {cid: ns["lifecycle_state"](cid) for cid in [*states, "bad"]}
    assert uncached == {
        "a": "complete",
        "b": "void",
        "c": "claimed",
        "d": "open",
        "bad": "ambiguous",
    }
    snapshots = {cid: ({"id": cid}, copy.deepcopy(state), "rev") for cid, state in states.items()}
    frozen = copy.deepcopy(snapshots)
    ns["_selection_snapshots"] = snapshots
    calls.clear()
    assert {cid: ns["lifecycle_state"](cid) for cid in [*states, "bad"]} == uncached
    assert calls == ["bad"], "only an uncached card is folded"
    assert snapshots == frozen, "the shared snapshot is never mutated"


class _CountingStore:
    """CardStore stand-in that counts instances and returns fixed folds."""

    instances = 0

    def __init__(self, home, cards):
        type(self).instances += 1
        self.cards = cards

    def fold(self, card_id):
        return self.cards.get(card_id)


def test_claim_revision_identical_from_shared_and_fresh_store(tmp_path):
    home = tmp_path
    store = CardStore(home)
    store.create(CardCore(id="deadbeef", title="[S] claimed"))
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="rev-1")
    store.create(CardCore(id="cafef00d", title="[S] unclaimed"))
    shared = CardStore(home)
    for cid in ("deadbeef", "cafef00d", "0badc0de"):
        assert runtime._claim_revision(home, cid, shared) == runtime._claim_revision(home, cid)
    assert runtime._claim_revision(home, "deadbeef") is not None


def test_collect_observations_folds_every_beat_from_one_store(tmp_path, monkeypatch):
    beats = tmp_path / "fleet" / "beats"
    beats.mkdir(parents=True)
    for index, cid in enumerate(("deadbeef", "cafef00d", "0badc0de")):
        (beats / f"{cid}.json").write_text(
            json.dumps(
                {
                    "pid": 100 + index,
                    "invocation_id": "d" * 32,
                    "card_id": cid,
                    "agent": "worker",
                    "claim_revision": "stale",
                }
            )
        )
    cards = {
        cid: SimpleNamespace(status="doing", claim_revision="current", meta={})
        for cid in ("deadbeef", "cafef00d", "0badc0de")
    }
    _CountingStore.instances = 0
    monkeypatch.setattr(runtime, "CardStore", lambda home: _CountingStore(home, cards))
    assert runtime.collect_observations(tmp_path) == ()
    assert _CountingStore.instances == 1


def _failed_job(root: Path, suffix: str, card_id: str, owner: str, revision: str) -> None:
    path = root / (suffix * 64 + ".job.json")
    plan.write_once(path, {"card": card_id, "owner": owner, "claim_revision": revision})
    plan.write_once(
        path.with_name(path.stem + ".failed.json"),
        {"card": card_id, "reason": "operator test recipe is invalid"},
    )


def test_release_failed_claims_reuses_store_until_a_release_runs(tmp_path, monkeypatch):
    root = tmp_path / "fleet/profile-requalifications"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    jobs = [
        ("a", "11111111", "rev-a"),
        ("b", "22222222", "rev-b"),
        ("c", "33333333", "rev-c"),
        ("d", "44444444", "rev-d"),
    ]
    cards = {}
    for suffix, cid, revision in jobs:
        owner = "niobe-requal-" + cid
        _failed_job(root, suffix, cid, owner, revision)
        # Only 33333333 still holds its requalification claim.
        held = cid == "33333333"
        cards[cid] = SimpleNamespace(
            status=SimpleNamespace(value="doing" if held else "backlog"),
            owner=owner if held else None,
            meta={"_claim_revision": revision} if held else {},
        )
    _CountingStore.instances = 0
    monkeypatch.setattr(refresh, "CardStore", lambda home: _CountingStore(home, cards))
    calls = []

    def coord(argv, **_kwargs):
        calls.append(argv[1:4])
        card = cards[argv[3]]
        if argv[2] == "release-claim":
            card.status.value, card.owner = "backlog", None
        elif argv[2] == "move":
            card.status.value = argv[4]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", coord)
    states = refresh._release_failed_claims(tmp_path, "/x/skcapstone", "niobe", root)
    assert states == ["33333333=released"]
    assert calls == [["coord", "release-claim", "33333333"], ["coord", "move", "33333333"]]
    # one shared store for the reads up to the release, one fresh read after
    # it, then a fresh shared store for the record after the release
    assert _CountingStore.instances == 3


def test_artifact_digest_memo_returns_identical_digest_once_per_scope(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    (artifact / "node_modules").mkdir(parents=True)
    (artifact / "node_modules" / "a.js").write_text("module.exports = 1\n")
    for path in (artifact / "node_modules" / "a.js", artifact / "node_modules", artifact):
        path.chmod(0o555 if path.is_dir() else 0o444)
    try:
        uncached = node.artifact_digest(artifact)
        walks = []
        real = node._artifact_digest
        monkeypatch.setattr(node, "_artifact_digest", lambda p: walks.append(p) or real(p))
        with node.artifact_digest_memo():
            assert node.artifact_digest(artifact) == uncached
            assert node.artifact_digest(artifact) == uncached
            with node.artifact_digest_memo():
                assert node.artifact_digest(artifact) == uncached
        assert len(walks) == 1
        assert node.artifact_digest(artifact) == uncached
        assert len(walks) == 2, "outside a scope nothing is remembered"
    finally:
        for path in (artifact, artifact / "node_modules", artifact / "node_modules" / "a.js"):
            path.chmod(0o755 if path.is_dir() else 0o644)


def test_artifact_digest_memo_never_remembers_a_failure(tmp_path, monkeypatch):
    attempts = []

    def failing(path):
        attempts.append(path)
        raise plan.TestEvidenceError("Node artifact is writable")

    monkeypatch.setattr(node, "_artifact_digest", failing)
    with node.artifact_digest_memo():
        for _ in range(2):
            with pytest.raises(plan.TestEvidenceError):
                node.artifact_digest(tmp_path)
    assert len(attempts) == 2


def test_node_binary_digest_identical_inside_and_outside_scope(tmp_path, monkeypatch):
    binary = tmp_path / "node"
    binary.write_bytes(b"\x7fELF fake node")
    monkeypatch.setattr(node, "NODE", binary)
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    outside = node._node_digest()
    assert outside == plan.sha(binary.read_bytes())
    reads.clear()
    with node.artifact_digest_memo():
        assert node._node_digest() == outside
        assert node._node_digest() == outside
    assert len(reads) == 1


def test_harvest_uses_one_store_and_one_digest_scope(tmp_path, monkeypatch):
    """Harvest validation still runs per plan; only the shared reads are reused."""
    root = tmp_path / "fleet/profile-requalifications"
    plans = tmp_path / "fleet/test-plans"
    root.parent.mkdir(mode=0o700)
    plan.private_dir(root, create=True)
    plan.private_dir(plans, create=True)
    for cid in ("11111111", "22222222", "33333333"):
        plan.write_once(
            plans / f"{cid}-p.json",
            {
                "binding": {"source_card": cid, "source_owner": "producer"},
                "profile_requalification": True,
            },
        )
    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}")
    scopes = []

    def load_plan(*_args, **_kwargs):
        scopes.append(node._DIGEST_MEMO is not None)
        return (
            {
                "profile_requalification": True,
                "profile_predecessor_sha256": "other",
                "runtime_sha256": "r",
                "policy_sha256": "p",
            },
            plans / "x.json",
            "e" * 64,
        )

    monkeypatch.setattr(refresh.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(refresh.plan, "load_plan", load_plan)
    monkeypatch.setattr(refresh.plan, "run_directory", lambda *_a: run)
    monkeypatch.setattr(
        refresh.profile, "read_profile", lambda *_a, **_k: ({"repository": "r"}, "c" * 64)
    )
    cards = {
        cid: SimpleNamespace(status=SimpleNamespace(value="ready"), owner=None)
        for cid in ("11111111", "22222222", "33333333")
    }
    _CountingStore.instances = 0
    monkeypatch.setattr(refresh, "CardStore", lambda home: _CountingStore(home, cards))
    assert refresh.harvest_completed(tmp_path, {"authority_host": "chiap08"}) == "idle"
    assert scopes == [True, True, True]
    assert _CountingStore.instances == 1
    assert node._DIGEST_MEMO is None
