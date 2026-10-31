"""Tests for skcapstone.presence_loop (PRES-6).

Hermetic: the Talk API is a fake fetcher, outreach is a recording sink, and the
clock and local hour are injected.
"""

from __future__ import annotations

import threading

from skcapstone.presence_loop import (
    PresenceConfig,
    PresenceLoop,
    PresenceState,
    detect_pending,
    in_quiet_hours,
    make_presence_task,
)

NOW = 1_800_000_000.0
SELF = "lumina"


def _room(token, rtype=1, actor="chef", age_min=30, mid=10, **extra):
    room = {
        "token": token,
        "type": rtype,
        "displayName": f"room-{token}",
        "lastMessage": {
            "id": mid,
            "actorId": actor,
            "actorType": "users",
            "actorDisplayName": actor,
            "timestamp": NOW - age_min * 60,
            "message": "hello   there",
            "systemMessage": "",
        },
    }
    room.update(extra)
    return room


class FakeTalk:
    """Returns a fixed room list and records POSTs."""

    def __init__(self, rooms=None, fail=False):
        self.rooms = rooms or []
        self.fail = fail
        self.posts: list[tuple[str, dict]] = []

    def __call__(self, method, path, body=None):
        if self.fail:
            raise OSError("boom")
        if method == "POST":
            self.posts.append((path, body))
            return {"ocs": {"data": {"id": 1}}}
        return {"ocs": {"data": self.rooms}}


