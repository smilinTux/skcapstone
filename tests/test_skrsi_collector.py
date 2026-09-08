from skcapstone.skrsi_collector import BoundedCollector
from skcapstone.skrsi_registry import AppendOnlyOutbox


def test_bounded_metadata_only_and_deduplicated(tmp_path):
    collector = BoundedCollector(AppendOnlyOutbox(tmp_path / "outbox.jsonl"), source="cardstore", target_ref="target/x", queue_size=1)
    assert collector.submit({"source": "cardstore", "natural_key": "a", "cursor": "1", "metadata": {"state": "done"}, "body_hash": "a" * 64})
    assert not collector.submit({"source": "cardstore", "natural_key": "b"})
    result = collector.drain()
    assert result.accepted == 1 and result.overloaded
    assert collector.submit({"source": "cardstore", "natural_key": "a", "cursor": "1", "metadata": {"state": "done"}, "body_hash": "a" * 64})
    assert collector.drain().accepted == 0
    assert "body" not in (tmp_path / "outbox.jsonl").read_text()


def test_protected_data_rejected(tmp_path):
    collector = BoundedCollector(AppendOnlyOutbox(tmp_path / "outbox.jsonl"), source="mail", target_ref="target/x")
    assert collector.submit({"source": "mail", "natural_key": "x", "body": "secret"})
    assert collector.drain().rejected == 1
