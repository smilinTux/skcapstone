"""Failed remote review retirement preserves exact evidence before reoffer."""

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_admission, review_dispatch, review_retire
from skcapstone.fleet.paths import FleetPaths


def test_collected_worker_journal_binds_one_exact_review_invocation(monkeypatch):
    unit = "skfleet-worker-codex-c60a542e.service"
    request_id = "a" * 64
    start = (
        "Started "
        + unit
        + " --card c60a542e --owner pi-seraph-chiap03-c60a542e"
        + " --claim-revision "
        + "b" * 32
        + " --host chiap03 --stdout /private/"
        + request_id
        + ".log"
    )
    rows = [
        {
            "MESSAGE": start,
            "__REALTIME_TIMESTAMP": "100",
            "_HOSTNAME": "chiap03",
            "USER_UNIT": unit,
            "USER_INVOCATION_ID": "c" * 32,
            "CODE_FUNC": "job_emit_done_message",
            "JOB_TYPE": "start",
            "JOB_RESULT": "done",
        },
        {
            "MESSAGE": unit + ": Consumed 22s CPU time.",
            "__REALTIME_TIMESTAMP": "200",
            "_HOSTNAME": "chiap03",
            "USER_UNIT": unit,
            "USER_INVOCATION_ID": "c" * 32,
            "CODE_FUNC": "unit_log_resources",
        },
    ]
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="\n".join(json.dumps(row) for row in rows)
        ),
    )
    evidence = review_retire._journal_terminal(
        unit,
        "chiap03",
        "c60a542e",
        "pi-seraph-chiap03-c60a542e",
        "b" * 32,
        request_id,
        "c" * 32,
    )
    assert evidence["request_id"] == request_id
    assert evidence["started_at_usec"] == 100
    assert evidence["completed_at_usec"] == 200
    with pytest.raises(ValueError, match="one exact collected worker invocation"):
        review_retire._journal_terminal(
            unit,
            "chiap03",
            "c60a542e",
            "pi-seraph-chiap03-c60a542e",
            "b" * 32,
            request_id,
            "d" * 32,
        )


