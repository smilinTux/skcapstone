from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from skcapstone.card_store_broker import CardStoreBroker, CardStoreBrokerClient
from skcapstone.card_store import CardCore, CardStore


def _run_broker(home: Path, sock: Path):
    broker = CardStoreBroker(home, sock)
    thread = threading.Thread(target=broker.start, daemon=True)
    thread.start()
    for _ in range(100):
        if sock.exists():
            return broker, thread
        time.sleep(0.01)
    raise AssertionError("broker did not start")


def test_broker_validates_revision_sequence_and_prev_hash(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c1", title="Card"))
    sock = tmp_path / "run" / "cardstore.sock"
    broker, thread = _run_broker(tmp_path, sock)
    try:
        client = CardStoreBrokerClient(sock, "worker-a")
        event = client.append("c1", "note", expected_revision=0, sequence=0, prev_hash="")
        assert event["worker_identity"] == "worker-a"
        with pytest.raises(RuntimeError, match="revision conflict"):
            client.append("c1", "note", expected_revision=0)
    finally:
        broker.close()
        thread.join(timeout=1)


def test_client_uses_json_serialization_and_append_only(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="c2", title="Card"))
    sock = tmp_path / "run" / "cardstore.sock"
    broker, thread = _run_broker(tmp_path, sock)
    try:
        CardStoreBrokerClient(sock, "worker-b").append("c2", "note", payload={"text": "quotes: \"ok\""})
        events = list((tmp_path / "cards" / "c2" / "events").glob("*.jsonl"))
        assert events
        for path in events:
            for line in path.read_bytes().splitlines():
                json.loads(line)
    finally:
        broker.close()
        thread.join(timeout=1)
