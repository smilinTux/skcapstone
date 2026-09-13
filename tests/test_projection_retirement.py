from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli import main
from skcapstone.coord_amendments import void_card
from skcapstone.coordination import AgentFile, Board
from skcapstone.projection_retirement import card_generation_sha256, retire_projection


def _world(tmp_path, *, void=True):
    store = CardStore(tmp_path)
    store.create(CardCore(id="deadbeef", kind="task", title="void", created_by="test"))
    if void:
        void_card(tmp_path, "deadbeef", reason="superseded", agent="test")
    board = Board(tmp_path)
    board.save_agent(
        AgentFile(
            agent="pi-test-deadbeef",
            current_task="deadbeef",
            claimed_tasks=["deadbeef"],
        )
    )
    projection = board.agents_dir / "pi-test-deadbeef.json"
    payload = json.loads(projection.read_text())
    payload["last_seen"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    projection.write_text(json.dumps(payload))
    return store, projection


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_retires_exact_ownerless_void_projection_and_restores(tmp_path):
    store, projection = _world(tmp_path)
    result = retire_projection(
        tmp_path,
        task_id="deadbeef",
        projection_agent="pi-test-deadbeef",
        expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
        expected_projection_sha256=_sha(projection),
        actor="repair",
    )

    assert not projection.exists()
    assert result.quarantined_path.is_file()
    assert result.projection_sha256 == _sha(result.quarantined_path)
    restored = result.restore(expected_quarantine_sha256=result.projection_sha256)
    assert restored == projection
    assert projection.is_file()


@pytest.mark.parametrize("fence", ["card", "projection"])
def test_stale_hash_fence_refuses_without_mutation(tmp_path, fence):
    store, projection = _world(tmp_path)
    card_hash = card_generation_sha256(store.fold("deadbeef"))
    projection_hash = _sha(projection)

    with pytest.raises(ValueError, match=f"{fence} hash conflict"):
        retire_projection(
            tmp_path,
            task_id="deadbeef",
            projection_agent="pi-test-deadbeef",
            expected_card_sha256="0" * 64 if fence == "card" else card_hash,
            expected_projection_sha256="0" * 64 if fence == "projection" else projection_hash,
            actor="repair",
        )

    assert projection.is_file()


def test_owned_card_refuses(tmp_path):
    store, projection = _world(tmp_path, void=False)
    store.append_event("deadbeef", "claim", "owner", owner="owner")
    with pytest.raises(ValueError, match="ownerless"):
        retire_projection(
            tmp_path,
            task_id="deadbeef",
            projection_agent="pi-test-deadbeef",
            expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
            expected_projection_sha256=_sha(projection),
            actor="repair",
        )


def test_task_mismatch_and_malformed_identity_refuse(tmp_path):
    store, projection = _world(tmp_path)
    payload = json.loads(projection.read_text())
    payload["current_task"] = "cafebabe"
    projection.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="task mismatch"):
        retire_projection(
            tmp_path,
            task_id="deadbeef",
            projection_agent="pi-test-deadbeef",
            expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
            expected_projection_sha256=_sha(projection),
            actor="repair",
        )
    with pytest.raises(ValueError, match="projection identity"):
        retire_projection(
            tmp_path,
            task_id="deadbeef",
            projection_agent="../pi-test-deadbeef",
            expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
            expected_projection_sha256=_sha(projection),
            actor="repair",
        )


def test_destination_conflict_refuses(tmp_path):
    store, projection = _world(tmp_path)
    destination = tmp_path / "agents-quarantine" / projection.name
    destination.parent.mkdir()
    destination.write_text("existing")
    with pytest.raises(ValueError, match="destination exists"):
        retire_projection(
            tmp_path,
            task_id="deadbeef",
            projection_agent="pi-test-deadbeef",
            expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
            expected_projection_sha256=_sha(projection),
            actor="repair",
        )


def test_cli_requires_both_hash_fences(tmp_path):
    _world(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "retire-ownerless-projection",
            "deadbeef",
            "--projection-agent",
            "pi-test-deadbeef",
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "expected-card-sha256" in result.output
