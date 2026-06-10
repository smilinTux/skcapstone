"""Tests for the stable public facade skcapstone.sdk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone import sdk


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point skcapstone at an isolated temp home for the duration of a test."""
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    # the facade resolves home via skcapstone.shared_home(), which reads the
    # module-level AGENT_HOME captured at import — patch it directly too.
    import skcapstone as pkg

    monkeypatch.setattr(pkg, "AGENT_HOME", str(tmp_path))
    return tmp_path


def test_is_available_true(home: Path):
    assert sdk.is_available() is True
    assert home.exists()


def test_alert_publishes_to_topic(home: Path):
    ok = sdk.alert("svc.error", {"message": "boom"}, level="error")
    assert ok is True
    topic_dir = home / "pubsub" / "topics" / "svc.error"
    assert topic_dir.is_dir()
    msgs = list(topic_dir.glob("msg-*.json"))
    assert len(msgs) == 1
    data = json.loads(msgs[0].read_text())
    assert data["payload"]["message"] == "boom"
    assert "error" in data["tags"]


def test_alert_unknown_level_falls_back_to_info(home: Path):
    sdk.alert("svc.weird", {"x": 1}, level="bogus")
    data = json.loads(next((home / "pubsub" / "topics" / "svc.weird").glob("msg-*.json")).read_text())
    assert data["tags"] == ["info"]


def test_register_and_unregister_job(home: Path):
    path = sdk.register_job({"name": "svc_tick", "every": "10m", "type": "shell", "command": "echo hi"})
    assert Path(path).exists()
    assert Path(path).name == "svc_tick.yaml"
    assert sdk.unregister_job("svc_tick") is True
    assert not Path(path).exists()


def test_coord_create_writes_task(home: Path):
    tid = sdk.coord_create("hello", description="d", priority="high", tags=["t"])
    matches = list((home / "coordination" / "tasks").glob(f"{tid}*.json"))
    assert len(matches) == 1
    task = json.loads(matches[0].read_text())
    assert task["title"] == "hello"
    assert task["priority"] == "high"


def test_register_service_writes_registry(home: Path):
    path = sdk.register_service("skvoice", health_url="http://localhost:9/health", pid_file="/tmp/x.pid")
    entry = json.loads(Path(path).read_text())
    assert entry["name"] == "skvoice"
    assert entry["health_url"] == "http://localhost:9/health"
    assert entry["pid_file"] == "/tmp/x.pid"