class Sink:
    """Records outreach calls."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, title, body, urgency="normal", dedup_key=None):
        self.calls.append((title, body, dedup_key))
        return self.ok


def _cfg(**kw):
    base = dict(
        enabled=True,
        mode="live",
        username=SELF,
        threshold_minutes=15,
        max_per_tick=3,
        quiet_start=22,
        quiet_end=7,
    )
    base.update(kw)
    return PresenceConfig(**base)


def _loop(tmp_path, rooms, hour=12, sink=None, **cfg):
    talk = FakeTalk(rooms)
    sink = sink or Sink()
    loop = PresenceLoop(
        _cfg(**cfg),
        talk,
        PresenceState(tmp_path / "s.json"),
        sink=sink,
        clock=lambda: NOW,
        hour_fn=lambda: hour,
    )
    return loop, talk, sink


# --- detection --------------------------------------------------------------


def test_detects_old_dm_and_mention_only():
    rooms = [
        _room("dm"),
        _room("grp", rtype=2, unreadMention=True),
        _room("grp2", rtype=2),
        _room("mine", actor=SELF),
        _room("fresh", age_min=2),
        _room(
            "sys",
            lastMessage={
                "id": 3,
                "actorId": "x",
                "systemMessage": "call_ended",
                "timestamp": NOW - 9999,
            },
        ),
        _room(
            "bot",
            lastMessage={"id": 4, "actorId": "b", "actorType": "bots", "timestamp": NOW - 9999},
        ),
        _room("ro", readOnly=1),
        _room("self", rtype=6),
    ]
    got = detect_pending(rooms, SELF, NOW, 15)
    assert {p.token for p in got} == {"dm", "grp"}
    assert {p.kind for p in got} == {"dm", "mention"}
    assert got[0].preview == "hello there"


def test_sorted_longest_waiting_first():
    got = detect_pending([_room("a", age_min=20), _room("b", age_min=90)], SELF, NOW, 15)
    assert [p.token for p in got] == ["b", "a"]


def test_empty_and_malformed_rooms():
    assert detect_pending([], SELF, NOW, 15) == []
    assert detect_pending([{"type": 1, "lastMessage": []}, {"type": 1}], SELF, NOW, 15) == []


# --- quiet hours ------------------------------------------------------------


def test_quiet_hours_wrap_and_disabled():
    assert in_quiet_hours(23, 22, 7) and in_quiet_hours(3, 22, 7)
    assert not in_quiet_hours(7, 22, 7) and not in_quiet_hours(12, 22, 7)
    assert in_quiet_hours(13, 12, 14) and not in_quiet_hours(14, 12, 14)
    assert not in_quiet_hours(5, 0, 0)


# --- loop -------------------------------------------------------------------


def test_dry_run_sends_nothing_and_writes_no_state(tmp_path):
    loop, talk, sink = _loop(tmp_path, [_room("dm")], mode="dry-run", talk_nudge=True)
    report = loop.tick()
    assert report["pending"] == ["dm:10"] and report["sent"] == []
    assert sink.calls == [] and talk.posts == []
    assert not (tmp_path / "s.json").exists()


def test_live_sends_once_per_message(tmp_path):
    loop, _, sink = _loop(tmp_path, [_room("dm")])
    assert loop.tick()["sent"] == ["dm:10"]
    second = loop.tick()
    assert second["sent"] == [] and second["skipped_seen"] == ["dm:10"]
    assert len(sink.calls) == 1 and sink.calls[0][2] == "presence:dm:10"


def test_state_survives_restart(tmp_path):
    loop, _, _ = _loop(tmp_path, [_room("dm")])
    loop.tick()
    loop2, _, sink2 = _loop(tmp_path, [_room("dm")])
    assert loop2.tick()["skipped_seen"] == ["dm:10"] and sink2.calls == []


def test_new_message_in_same_room_fires_again(tmp_path):
    loop, talk, sink = _loop(tmp_path, [_room("dm", mid=10)])
    loop.tick()
    talk.rooms = [_room("dm", mid=11)]
    assert loop.tick()["sent"] == ["dm:11"] and len(sink.calls) == 2


def test_quiet_hours_defer_then_fire(tmp_path):
    hour = {"h": 23}
    talk, sink = FakeTalk([_room("dm")]), Sink()
    loop = PresenceLoop(
        _cfg(),
        talk,
        PresenceState(tmp_path / "s.json"),
        sink=sink,
        clock=lambda: NOW,
        hour_fn=lambda: hour["h"],
    )
    assert loop.tick()["deferred"] == ["dm:10"] and sink.calls == []
    hour["h"] = 8
    assert loop.tick()["sent"] == ["dm:10"]


def test_per_tick_cap(tmp_path):
    rooms = [_room(f"r{i}", mid=i + 1) for i in range(5)]
    report = _loop(tmp_path, rooms, max_per_tick=2)[0].tick()
    assert len(report["sent"]) == 2 and len(report["deferred"]) == 3


def test_talk_nudge_opt_in_replies_to_message(tmp_path):
    loop, talk, _ = _loop(tmp_path, [_room("dm", mid=42)], talk_nudge=True)
    loop.tick()
    assert talk.posts[0][0].endswith("/chat/dm?format=json")
    assert talk.posts[0][1]["replyTo"] == 42


def test_failed_sink_is_retried_next_tick(tmp_path):
    loop, _, sink = _loop(tmp_path, [_room("dm")], sink=Sink(ok=False))
    assert loop.tick()["sent"] == []
    sink.ok = True
    assert loop.tick()["sent"] == ["dm:10"]


def test_fetch_failure_never_raises(tmp_path):
    loop = PresenceLoop(
        _cfg(),
        FakeTalk(fail=True),
        PresenceState(tmp_path / "s.json"),
        sink=Sink(),
        clock=lambda: NOW,
        hour_fn=lambda: 12,
    )
    assert loop.tick()["error"].startswith("room list failed")


def test_corrupt_state_file_starts_empty(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    assert not PresenceState(tmp_path / "s.json").seen("x:1")


# --- config + registration --------------------------------------------------


def test_config_defaults_off_and_dry_run():
    cfg = PresenceConfig.from_env({})
    assert not cfg.enabled and cfg.mode == "dry-run" and not cfg.live
    assert cfg.interval_seconds == 900 and cfg.threshold_minutes == 15


def test_config_bad_values_fall_back():
    cfg = PresenceConfig.from_env(
        {"SKCAPSTONE_PRESENCE_MODE": "yolo", "SKCAPSTONE_PRESENCE_INTERVAL": "abc"}
    )
    assert cfg.mode == "dry-run" and cfg.interval_seconds == 900
    assert PresenceConfig.from_env({"SKCAPSTONE_PRESENCE_INTERVAL": "5"}).interval_seconds == 60


def test_password_not_in_repr():
    assert "s3cret" not in repr(PresenceConfig(app_password="s3cret"))


def test_make_task_off_or_without_creds(tmp_path):
    assert make_presence_task(tmp_path, {}) is None
    assert make_presence_task(tmp_path, {"SKCAPSTONE_PRESENCE_ENABLED": "1"}) is None


def test_make_task_enabled(tmp_path):
    env = {
        "SKCAPSTONE_PRESENCE_ENABLED": "1",
        "NEXTCLOUD_TALK_SERVER_URL": "https://x",
        "NEXTCLOUD_TALK_USERNAME": "u",
        "NEXTCLOUD_TALK_APP_PASSWORD": "p",
        "SKCAPSTONE_PRESENCE_INTERVAL": "600",
    }
    interval, cb = make_presence_task(tmp_path, env)
    assert interval == 600 and callable(cb)


def test_build_scheduler_registers_only_when_enabled(tmp_path, monkeypatch):
    from skcapstone.scheduled_tasks import build_scheduler

    monkeypatch.delenv("SKCAPSTONE_PRESENCE_ENABLED", raising=False)
    names = [t.name for t in build_scheduler(tmp_path, threading.Event())._tasks]
    assert "presence_loop" not in names

    monkeypatch.setenv("SKCAPSTONE_PRESENCE_ENABLED", "1")
    monkeypatch.setenv("NEXTCLOUD_TALK_SERVER_URL", "https://x")
    monkeypatch.setenv("NEXTCLOUD_TALK_USERNAME", "u")
    monkeypatch.setenv("NEXTCLOUD_TALK_APP_PASSWORD", "p")
    names = [t.name for t in build_scheduler(tmp_path, threading.Event())._tasks]
    assert "presence_loop" in names
