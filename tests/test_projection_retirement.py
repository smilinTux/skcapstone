"""Source-only retirement boundary for the immutable SKCoord generation broker."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli import main
from skcapstone.coord_amendments import void_card
from skcapstone.coordination import AgentFile, Board
from skcapstone.projection_retirement import (
    card_generation_sha256,
    restore_projection,
    retire_projection,
)


def _world(root: Path, *, void: bool = True) -> tuple[CardStore, Path]:
    """Create one folded card and one stale legacy cache in a replica."""
    root.mkdir(exist_ok=True)
    store = CardStore(root)
    store.create(CardCore(id="deadbeef", kind="task", title="void", created_by="test"))
    if void:
        void_card(root, "deadbeef", reason="superseded", agent="test")
    board = Board(root)
    board.save_agent(AgentFile(agent="pi-test", current_task="deadbeef"))
    path = board.agents_dir / "pi-test.json"
    payload = json.loads(path.read_text())
    payload["last_seen"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(payload))
    return store, path


def _sha(path: Path) -> str:
    """Hash one exact byte stream."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retire(root: Path, store: CardStore, path: Path):
    """Invoke retirement with exact folded-card and cache digests."""
    return retire_projection(
        root,
        task_id="deadbeef",
        projection_agent="pi-test",
        expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
        expected_projection_sha256=_sha(path),
        actor="repair",
    )


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_broker_outage_preserves_legacy_cache(tmp_path: Path, replica: str) -> None:
    """An unavailable authority never falls back to a local canonical move."""
    root = tmp_path / replica
    store, path = _world(root)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="generation broker unavailable"):
        _retire(root, store, path)
    assert path.read_bytes() == before
    assert not (root / "agents-quarantine").exists()


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_remote_cache_race_cannot_delete_either_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replica: str
) -> None:
    """A peer cache write at broker handoff cannot be moved by SKCapstone."""
    import skcapstone.projection_retirement as retirement

    root = tmp_path / replica
    store, path = _world(root)
    original = path.read_bytes()
    generation = root / "coordination" / "agent-generations" / "pi-test" / "generation.json"
    generation.parent.mkdir(parents=True)
    generation.write_bytes(original)
    peer = original.replace(b'"agent": "pi-test"', b'"agent": "peer"')
    assert peer != original

    class RacedBroker:
        """Represent a remote write before authority validation."""

        @staticmethod
        def retire_projection(*args, **kwargs):
            """Refuse a competing cache generation without deleting bytes."""
            path.write_bytes(peer)
            raise ValueError("generation conflict")

    monkeypatch.setattr(retirement, "_generation_broker", lambda: RacedBroker)
    with pytest.raises(ValueError, match="generation conflict"):
        _retire(root, store, path)
    assert path.read_bytes() == peer
    assert generation.read_bytes() == original


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_retire_fences_disk_generation_race_without_cache_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replica: str
) -> None:
    """A competing immutable head invalidates the supplied generation hash."""
    import skcapstone.projection_retirement as retirement

    root = tmp_path / replica
    store, cache = _world(root)
    before = cache.read_bytes()
    generation_dir = root / "coordination" / "agents" / ".generations" / "pi-test" / replica
    generation_dir.mkdir(parents=True)
    first = generation_dir / "first.json"
    first.write_bytes(b'{"generation_id":"first"}')
    expected = _sha(first)
    competing = generation_dir / "second.json"

    def broker_retire(home, **kwargs):
        """Recheck the on-disk head at the immutable broker boundary."""
        assert home == root
        assert kwargs["expected_generation_sha256"] == expected
        if competing.exists():
            raise ValueError("generation conflict")
        pytest.fail("stale race reached disposition append")

    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: SimpleNamespace(retire_projection=broker_retire),
    )
    competing.write_bytes(b'{"generation_id":"second","predecessor":"race"}')
    with pytest.raises(ValueError, match="generation conflict"):
        retire_projection(
            root,
            task_id="deadbeef",
            projection_agent="pi-test",
            expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
            expected_projection_sha256=expected,
            actor="repair",
        )
    assert first.read_bytes() == b'{"generation_id":"first"}'
    assert competing.read_bytes().startswith(b'{"generation_id":"second"')
    assert cache.read_bytes() == before


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_restore_fences_changed_disk_receipt_without_cache_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replica: str
) -> None:
    """Restore rejects a changed immutable disposition on either replica."""
    import skcapstone.projection_retirement as retirement

    root = tmp_path / replica
    _, cache = _world(root)
    before = cache.read_bytes()
    disposition = (
        root / "coordination" / "agents" / ".generations" / "pi-test" / replica / "retired.json"
    )
    disposition.parent.mkdir(parents=True)
    disposition.write_bytes(b'{"disposition":"retired"}')
    expected = _sha(disposition)
    competing = disposition.with_name("restored.json")

    def broker_restore(home, **kwargs):
        """Require the exact disposition before appending restoration."""
        assert home == root
        assert kwargs["expected_receipt_sha256"] == expected
        if competing.exists():
            raise ValueError("generation conflict")
        pytest.fail("stale restore reached restoration append")

    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: SimpleNamespace(restore_projection=broker_restore),
    )
    competing.write_bytes(b'{"disposition":"competing"}')
    with pytest.raises(ValueError, match="generation conflict"):
        restore_projection(
            root,
            projection_agent="pi-test",
            expected_quarantine_sha256="a" * 64,
            expected_receipt_sha256=expected,
            actor="repair",
        )
    assert disposition.read_bytes() == b'{"disposition":"retired"}'
    assert competing.read_bytes() == b'{"disposition":"competing"}'
    assert cache.read_bytes() == before


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_contract_stub_retire_restore_keeps_immutable_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replica: str
) -> None:
    """Exercise both adapter paths with disk receipts and unchanged cache bytes."""
    import skcapstone.projection_retirement as retirement

    root = tmp_path / replica
    store, cache = _world(root)
    cache_before = cache.read_bytes()
    generation_dir = root / "coordination" / "agents" / ".generations" / "pi-test" / replica
    generation_dir.mkdir(parents=True)
    initial = generation_dir / "initial.json"
    initial.write_bytes(b'{"generation_id":"initial"}')
    initial_hash = _sha(initial)
    tombstone = generation_dir / "tombstone.json"
    disposition = generation_dir / "disposition.json"
    restoration = generation_dir / "restoration.json"
    restore_disposition = generation_dir / "restore-disposition.json"

    class ContractStub:
        """Persist test-only immutable records through the broker API seam."""

        @staticmethod
        def retire_projection(home, **kwargs):
            """Append one tombstone and disposition for an exact head."""
            assert home == root
            if _sha(initial) != kwargs["expected_generation_sha256"]:
                raise ValueError("generation conflict")
            with tombstone.open("xb") as record:
                record.write(b'{"kind":"tombstone","predecessor":"initial"}')
            with disposition.open("xb") as record:
                record.write(b'{"kind":"retired","generation":"tombstone"}')
            return SimpleNamespace(
                generation_sha256=_sha(tombstone),
                disposition_sha256=_sha(disposition),
            )

        @staticmethod
        def restore_projection(home, **kwargs):
            """Append restoration only for the exact prior receipt."""
            assert home == root
            if _sha(tombstone) != kwargs["expected_generation_sha256"]:
                raise ValueError("generation conflict")
            if _sha(disposition) != kwargs["expected_receipt_sha256"]:
                raise ValueError("disposition conflict")
            with restoration.open("xb") as record:
                record.write(b'{"kind":"restoration","predecessor":"tombstone"}')
            with restore_disposition.open("xb") as record:
                record.write(b'{"kind":"restored","generation":"restoration"}')
            return SimpleNamespace(
                generation_sha256=_sha(restoration),
                disposition_sha256=_sha(restore_disposition),
            )

    monkeypatch.setattr(retirement, "_generation_broker", lambda: ContractStub)
    retired = retire_projection(
        root,
        task_id="deadbeef",
        projection_agent="pi-test",
        expected_card_sha256=card_generation_sha256(store.fold("deadbeef")),
        expected_projection_sha256=initial_hash,
        actor="repair",
    )
    assert retired.generation_sha256 == _sha(tombstone)
    assert retired.disposition_sha256 == _sha(disposition)
    restored = restore_projection(
        root,
        projection_agent="pi-test",
        expected_quarantine_sha256=retired.generation_sha256,
        expected_receipt_sha256=retired.disposition_sha256,
        actor="repair",
    )
    assert restored.generation_sha256 == _sha(restoration)
    assert restored.disposition_sha256 == _sha(restore_disposition)
    assert initial.read_bytes() == b'{"generation_id":"initial"}'
    assert tombstone.read_bytes() == b'{"kind":"tombstone","predecessor":"initial"}'
    assert disposition.read_bytes() == b'{"kind":"retired","generation":"tombstone"}'
    assert cache.read_bytes() == cache_before