@pytest.mark.parametrize("exit_mode", ["worker-exit", "systemd-terminal", "journal-terminal"])
def test_failed_review_retirement_requires_proof_and_archives_before_reoffer(
    tmp_path, monkeypatch, exit_mode
):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node, owner = "c60a542e", "node-chiap03", "pi-seraph-chiap03-c60a542e"
    request_id = "a" * 64
    request = {
        "schema": "skfleet.builder-dispatch/v2",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "reviewer": owner,
        "production": {"host": "chiap03", "family": "glm"},
    }
    status = {
        "schema": "skfleet.builder-dispatch-status/v1",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "state": "running",
        "owner": owner,
        "claim_revision": "b" * 32,
        "execution": {
            "host": "chiap03",
            "unit": "skfleet-worker-glm-c60a542e.service",
            "invocation": "c" * 32,
            "request_id": request_id,
            "request_sha256": review_retire.production_builder.digest(request),
        },
    }
    request_path = review_retire.dispatch.request_path(paths, node, card)
    status_path = review_retire.dispatch.status_path(paths, node, card)
    review_retire.source_bundle._once(request_path, json.dumps(request).encode())
    review_retire.source_bundle._once(status_path, json.dumps(status).encode())
    exit_path = home / "evidence/worker-exits" / (card + "-" + "d" * 16 + ".json")
    if exit_mode == "worker-exit":
        review_retire.source_bundle._once(
            exit_path,
            json.dumps(
                {
                    "card_id": card,
                    "owner": owner,
                    "claim_revision": "b" * 32,
                    "host": "chiap03",
                    "child_exit_code": 1,
                }
            ).encode(),
        )
    events = [
        {
            "action": "remote_review_offer",
            "request_id": request_id,
            "request_sha256": review_retire.production_builder.digest(request),
        },
        {
            "action": "review_assignment_launch",
            "recommendation_id": request_id,
            "claim_revision": "b" * 32,
            "execution": status["execution"],
            "writer": owner,
            "launched": True,
        },
    ]

    class FakeStore:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return SimpleNamespace(archived=False, meta={}, labels=[])

        def _read_events(self, _card):
            return events

        def append_event(self, _card, action, writer, **payload):
            events.append(dict(action=action, writer=writer, **payload))

    monkeypatch.setattr(review_retire, "CardStore", FakeStore)
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review_retire, "governed_review_assignment_ready", lambda *_: True)
    monkeypatch.setattr(review_retire, "review_state_revision", lambda *_: "e" * 64)
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(
        review_retire.production_builder, "node_binding", lambda *_: {"host": "chiap03"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        review_retire, "unit_terminal", lambda *_args, **_kw: {"ActiveState": "inactive"}
    )
    kwargs = dict(
        request_sha256=review_retire.source_bundle._sha(request_path.read_bytes()),
        status_sha256=review_retire.source_bundle._sha(status_path.read_bytes()),
        card_sha256="e" * 64,
        actor="jarvis",
        reason="failed before review custody",
    )
    with pytest.raises(ValueError, match="release proof missing"):
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert request_path.exists() and status_path.exists()
    events.append(
        {"action": "release_claim", "released_owner": owner, "expected_claim_revision": "b" * 32}
    )
    with pytest.raises(ValueError, match="hash changed"):
        review_retire.retire(
            paths,
            home,
            node,
            card,
            apply=True,
            **{**kwargs, "request_sha256": "f" * 64},
        )
    if exit_mode == "worker-exit":
        failed_exit = exit_path.read_bytes()
        exit_value = json.loads(failed_exit)
        exit_value["child_exit_code"] = 0
        exit_path.write_text(json.dumps(exit_value))
        with pytest.raises(ValueError, match="failed worker exit required"):
            review_retire.retire(paths, home, node, card, apply=True, **kwargs)
        exit_path.write_bytes(failed_exit)

    def live_unit(*_args, **_kwargs):
        raise ValueError("exact managed worker death unproven")

    monkeypatch.setattr(review_retire, "unit_terminal", live_unit)
    with pytest.raises(ValueError, match="death unproven"):
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert request_path.exists() and status_path.exists()
    unit_state = (
        {
            "LoadState": "loaded",
            "ActiveState": "failed",
            "SubState": "failed",
            "MainPID": "0",
            "ControlPID": "0",
            "InvocationID": "c" * 32,
            "Result": "exit-code",
            "ExecMainStatus": "143",
        }
        if exit_mode == "systemd-terminal"
        else (
            {"LoadState": "not-found", "ActiveState": "inactive"}
            if exit_mode == "journal-terminal"
            else {"ActiveState": "inactive"}
        )
    )
    monkeypatch.setattr(review_retire, "unit_terminal", lambda *_args, **_kw: unit_state)
    if exit_mode == "journal-terminal":
        monkeypatch.setattr(
            review_retire,
            "_prestart_state",
            lambda unit, host, card: {
                "unit": unit,
                "unit_state": {"LoadState": "not-found"},
                "matching_sessions": 0,
            },
        )
        monkeypatch.setattr(
            review_retire, "_admission_inventory", lambda *_args, **_kwargs: "f" * 64
        )
        monkeypatch.setattr(
            review_retire,
            "_journal_terminal",
            lambda unit, host, card, owner, claim, request_id, invocation: {
                "schema": "skfleet.collected-worker-terminal/v1",
                "unit": unit,
                "host": host,
                "card_id": card,
                "owner": owner,
                "claim_revision": claim,
                "request_id": request_id,
                "invocation": invocation,
                "started_at_usec": 10,
                "started_message_sha256": "a" * 64,
                "completed_at_usec": 20,
                "completed_message_sha256": "b" * 64,
            },
        )
    assert (
        review_retire.retire(paths, home, node, card, **kwargs)["state"] == "qualified-check-only"
    )
    assert request_path.exists() and status_path.exists()
    result = review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert result["state"] == "retired"
    assert not request_path.exists() and not status_path.exists()
    archive = Path(result["receipt"]).parent
    assert (
        archive / ("worker-exit.json" if exit_mode == "worker-exit" else "systemd-terminal.json")
    ).exists()
    assert review_retire.retired_offers(home, card, events) == {request_id}
    assert (
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)["state"]
        == "already-retired"
    )
    # Interrupted cleanup retains its exact event and can remove remaining pointers.
    request_path.write_bytes((archive / "request.json").read_bytes())
    request_path.chmod(0o600)
    assert (
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)["state"]
        == "already-retired"
    )
    assert not request_path.exists()
    (archive / "status.json").write_bytes(b"{}")
    assert review_retire.retired_offers(home, card, events) == set()


