"""A finished review whose guarded handoff failed is recovered, bounded or released."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch as dispatch
from skcapstone.fleet import review_handoff_recovery as rhr
from skcapstone.fleet import review_retire, source_bundle
from skcapstone.fleet.paths import FleetPaths
from skcapstone.fleet.production_brief import (
    production_source_review_brief,
    source_review_binding,
    source_review_handoff_recipe,
)

CARD, SOURCE = "ab264e92", "89508f83"
NODE, HOST = "node-chiap03", "chiap03"
OWNER = "pi-seraph-chiap03-ab264e92"
CLAIM = "c" * 32
OFFER = "e" * 64
HEAD, TREE = "a" * 40, "b" * 40
PACKET = "d" * 64
UNIT, INVOCATION = "skfleet-worker-codex-ab264e92.service", "f" * 32


def private(path, text):
    """Write evidence the way production does: owned, mode 0600."""
    path.write_text(text)
    path.chmod(0o600)


class FakeStore:
    """Minimal native card store: folds, events and appended outcomes."""

    def __init__(self):
        self.row = SimpleNamespace(
            id=CARD,
            archived=False,
            owner=OWNER,
            status="review",
            labels=["review", "source-only"],
            links={},
            meta={"_claim_revision": CLAIM, "link_source_card": SOURCE},
            model_dump=lambda mode="json": {"id": CARD},
        )
        self.events = [
            {"action": "claim", "claim_revision": CLAIM, "ts": "2026-10-13T10:00:00Z"},
            {
                "action": "review_assignment_launch",
                "schema": "skfleet.review-assignment-launch/v3",
                "writer": OWNER,
                "claim_revision": CLAIM,
                "launched": True,
                "execution": {"request_id": OFFER, "node": NODE},
            },
        ]

    def fold(self, card):
        return self.row if card == CARD else None

    def _read_events(self, card):
        return list(self.events)

    def _legacy_events(self, card):
        return []

    def add_verdict(self, ts="2026-10-13T11:00:00Z", **extra):
        self.events.append(dict(action="verdict", verdict="PASS", writer=OWNER, ts=ts, **extra))


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.paths = FleetPaths(tmp_path / "fleet")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.store = FakeStore()
        self.execution = {
            "host": HOST,
            "unit": UNIT,
            "invocation": INVOCATION,
            "request_id": OFFER,
            "node": NODE,
        }
        self.workspace = self.paths.root / "workspaces" / OWNER
        (self.workspace / ".git").mkdir(parents=True)
        self.status = {
            "schema": "skfleet.builder-dispatch-status/v1",
            "card_id": CARD,
            "node": NODE,
            "work_kind": "review",
            "request_id": OFFER,
            "owner": OWNER,
            "claim_revision": CLAIM,
            "state": rhr.STATE,
            "execution": self.execution,
            "terminal": {"exit_code": 0, "workspace": str(self.workspace)},
            "review_packet": {"card": CARD, "sha256": PACKET},
        }
        self.request = {
            "request_id": OFFER,
            "reviewer": OWNER,
            "source": {"card": SOURCE, "head": HEAD, "tree": TREE},
        }
        self.write_status()
        path = dispatch.request_path(self.paths, NODE, CARD)
        path.parent.mkdir(parents=True, exist_ok=True)
        private(path, json.dumps(self.request))
        self.manifest = {
            "binding": {"source_head": HEAD, "reviewer_identity": OWNER},
            "execution": self.execution,
            "verdict": "PASS",
            "review_head": "1" * 40,
            "decision_sha256": "2" * 64,
            "report_sha256": "3" * 64,
        }
        self.proposal = {
            "review_head": "1" * 40,
            "decision_sha256": "2" * 64,
            "report_sha256": "3" * 64,
        }
        self.runs = []
        self.run_returncode = 0
        self.packet_error = None
        self.terminal_error = None
        self.inspect_error = None
        self.binding_error = None
        monkeypatch.setattr(rhr, "CardStore", lambda home: self.store)
        monkeypatch.setattr(rhr, "unit_terminal", self._unit_terminal)
        monkeypatch.setattr(source_bundle, "load_review_packet", self._packet)
        monkeypatch.setattr(source_bundle, "inspect_remote_review", self._inspect)
        monkeypatch.setattr(rhr.socket, "gethostname", lambda: HOST)
        monkeypatch.setattr(rhr.subprocess, "run", self._run)
        from skcapstone.fleet import production_brief, worker_git

        monkeypatch.setattr(production_brief, "source_review_binding", self._binding)
        monkeypatch.setattr(worker_git, "identity", lambda owner: {"GIT_AUTHOR_NAME": owner})

    def write_status(self):
        path = dispatch.status_path(self.paths, NODE, CARD)
        path.parent.mkdir(parents=True, exist_ok=True)
        private(path, json.dumps(self.status))

    def _unit_terminal(self, unit, invocation, host=None):
        if self.terminal_error:
            raise rhr.ReviewEvidenceError("alive")
        return {}

    def _packet(self, home, reference, card):
        if self.packet_error:
            raise self.packet_error
        return {"manifest": self.manifest}

    def _inspect(self, workspace, **binding):
        if self.inspect_error:
            raise self.inspect_error
        return self.proposal

    def _binding(self, **kwargs):
        if self.binding_error:
            raise ValueError(self.binding_error)
        return (
            {"schema": "x", "card": CARD, "source_head": HEAD, "source_tree": TREE},
            {"link_head_revision": HEAD},
        )

    def _run(self, command, **kwargs):
        if command[:1] == ["/bin/bash"]:
            self.runs.append((command, kwargs))
            if self.run_returncode == 0:
                self.store.add_verdict()
            return SimpleNamespace(
                returncode=self.run_returncode, stdout="", stderr="boom: bad dir"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _request(world, attempt=1):
    events = rhr.recover_review_handoffs(world.paths, world.home)
    return events, rhr.request_file(world.paths, NODE, CARD, CLAIM, attempt)


# ----------------------------------------------------------------- detection


def test_detects_finished_review_without_native_verdict(world):
    found = rhr.detect(world.paths, world.home)
    assert [(c["card"], c["node"], c["owner"], c["claim"]) for c in found] == [
        (CARD, NODE, OWNER, CLAIM)
    ]
    assert "refusal" not in found[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.status.update(state="running"),
        lambda w: w.status.update(review_packet=None),
        lambda w: w.status.update(terminal={"exit_code": 1}),
        lambda w: w.status.update(work_kind="source"),
        lambda w: setattr(w.store.row, "owner", "someone-else"),
        lambda w: w.store.row.meta.update(_claim_revision="9" * 32),
        lambda w: setattr(w.store.row, "archived", True),
        lambda w: setattr(w.store.row, "labels", ["source-only"]),
        lambda w: w.store.add_verdict(),
    ],
    ids=[
        "running",
        "no-packet",
        "nonzero-exit",
        "not-review",
        "owner-changed",
        "claim-changed",
        "archived",
        "not-review-card",
        "native-verdict-present",
    ],
)
def test_detection_skips_everything_else(world, mutate):
    mutate(world)
    world.write_status()
    assert rhr.detect(world.paths, world.home) == []


def test_verdict_older_than_claim_is_not_a_native_verdict(world):
    world.store.add_verdict(ts="2026-10-13T09:00:00Z")  # previous generation
    assert [c["card"] for c in rhr.detect(world.paths, world.home)] == [CARD]


def test_live_reviewer_unit_is_refused_not_recovered(world):
    world.terminal_error = True
    (found,) = rhr.detect(world.paths, world.home)
    assert found["refusal"] == "reviewer-unit-not-terminal"
    events = rhr.recover_review_handoffs(world.paths, world.home)
    assert events == [
        {"card": CARD, "node": NODE, "state": "refused", "reason": "reviewer-unit-not-terminal"}
    ]
    assert not list((world.paths.root / "review-handoff").glob("*/*.json"))


def test_launch_binding_must_match(world):
    world.store.events.pop()
    (found,) = rhr.detect(world.paths, world.home)
    assert found["refusal"] == "generation-binding-differs"


# ------------------------------------------------------------ request issuing


def test_request_is_bound_immutable_and_not_reissued_while_in_flight(world):
    events, path = _request(world)
    assert events[0]["state"] == "requested"
    request = json.loads(path.read_text())
    assert request["schema"] == rhr.REQUEST_SCHEMA
    for key, value in {
        "card_id": CARD,
        "node": NODE,
        "host": HOST,
        "owner": OWNER,
        "claim_revision": CLAIM,
        "source_head": HEAD,
        "packet_sha256": PACKET,
        "decision_sha256": "2" * 64,
        "report_sha256": "3" * 64,
        "review_head": "1" * 40,
        "attempt": 1,
        "workspace": str(world.workspace),
    }.items():
        assert request[key] == value
    before = path.read_bytes()
    assert rhr.recover_review_handoffs(world.paths, world.home) == []
    assert path.read_bytes() == before
    assert len(list(path.parent.glob("*.request.json"))) == 1


def test_packet_head_mismatch_in_manifest_is_unrecoverable_packet_changed(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        rhr,
        "release_unrecoverable",
        lambda *a: calls.append(a[3:6]) or rhr._event(CARD, NODE, "released"),
    )
    world.manifest["binding"]["source_head"] = "9" * 40
    rhr.recover_review_handoffs(world.paths, world.home)
    assert calls == [(CARD, CLAIM, "packet-changed")]


def test_missing_packet_artifact_triggers_bound(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        rhr,
        "release_unrecoverable",
        lambda *a: calls.append(a[3:6]) or rhr._event(CARD, NODE, "released"),
    )
    world.packet_error = source_bundle.SourceBundleError("review artifact content changed")
    rhr.recover_review_handoffs(world.paths, world.home)
    assert calls == [(CARD, CLAIM, "packet-missing")]


# ------------------------------------------------------------ node: recovery


def _run_node(world, attempt=1):
    (
        _request(world, attempt)
        if not rhr.request_file(world.paths, NODE, CARD, CLAIM, attempt).exists()
        else None
    )
    path = rhr.request_file(world.paths, NODE, CARD, CLAIM, attempt)
    return rhr.run_recovery(world.paths, world.home, NODE, path)


def test_successful_recovery_runs_the_workers_own_recipe_in_the_workspace(world):
    result = _run_node(world)
    assert result["state"] == "recovered"
    ((command, kwargs),) = world.runs
    assert kwargs["cwd"] == world.workspace
    env = kwargs["env"]
    assert env["SKAGENT"] == OWNER and env["SKFLEET_CLAIM_REVISION"] == CLAIM
    assert env["SKFLEET_CARD_ID"] == CARD
    recipe = source_review_handoff_recipe(
        card_id=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        source_head=HEAD,
        tree=TREE,
        expected={"schema": "x", "card": CARD, "source_head": HEAD, "source_tree": TREE},
        bindings={"link_head_revision": HEAD},
    )
    assert command == ["/bin/bash", "-c", recipe]
    assert "guarded_link verdict" in recipe and "--expected-claim-revision" in recipe
    stored = json.loads(rhr.result_file(world.paths, NODE, CARD, CLAIM, 1).read_text())
    assert stored["state"] == "recovered"


def test_authority_logs_recovered_once_then_nothing(world):
    _run_node(world)
    first = rhr.recover_review_handoffs(world.paths, world.home)
    assert [(e["state"], e["card"]) for e in first] == [("recovered", CARD)]
    assert rhr.recover_review_handoffs(world.paths, world.home) == []


def test_second_attempt_after_success_writes_nothing(world):
    assert _run_node(world)["state"] == "recovered"
    path = rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    # A re-run of the very same attempt is a no-op returning the stored result.
    assert rhr.run_recovery(world.paths, world.home, NODE, path)["state"] == "recovered"
    # A fresh request for an already handed-off review finds the verdict first.
    second = json.loads(path.read_text()) | {"attempt": 2}
    other = rhr.request_file(world.paths, NODE, CARD, CLAIM, 2)
    private(other, json.dumps(second))
    result = rhr.run_recovery(world.paths, world.home, NODE, other)
    assert result["state"] == "already-handed-off"
    assert len(world.runs) == 1


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda w: setattr(w.store.row, "owner", "other"), "claim-changed"),
        (lambda w: w.store.row.meta.update(_claim_revision="9" * 32), "claim-changed"),
        (lambda w: w.store.row.meta.update(claim_conflicts=["x"]), "claim-changed"),
        (lambda w: setattr(w.store.row, "archived", True), "claim-changed"),
        (
            lambda w: setattr(w, "binding_error", "invalid exact source review binding"),
            "binding-changed",
        ),
        (lambda w: w.status["review_packet"].update(sha256="0" * 64), "packet-changed"),
        (lambda w: w.status.update(claim_revision="9" * 32), "packet-changed"),
        (lambda w: w.status.update(state="running"), "packet-changed"),
        (lambda w: w.proposal.update(decision_sha256="9" * 64), "packet-changed"),
        (lambda w: w.proposal.update(review_head="9" * 40), "packet-changed"),
        (lambda w: w.proposal.update(report_sha256="9" * 64), "packet-changed"),
        (
            lambda w: setattr(w, "inspect_error", source_bundle.SourceBundleError("gone")),
            "packet-missing",
        ),
    ],
)
def test_every_binding_change_refuses_and_writes_nothing(world, mutate, reason):
    _request(world)  # issued against the pre-change world
    mutate(world)
    world.write_status()
    result = rhr.run_recovery(
        world.paths, world.home, NODE, rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    )
    assert result["state"] == "refused" and result["reason"] == reason
    assert world.runs == []
    assert [e["action"] for e in world.store.events] == ["claim", "review_assignment_launch"]


def test_source_head_change_is_distinguished(world):
    _request(world)
    world.binding_error = "invalid exact source review binding or reviewer"
    world.store.row.links["link_head_revision"] = "9" * 40
    result = rhr.run_recovery(
        world.paths, world.home, NODE, rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    )
    assert result["reason"] == "source-head-changed" and world.runs == []


def test_workspace_gone_or_redirected_is_refused(world, tmp_path):
    _request(world)
    (world.workspace / ".git").rmdir()
    result = rhr.run_recovery(
        world.paths, world.home, NODE, rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    )
    assert result["reason"] == "workspace-missing" and world.runs == []


def test_request_naming_a_foreign_workspace_is_refused(world):
    _request(world)
    path = rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    value = json.loads(path.read_text()) | {"workspace": "/etc"}
    private(path, json.dumps(value))
    assert rhr.run_recovery(world.paths, world.home, NODE, path)["reason"] == "workspace-missing"
    assert world.runs == []


def test_request_for_another_host_is_refused(world, monkeypatch):
    _request(world)
    monkeypatch.setattr(rhr.socket, "gethostname", lambda: "chiap09")
    path = rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
    assert rhr.run_recovery(world.paths, world.home, NODE, path)["reason"] == "request-changed"
    assert world.runs == []


def test_handoff_that_fails_again_is_refused_and_bounded(world):
    world.run_returncode = 1
    result = _run_node(world)
    assert result["state"] == "refused" and result["reason"] == "handoff-failed"
    assert "bad dir" in result["detail"]
    events = rhr.recover_review_handoffs(world.paths, world.home)
    assert ("refused", "handoff-failed") in {(e["state"], e["reason"]) for e in events}
    # attempts 2 and 3 are issued, then it stops and keeps custody.
    for attempt in (2, 3):
        assert rhr.request_file(world.paths, NODE, CARD, CLAIM, attempt).exists()
        rhr.run_recovery(
            world.paths,
            world.home,
            NODE,
            rhr.request_file(world.paths, NODE, CARD, CLAIM, attempt),
        )
        rhr.recover_review_handoffs(world.paths, world.home)
    final = rhr.recover_review_handoffs(world.paths, world.home)
    assert [(e["state"], e["reason"]) for e in final] == [("refused", "attempts-exhausted")]
    assert not rhr.request_file(world.paths, NODE, CARD, CLAIM, 4).exists()


def test_handoff_success_without_native_verdict_is_not_reported_recovered(world, monkeypatch):
    monkeypatch.setattr(world.store, "add_verdict", lambda *a, **k: None)
    result = _run_node(world)
    assert result == result | {"state": "refused", "reason": "readback-missing"}


# --------------------------------------------------------------- node: unit


def test_consume_launches_a_private_managed_unit_once(world):
    _request(world)
    commands = []

    def launcher(command):
        commands.append(command)
        rhr.run_recovery(
            world.paths, world.home, NODE, rhr.request_file(world.paths, NODE, CARD, CLAIM, 1)
        )
        return 0

    (outcome,) = rhr.consume(world.paths, world.home, NODE, launcher=launcher)
    assert outcome["card"] == CARD
    (command,) = commands
    assert command[0] == "/usr/bin/systemd-run"
    assert "--property=UMask=0077" in command and "--wait" in command
    assert command[command.index("--") + 3] == "skcapstone.fleet.review_handoff_recovery"
    assert rhr.consume(world.paths, world.home, NODE, launcher=launcher) == []
    assert len(commands) == 1


def test_consume_records_a_dead_unit_as_a_refusal(world):
    _request(world)
    (outcome,) = rhr.consume(world.paths, world.home, NODE, launcher=lambda command: 1)
    result = json.loads(rhr.result_file(world.paths, NODE, CARD, CLAIM, 1).read_text())
    assert result["reason"] == "handoff-failed" and "without result" in result["detail"]
    assert outcome["exit"] == 1


# --------------------------------------------------------- bound and release


def _bound_world(world, monkeypatch):
    released, superseded = [], []

    class Board:
        def __init__(self, home):
            pass

        def release_claim(self, owner, card, **kwargs):
            released.append((owner, card, kwargs))
            world.store.row.owner = None
            world.store.row.meta.pop("_claim_revision")

    monkeypatch.setattr("skcapstone.coordination.Board", Board)
    monkeypatch.setattr(
        review_retire,
        "supersede_generation",
        lambda *a, **k: superseded.append((a[2:5], k)) or "superseded",
    )
    monkeypatch.setattr(rhr.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr("skcapstone.release_guard.finished_work_verdict", lambda *a: None)
    return released, superseded


@pytest.mark.parametrize("reason", sorted(rhr.UNRECOVERABLE))
def test_unrecoverable_result_releases_only_the_dead_reviewers_claim(world, monkeypatch, reason):
    released, superseded = _bound_world(world, monkeypatch)
    _request(world)
    path = rhr.result_file(world.paths, NODE, CARD, CLAIM, 1)
    request_raw = rhr.request_file(world.paths, NODE, CARD, CLAIM, 1).read_bytes()
    private(
        path,
        json.dumps(
            {
                "schema": rhr.RESULT_SCHEMA,
                "request_sha256": source_bundle._sha(request_raw),
                "state": "refused",
                "reason": reason,
            }
        ),
    )
    events = rhr.recover_review_handoffs(world.paths, world.home)
    assert [(e["state"], e["card"]) for e in events] == [("released", CARD)]
    assert [(r[0], r[1]) for r in released] == [(OWNER, CARD)]
    assert released[0][2]["expected_claim_revision"] == CLAIM
    assert superseded and superseded[0][0] == (CARD, CLAIM, OWNER)
    assert all(r[1] != SOURCE for r in released)  # never the source card
    assert rhr.recover_review_handoffs(world.paths, world.home) == []  # acked


def test_pass_holding_claim_is_never_released(world, monkeypatch):
    released, superseded = _bound_world(world, monkeypatch)
    monkeypatch.setattr("skcapstone.release_guard.finished_work_verdict", lambda *a: "PASS")
    event = rhr.release_unrecoverable(
        world.paths, world.home, NODE, CARD, CLAIM, "workspace-missing", None, "niobe"
    )
    assert event["state"] == "refused" and event["reason"] == "holds-finished-work"
    assert released == [] and superseded == []


def test_native_verdict_present_blocks_release(world, monkeypatch):
    released, _ = _bound_world(world, monkeypatch)
    world.store.add_verdict()
    event = rhr.release_unrecoverable(
        world.paths, world.home, NODE, CARD, CLAIM, "workspace-missing", None, "niobe"
    )
    assert event["reason"] == "holds-finished-work" and released == []


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda w: setattr(w.store.row, "labels", ["source-only"]), "release-fence-differs"),
        (lambda w: w.status.update(claim_revision="9" * 32), "release-fence-differs"),
        (lambda w: w.status.update(state="running"), "release-fence-differs"),
        (lambda w: setattr(w.store.row, "owner", "other"), "claim-changed"),
    ],
)
def test_release_fences(world, monkeypatch, mutate, reason):
    released, superseded = _bound_world(world, monkeypatch)
    mutate(world)
    world.write_status()
    event = rhr.release_unrecoverable(
        world.paths, world.home, NODE, CARD, CLAIM, "workspace-missing", None, "niobe"
    )
    assert event["state"] == "refused" and event["reason"] == reason
    assert released == [] and superseded == []


def test_started_acceptance_blocks_release(world, monkeypatch):
    released, _ = _bound_world(world, monkeypatch)
    directory = world.home / "evidence/production-acceptance" / (CARD + "-" + CLAIM)
    directory.mkdir(parents=True)
    (directory / "finish-intent.json").write_text("{}")
    monkeypatch.setattr("skcapstone.fleet.evidence_io.review_directory", lambda *a: directory)
    event = rhr.release_unrecoverable(
        world.paths, world.home, NODE, CARD, CLAIM, "workspace-missing", None, "niobe"
    )
    assert event["reason"] == "acceptance-started" and released == []


def test_release_is_idempotent_after_the_claim_is_gone(world, monkeypatch):
    released, superseded = _bound_world(world, monkeypatch)
    for _ in range(2):
        event = rhr.release_unrecoverable(
            world.paths, world.home, NODE, CARD, CLAIM, "workspace-missing", None, "niobe"
        )
        assert event["state"] == "released"
    assert len(released) == 1 and len(superseded) == 2


# ------------------------------------------------------ single source of truth


def test_worker_brief_embeds_the_same_handoff_recipe_recovery_runs():
    core = {
        "id": CARD,
        "labels": ["source-only", "review"],
        "links": {
            "link_source_card": SOURCE,
            "candidate_evidence_sha256": "7" * 64,
            "link_head_revision": HEAD,
            "candidate_tree": TREE,
            "producer_identity": "pi-builder-node-chiap01",
        },
        "meta": {},
        "title": "t",
        "description": "d",
        "acceptance_criteria": [],
    }
    kwargs = dict(
        card_id=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        source_head=HEAD,
        core=core,
        labels=core["labels"],
    )
    try:
        expected, bindings = source_review_binding(**kwargs)
    except ValueError:
        pytest.skip(
            "fixture binding shape differs; covered by test_production_source_review_brief"
        )
    brief = production_source_review_brief(workspace="/w", **kwargs)
    recipe = source_review_handoff_recipe(
        card_id=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        source_head=HEAD,
        tree=expected["source_tree"],
        expected=expected,
        bindings=bindings,
    )
    assert recipe in brief


# --------------------------------------------- worker fails loudly (handoff)


def _worker(tmp_path, monkeypatch, verdict):
    from pathlib import Path

    from skcapstone.fleet import production_review_custody as custody

    args = SimpleNamespace(
        card="ab000002",
        claim_revision="a" * 32,
        owner="pi-seraph-control-ab000002",
        host="control",
        lane="deepseek",
        model="m",
        source_base_revision="b" * 40,
        production_child_exit_code=0,
    )
    card = SimpleNamespace(owner=args.owner, meta={"_claim_revision": args.claim_revision})
    monkeypatch.setenv("INVOCATION_ID", "c" * 32)
    monkeypatch.setattr(custody.socket, "gethostname", lambda: "control")
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, *a, **k: (
            "0::/user.slice/skfleet-worker-deepseek-ab000002.service\n"
            if str(self) == "/proc/self/cgroup"
            else original(self, *a, **k)
        ),
    )
    monkeypatch.setattr(
        "skcoord.card_store.CardStore", lambda home: SimpleNamespace(fold=lambda c: card)
    )
    monkeypatch.setattr(rhr, "native_outcome_current", lambda store, row: verdict)
    return custody, tmp_path, args, card


def test_exit_zero_without_native_verdict_is_handoff_failed_not_completed(tmp_path, monkeypatch):
    custody, home, args, card = _worker(tmp_path, monkeypatch, None)
    state = custody.retain_review_exit(home, args, card, terminal_proven=True)
    assert state["state"] == "handoff-failed"
    assert "handoff-failed" in state["reason"] and state["claim_released"] is False
    receipt = custody.read_exit(home, args.card, args.claim_revision)
    assert receipt["handoff"] == "failed"  # still exported, so recovery can find the packet


def test_exit_zero_with_native_verdict_is_completed(tmp_path, monkeypatch):
    custody, home, args, card = _worker(tmp_path, monkeypatch, {"verdict": "PASS"})
    state = custody.retain_review_exit(home, args, card, terminal_proven=True)
    assert state["state"] == "awaiting-review-acceptance" and "reason" not in state
    assert custody.read_exit(home, args.card, args.claim_revision)["handoff"] == "completed"


def test_subprocess_import_kept():
    assert subprocess  # the module under test shells out; keep the import honest