@pytest.mark.parametrize("replica", ["replica-a", "replica-b"])
def test_unresolved_conflict_fails_before_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replica: str
) -> None:
    """No mtime winner may authorize retirement on either replica."""
    import skcapstone.projection_retirement as retirement

    root = tmp_path / replica
    store, path = _world(root)
    conflict = path.with_name("pi-test.sync-conflict-20260916-010203-PEER1.json")
    conflict.write_bytes(b'{"peer":"preserve"}\n')
    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: pytest.fail("broker called for ambiguous cache"),
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ambiguous"):
        _retire(root, store, path)
    assert path.read_bytes() == before
    assert conflict.read_bytes() == b'{"peer":"preserve"}\n'


def test_validated_retirement_delegates_without_moving_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker receives exact fences while unrelated cache fields survive."""
    import skcapstone.projection_retirement as retirement

    store, path = _world(tmp_path)
    before = path.read_bytes()
    seen = {}
    receipt = SimpleNamespace(
        generation_sha256="a" * 64,
        disposition_sha256="b" * 64,
    )

    class Broker:
        """Capture the SKCoord boundary call."""

        @staticmethod
        def retire_projection(home, **kwargs):
            """Return a hash-bound immutable receipt."""
            seen.update(home=home, **kwargs)
            return receipt

    monkeypatch.setattr(retirement, "_generation_broker", lambda: Broker)
    result = _retire(tmp_path, store, path)
    assert result is receipt
    assert seen["expected_card_sha256"] == card_generation_sha256(store.fold("deadbeef"))
    assert seen["expected_generation_sha256"] == _sha(path)
    assert path.read_bytes() == before


def test_cli_reads_immutable_retirement_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public seam reports broker hashes without legacy quarantine fields."""
    import skcapstone.projection_retirement as retirement

    store, path = _world(tmp_path)
    before = path.read_bytes()
    receipt = SimpleNamespace(generation_sha256="a" * 64, disposition_sha256="b" * 64)
    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: SimpleNamespace(retire_projection=lambda *args, **kwargs: receipt),
    )
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "retire-ownerless-projection",
            "deadbeef",
            "--projection-agent",
            "pi-test",
            "--expected-card-sha256",
            card_generation_sha256(store.fold("deadbeef")),
            "--expected-projection-sha256",
            _sha(path),
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert '"generation_sha256": "' + "a" * 64 + '"' in result.output
    assert '"disposition_sha256": "' + "b" * 64 + '"' in result.output
    assert path.read_bytes() == before