def test_retirement_without_archive_cannot_release_historical_offer(tmp_path):
    card, request_id = "c60a542e", "a" * 64
    events = [
        {"action": "remote_review_offer", "request_id": request_id, "request_sha256": "b" * 64},
        {
            "action": "remote_review_retire",
            "writer": "jarvis",
            "actor": "jarvis",
            "schema": review_retire.SCHEMA,
            "request_id": request_id,
            "request_sha256": "b" * 64,
            "offer_request_sha256": "e" * 64,
            "status_sha256": "c" * 64,
            "receipt_sha256": "d" * 64,
        },
    ]
    assert review_retire.retired_offers(tmp_path, card, events) == set()


@pytest.mark.parametrize(
    "retirement_mode",
    [
        "released",
        "expired-unclaimed",
        "unexpired-unclaimed",
        "claimed-after-offer",
        "reviewer-self-release-churn",
        "archived-orphaned-claim",
    ],
)
def test_prestart_retirement_requires_exact_release_or_expired_unclaimed_offer(
    tmp_path, monkeypatch, retirement_mode
):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node = "c60a542e", "node-chiap03"
    owner, claim, request_id = "pi-seraph-chiap03-c60a542e", "b" * 32, "a" * 64
    source = {"card": "3f9eaa96", "head": "c" * 40, "claim": "d" * 32}
    core = dict(
        title="[REVIEW] exact source",
        description="review exact candidate",
        acceptance_criteria=["verify exact bytes"],
        dependencies=[],
        links={"repository": "smilinTux/sklegal"},
        labels=["review", "source-only", "seat-seraph"],
    )
    request = {
        "schema": "skfleet.builder-dispatch/v2",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "reviewer": owner,
        "lease_expires_at": (
            "2000-01-01T00:00:00Z"
            if retirement_mode
            in {
                "expired-unclaimed",
                "claimed-after-offer",
                "reviewer-self-release-churn",
                "archived-orphaned-claim",
            }
            else "2999-01-01T00:00:00Z"
        ),
        "review_revision": "e" * 64,
        "source": source,
        "criteria_sha256": review_retire.production_builder.digest(core),
        "production": {"host": "chiap03", "family": "codex"},
        "labels": core["labels"],
    }
    request_path = review_retire.dispatch.request_path(paths, node, card)
    status_path = review_retire.dispatch.status_path(paths, node, card)
    review_retire.source_bundle._once(request_path, json.dumps(request).encode())
    raw_request = request_path.read_bytes()
    offer_digest = review_retire.production_builder.digest(request)
    events = [
        {
            "action": "remote_review_offer",
            "request_id": request_id,
            "request_sha256": offer_digest,
            "ts": "2026-10-08T12:00:00+00:00",
        },
    ]
    if retirement_mode == "released":
        events.append(
            {
                "action": "release_claim",
                "writer": "jarvis",
                "released_owner": owner,
                "expected_claim_revision": claim,
            }
        )
    elif retirement_mode == "claimed-after-offer":
        events.append(
            {
                "action": "claim",
                "writer": owner,
                "owner": owner,
                "claim_revision": claim,
                "ts": "2026-10-08T12:01:00+00:00",
            }
        )
    elif retirement_mode == "reviewer-self-release-churn":
        events.extend(
            [
                {
                    "action": "claim",
                    "writer": owner,
                    "owner": owner,
                    "claim_revision": claim,
                    "ts": "2026-10-08T12:01:00+00:00",
                },
                {
                    "action": "release_claim",
                    "writer": owner,
                    "released_owner": owner,
                    "expected_claim_revision": claim,
                    "ts": "2026-10-08T12:02:00+00:00",
                },
                {
                    "action": "move",
                    "writer": owner,
                    "column": "review",
                    "ts": "2026-10-08T12:02:01+00:00",
                },
                {
                    "action": "claim",
                    "writer": owner,
                    "owner": owner,
                    "claim_revision": "c" * 32,
                    "ts": "2026-10-08T12:03:00+00:00",
                },
                {
                    "action": "release_claim",
                    "writer": owner,
                    "released_owner": owner,
                    "expected_claim_revision": "c" * 32,
                    "ts": "2026-10-08T12:04:00+00:00",
                },
                {
                    "action": "move",
                    "writer": owner,
                    "column": "review",
                    "ts": "2026-10-08T12:04:01+00:00",
                },
            ]
        )
    elif retirement_mode == "archived-orphaned-claim":
        events.extend(
            [
                {
                    "action": "claim",
                    "writer": owner,
                    "owner": owner,
                    "claim_revision": "e" * 32,
                    "ts": "2026-10-08T12:01:00+00:00",
                },
                {
                    "action": "release_claim",
                    "writer": owner,
                    "released_owner": owner,
                    "expected_claim_revision": "e" * 32,
                    "ts": "2026-10-08T12:02:00+00:00",
                },
                {
                    "action": "void",
                    "writer": "jarvis",
                    "reason": "stale custody",
                    "ts": "2026-10-08T12:03:00+00:00",
                },
                {"action": "archive", "writer": "jarvis", "ts": "2026-10-08T12:04:00+00:00"},
                {
                    "action": "claim",
                    "writer": owner,
                    "owner": owner,
                    "claim_revision": claim,
                    "ts": "2026-10-08T12:05:00+00:00",
                },
            ]
        )
    current = SimpleNamespace(
        archived=retirement_mode == "archived-orphaned-claim",
        owner=None,
        meta={"voided": True} if retirement_mode == "archived-orphaned-claim" else {},
        **core,
    )
    current.model_dump = lambda **_kwargs: core

    class FakeStore:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return current

        def _read_events(self, _card):
            return events

        def append_event(self, _card, action, writer, **payload):
            events.append(dict(action=action, writer=writer, **payload))

    monkeypatch.setattr(review_retire, "CardStore", FakeStore)
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review_retire, "governed_review_assignment_ready", lambda *_: True)
    monkeypatch.setattr(review_retire, "review_state_revision", lambda *_: "f" * 64)
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(
        review_retire.production_builder, "node_binding", lambda *_args: {"host": "chiap03"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        review_retire,
        "_prestart_state",
        lambda *_args: {
            "unit": "skfleet-worker-codex-c60a542e.service",
            "unit_state": {"LoadState": "not-found"},
            "matching_sessions": 0,
        },
    )
    if retirement_mode == "archived-orphaned-claim":

        def unavailable_source(*_args):
            raise ValueError("original source custody unavailable")

        monkeypatch.setattr(review_dispatch, "_source", unavailable_source)
        monkeypatch.setattr(review_retire, "_superseded_source_matches", lambda *_args: True)
    else:
        monkeypatch.setattr(review_dispatch, "_source", lambda *_args: source)
    kwargs = dict(
        request_sha256=review_retire.source_bundle._sha(raw_request),
        card_sha256="f" * 64,
        previous_owner=(
            owner if retirement_mode in {"released", "archived-orphaned-claim"} else None
        ),
        previous_claim_revision=(
            claim if retirement_mode in {"released", "archived-orphaned-claim"} else None
        ),
        actor="jarvis",
        reason="retained sealed offer never reached native launch",
    )
    if retirement_mode == "unexpired-unclaimed":
        with pytest.raises(ValueError, match="offer has not expired"):
            review_retire.retire_prestart(paths, home, node, card, **kwargs)
        return
    if retirement_mode == "claimed-after-offer":
        with pytest.raises(ValueError, match="exact release and unused offer proof"):
            review_retire.retire_prestart(paths, home, node, card, **kwargs)
        return
    assert (
        review_retire.retire_prestart(paths, home, node, card, **kwargs)["state"]
        == "qualified-check-only"
    )
    assert request_path.exists() and not status_path.exists()
    result = review_retire.retire_prestart(paths, home, node, card, apply=True, **kwargs)
    assert result["state"] == "retired-prestart"
    assert not request_path.exists() and not status_path.exists()
    assert review_retire.retired_offers(home, card, events) == {request_id}


def test_prestart_retirement_refuses_when_status_or_launch_exists(tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node = "c60a542e", "node-chiap03"
    status = review_retire.dispatch.status_path(paths, node, card)
    status.parent.mkdir(parents=True)
    status.write_text("{}")
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    with pytest.raises(ValueError, match="status already exists"):
        review_retire.retire_prestart(
            paths,
            home,
            node,
            card,
            request_sha256="a" * 64,
            card_sha256="b" * 64,
            previous_owner="pi-seraph-chiap03-c60a542e",
            previous_claim_revision="c" * 32,
            actor="jarvis",
            reason="bounded test",
        )


def test_reviewer_self_release_churn_requires_exact_pairs_and_no_other_events():
    reviewer = "pi-seraph-chiap03-c60a542e"
    request_id = "a" * 64
    offer_at = "2026-10-08T12:00:00+00:00"
    offer = {"action": "remote_review_offer", "request_id": request_id, "ts": offer_at}
    recommendation = {
        "action": "review_assignment_recommendation",
        "writer": "link",
        "recommendation_id": request_id,
        "ts": "2026-10-08T12:00:30+00:00",
    }
    claim = {
        "action": "claim",
        "writer": reviewer,
        "owner": reviewer,
        "claim_revision": "b" * 32,
        "ts": "2026-10-08T12:01:00+00:00",
    }
    release = {
        "action": "release_claim",
        "writer": reviewer,
        "released_owner": reviewer,
        "expected_claim_revision": "b" * 32,
        "ts": "2026-10-08T12:02:00+00:00",
    }
    observation = {
        "action": "mero_observation",
        "writer": "mero",
        "schema": "skfleet.mero-observation/v1",
        "state": "worker_absent_after_quorum",
        "process": {"host": "chiap03", "sessions": [], "claim_revision": "b" * 32},
        "evidence_sha256": "c" * 64,
        "ts": "2026-10-08T12:01:30+00:00",
    }
    events = [offer, recommendation, claim, release]
    assert review_retire._reviewer_self_release_churn(events, offer_at, reviewer, request_id)
    assert review_retire._reviewer_self_release_churn(
        [offer, recommendation, claim, observation, release], offer_at, reviewer, request_id
    )
    quorum_release = [
        offer,
        recommendation,
        claim,
        observation,
        {**release, "writer": "niobe"},
        {
            "action": "move",
            "writer": "niobe",
            "column": "review",
            "ts": "2026-10-08T12:02:30+00:00",
        },
    ]
    assert review_retire._reviewer_self_release_churn(
        quorum_release, offer_at, reviewer, request_id
    )

    wrong_owner_release = [offer, recommendation, claim, {**release, "writer": "other-reviewer"}]
    assert not review_retire._reviewer_self_release_churn(
        wrong_owner_release, offer_at, reviewer, request_id
    )
    other_reviewer = "pi-seraph-chiap02-c60a542e"
    wrong_claim_owner = [
        offer,
        recommendation,
        {**claim, "writer": other_reviewer, "owner": other_reviewer},
        {
            **release,
            "writer": other_reviewer,
            "released_owner": other_reviewer,
        },
    ]
    assert not review_retire._reviewer_self_release_churn(
        wrong_claim_owner, offer_at, reviewer, request_id
    )
    unexpected_mutation = [*events, {"action": "label", "ts": "2026-10-08T12:03:00+00:00"}]
    assert not review_retire._reviewer_self_release_churn(
        unexpected_mutation, offer_at, reviewer, request_id
    )
    launch = [*events, {"action": "review_assignment_launch", "recommendation_id": request_id}]
    assert not review_retire._reviewer_self_release_churn(launch, offer_at, reviewer, request_id)
    unmatched_observation = [
        offer,
        recommendation,
        claim,
        {**observation, "process": {**observation["process"], "claim_revision": "d" * 32}},
        release,
    ]
    assert not review_retire._reviewer_self_release_churn(
        unmatched_observation, offer_at, reviewer, request_id
    )


def test_prestart_retirement_refuses_matching_resource_intent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    reservation = home / "fleet/resource-admission/chiap03" / ("a" * 64)
    reservation.mkdir(parents=True)
    (reservation / "intent.json").write_text("{}")
    intent = {
        "binding": {
            "card_id": "c60a542e",
            "owner": "pi-seraph-chiap03-c60a542e",
            "claim_revision": "b" * 32,
            "request_id": "c" * 64,
        }
    }
    from skcapstone.fleet import production_admission

    monkeypatch.setattr(production_admission, "read_json", lambda _path: intent)
    monkeypatch.setattr(production_admission, "_reservation_id", lambda _intent: "a" * 64)
    with pytest.raises(ValueError, match="matching resource admission intent exists"):
        review_retire._admission_inventory(
            home,
            "chiap03",
            "c60a542e",
            "pi-seraph-chiap03-c60a542e",
            "b" * 32,
            "c" * 64,
        )


def test_prestart_inventory_ignores_private_admission_lock(tmp_path):
    root = tmp_path / "home/fleet/resource-admission/chiap03"
    root.mkdir(parents=True)
    lock = root / ".lock"
    lock.touch(mode=0o600)

    digest = review_retire._admission_inventory(
        tmp_path / "home",
        "chiap03",
        "c60a542e",
        "pi-seraph-chiap03-c60a542e",
        "b" * 32,
        "c" * 64,
    )

    assert digest == review_retire.source_bundle._sha(b"[]")


def test_prestart_inventory_rejects_unsafe_admission_lock(tmp_path):
    root = tmp_path / "home/fleet/resource-admission/chiap03"
    root.mkdir(parents=True)
    target = tmp_path / "lock-target"
    target.touch(mode=0o600)
    (root / ".lock").symlink_to(target)

    with pytest.raises(ValueError, match="resource admission lock is unsafe"):
        review_retire._admission_inventory(
            tmp_path / "home",
            "chiap03",
            "c60a542e",
            "pi-seraph-chiap03-c60a542e",
            "b" * 32,
            "c" * 64,
        )


def test_admission_inventory_accepts_only_exact_journal_proven_review_intent(tmp_path):
    home = tmp_path / "home"
    host, card, owner, claim = "chiap03", "c60a542e", "reviewer", "b" * 32
    request_id, request_sha = "a" * 64, "c" * 64
    binding = {
        "card_id": card,
        "owner": owner,
        "claim_revision": claim,
        "request_id": request_id,
        "request_sha256": request_sha,
        "work_kind": "review",
    }
    unit = "skfleet-worker-glm-" + card + ".service"
    intent = {
        "schema": "skfleet.resource-admission/v1",
        "host": host,
        "unit": unit,
        "binding": binding,
        "resources": {"memory_max_bytes": 1024},
        "argv_sha256": "d" * 64,
    }
    identity = production_admission._reservation_id(intent)
    directory = home / "fleet/resource-admission" / host / identity
    directory.mkdir(parents=True)
    private_paths = (
        home,
        home / "fleet",
        home / "fleet/resource-admission",
        directory.parent,
        directory,
    )
    for path in private_paths:
        path.chmod(0o700)
    intent_path = directory / "intent.json"
    intent_path.write_text(json.dumps(intent))
    start_path = directory / "start.json"
    start_path.write_text(
        json.dumps(
            {
                "schema": "skfleet.resource-start/v1",
                "reservation_id": identity,
                "binding": binding,
                "argv_sha256": intent["argv_sha256"],
            }
        )
    )
    intent_path.chmod(0o600)
    start_path.chmod(0o600)
    proof = {
        "schema": "skfleet.collected-worker-terminal/v1",
        "unit": unit,
        "host": host,
        "card_id": card,
        "owner": owner,
        "claim_revision": claim,
        "request_id": request_id,
    }
    assert (
        len(
            review_retire._admission_inventory(
                home, host, card, owner, claim, request_id, terminal=proof, unit=unit
            )
        )
        == 64
    )
    with pytest.raises(ValueError, match="matching resource admission intent"):
        review_retire._admission_inventory(home, host, card, owner, claim, request_id)


def test_superseded_archived_offer_source_matches_exact_prior_candidate(tmp_path, monkeypatch):
    parent, head, owner, claim = (
        "9b5acdd3",
        "5cd8c90b1eceb6d02e4ef17c3cd277c96235a5e6",
        "pi-codex-builder-node-chiap03-9b5acdd3",
        "ed5db482601c42db9d9391d626dca8fb",
    )
    manifest = {
        "schema": "skfleet.source-bundle/v1",
        "card": parent,
        "head": head,
        "owner": owner,
        "claim_revision": claim,
        "tree": "a7f1d198473f847858cbd8172edbc2dbdb1030ea",
        "evidence_sha256": "a723bef1e7671e801cd3cf5ce08ba0ac9380660feadf2078cb971b86b6542292",
        "ref": "refs/heads/feat/9b5acdd3-bound-git-bundle-threads",
    }
    core = {
        "meta": {
            "link_source_card": parent,
            "link_head_revision": head,
            "producer_identity": owner,
            "candidate_evidence_sha256": manifest["evidence_sha256"],
            "source_revision": "955ebc15c4d291aefd11b22b6a43119453b6f023291336562715233f4184588a",
        },
        "links": {"repository": "https://github.com/smilinTux/skcapstone"},
    }
    request_source = {
        "card": parent,
        "claim": claim,
        "evidence_sha256": manifest["evidence_sha256"],
        "family": "codex",
        "head": head,
        "manifest_sha256": review_retire.production_builder.digest(manifest),
        "owner": owner,
        "revision": core["meta"]["source_revision"],
        "tree": manifest["tree"],
    }
    prior = {
        "event_id": "a" * 32,
        "ts": "2026-10-08T16:43:00+00:00",
        "action": "verdict",
        "verdict": "PASS_FOR_REVIEW",
        "writer": owner,
        "expected_claim_revision": claim,
        "candidate_commit": head,
        "candidate_tree": manifest["tree"],
        "candidate_sha256": manifest["evidence_sha256"],
        "candidate_ref": manifest["ref"],
    }
    supersession = {
        "ts": "2026-10-08T17:03:00+00:00",
        "action": "link",
        "link_key": "verdict_superseded",
        "link_value": "SUPERSEDED prior_event=" + "a" * 32 + " reason=custody lost",
    }
    events = [prior, supersession]
    source_card = SimpleNamespace(
        archived=False,
        owner="pi-glm-builder-node-chiap03-9b5acdd3",
        status=SimpleNamespace(value="doing"),
    )

    class FakeStore:
        def __init__(self, _home):
            pass

        def fold(self, card_id):
            return source_card if card_id == parent else None

        def _read_events(self, _card_id):
            return events

        def _legacy_events(self, _card_id):
            return []

    monkeypatch.setattr(review_retire, "CardStore", FakeStore)
    monkeypatch.setattr(
        review_retire.source_bundle,
        "_review_manifest",
        lambda *_args: (manifest, tmp_path / "source.bundle"),
    )
    review_card = SimpleNamespace(model_dump=lambda **_kwargs: core)
    assert review_retire._superseded_source_matches(
        tmp_path, review_card, {"source": request_source}
    )

    prior["candidate_tree"] = "0" * 40
    assert not review_retire._superseded_source_matches(
        tmp_path, review_card, {"source": request_source}
    )