def test_cli_restore_reads_immutable_receipt_without_publishing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore delegates exact hashes and reports its appended generation."""
    import skcapstone.projection_retirement as retirement

    _, path = _world(tmp_path)
    before = path.read_bytes()
    seen = {}

    def restore(home, **kwargs):
        """Capture the immutable restore contract without changing a cache."""
        seen.update(home=home, **kwargs)
        return SimpleNamespace(generation_sha256="c" * 64, disposition_sha256="d" * 64)

    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: SimpleNamespace(restore_projection=restore),
    )
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "restore-retired-projection",
            "pi-test",
            "--expected-quarantine-sha256",
            "a" * 64,
            "--expected-receipt-sha256",
            "b" * 64,
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert seen["expected_generation_sha256"] == "a" * 64
    assert seen["expected_receipt_sha256"] == "b" * 64
    assert '"generation_sha256": "' + "c" * 64 + '"' in result.output
    assert path.read_bytes() == before


def test_restore_outage_preserves_legacy_cache(tmp_path: Path) -> None:
    """Restore also refuses without a generation authority."""
    _, path = _world(tmp_path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="generation broker unavailable"):
        restore_projection(
            tmp_path,
            projection_agent="pi-test",
            expected_quarantine_sha256=_sha(path),
            expected_receipt_sha256="a" * 64,
            actor="repair",
        )
    assert path.read_bytes() == before


def test_incomplete_broker_refuses_without_cache_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed module without the disposition method is unavailable."""
    import skcapstone.projection_retirement as retirement

    store, path = _world(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(retirement, "_generation_broker", lambda: object())
    with pytest.raises(RuntimeError, match="generation broker unavailable"):
        _retire(tmp_path, store, path)
    assert path.read_bytes() == before


def test_restore_refuses_conflict_sibling_before_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore cannot pick a legacy cache winner from conflict bytes."""
    import skcapstone.projection_retirement as retirement

    _, path = _world(tmp_path)
    conflict = path.with_name("pi-test.sync-conflict-20260916-010203-PEER1.json")
    conflict.write_bytes(b"peer")
    monkeypatch.setattr(
        retirement,
        "_generation_broker",
        lambda: pytest.fail("broker called for ambiguous restore"),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        restore_projection(
            tmp_path,
            projection_agent="pi-test",
            expected_quarantine_sha256=_sha(path),
            expected_receipt_sha256="a" * 64,
            actor="repair",
        )
    assert conflict.read_bytes() == b"peer"


def test_owned_card_refuses_before_broker(tmp_path: Path) -> None:
    """The exact CardStore fold rejects nonowner retirement."""
    store, path = _world(tmp_path, void=False)
    store.append_event("deadbeef", "claim", "owner", owner="owner")
    with pytest.raises(ValueError, match="ownerless"):
        _retire(tmp_path, store, path)


def test_cli_requires_hash_fences(tmp_path: Path) -> None:
    """The public command requires exact digests."""
    _world(tmp_path)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "retire-ownerless-projection",
            "deadbeef",
            "--projection-agent",
            "pi-test",
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "expected-card-sha256" in result.output


def test_cli_with_fences_fails_closed_without_broker(tmp_path: Path) -> None:
    """The public command cannot retire a legacy cache on broker outage."""
    store, path = _world(tmp_path)
    before = path.read_bytes()
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "retire-ownerless-projection",
            "deadbeef",
            "--projection-agent",
            "pi-test",
            "--expected-card-sha256",
            card_generation_sha256(store.fold("deadbeef")),
            "--expected-projection-sha256",
            _sha(path),
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "generation broker unavailable" in result.output
    assert path.read_bytes() == before
