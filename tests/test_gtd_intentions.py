"""SKMEM-P2-05: GTD capture --due/--remind, overdue listing, waiting-for sweep.

skmemory intentions are replaced by an in-memory fake (the pinned skmemory
predates them); nothing here touches a real agent store or ~/.skcapstone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone import gtd_intentions as gi

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


class FakeIntention:
    def __init__(self, **kw):
        self.state = "pending"
        self.snooze_until = None
        self.origin_memory_id = None
        self.__dict__.update(kw)


class FakeStore:
    def __init__(self, data):
        self.data = data

    def get(self, iid):
        return self.data.get(iid)

    def save(self, i):
        self.data[i.id] = i

    def list(self, state=None):
        return [i for i in self.data.values() if state is None or i.state == state]

    def close(self):
        pass


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    data: dict = {}
    api = SimpleNamespace(Intention=FakeIntention, open_store=lambda: FakeStore(data))
    monkeypatch.setattr(gi, "_load_api", lambda: api)
    return data


def _cli():
    import click

    from skcapstone.cli.gtd import register_gtd_commands

    g = click.Group()
    register_gtd_commands(g)
    return g


def run(*args):
    return CliRunner().invoke(_cli(), ["gtd", *args], catch_exceptions=False)


def test_parse_when():
    assert gi.parse_when("in 3 days", NOW) == NOW + timedelta(days=3)
    assert gi.parse_when("in 2 hours", NOW) == NOW + timedelta(hours=2)
    assert gi.parse_when("2026-12-01", NOW) == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert gi.parse_when("2026-12-01T09:00:00Z", NOW).hour == 9
    with pytest.raises(ValueError):
        gi.parse_when("next tuesday", NOW)


def test_capture_due_and_remind_create_linked_intentions(fake):
    r = run("capture", "Call dentist", "--due", "in 2 days", "--remind", "2026-12-01")
    assert r.exit_code == 0, r.output
    assert len(fake) == 2
    item_ids = {i.origin_memory_id for i in fake.values()}
    assert len(item_ids) == 1
    (item_id,) = item_ids
    assert set(fake) == {f"gtd-{item_id}-due", f"gtd-{item_id}-remind"}
    assert all(i.kind == "time" and i.channel == "inline" for i in fake.values())


def test_channel_from_alert_env(fake, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alert.env").write_text("SKALERT_TALK_ROOM=abc\n")
    run("capture", "x", "--due", "in 1 days")
    assert {i.channel for i in fake.values()} == {"talk"}


def test_capture_without_flags_makes_no_intention(fake):
    assert run("capture", "plain").exit_code == 0
    assert fake == {}


def test_bad_time_captures_nothing(fake):
    r = run("capture", "x", "--due", "whenever")
    assert r.exit_code != 0 and "--due" in r.output
    assert "Inbox is empty" in run("inbox").output


def test_unavailable_keeps_item_and_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    r = run("capture", "keep me", "--due", "in 1 days")
    assert r.exit_code != 0
    assert "not available" in r.output and "was captured" in r.output
    inbox = json.loads((tmp_path / "coordination" / "gtd" / "inbox.json").read_text())
    assert [i["text"] for i in inbox] == ["keep me"]
    # a plain capture is unaffected
    assert run("capture", "other").exit_code == 0


def test_next_and_waiting_omit_section_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    assert gi.overdue_intentions(NOW) == []
    assert "Overdue" not in run("next").output
    assert "Overdue" not in run("waiting").output


def test_overdue_listing_filters_and_orders(fake):
    mk = lambda iid, due, **kw: fake.__setitem__(  # noqa: E731
        iid, FakeIntention(id=iid, kind="time", title=iid, due_at=due, **kw)
    )
    mk("late", NOW - timedelta(days=5))
    mk("later", NOW - timedelta(days=1))
    mk("future", NOW + timedelta(days=1))
    mk("done", NOW - timedelta(days=9), state="done")
    mk("snoozed", NOW - timedelta(days=9), snooze_until=NOW + timedelta(days=1))
    assert [d["id"] for d in gi.overdue_intentions(NOW)] == ["late", "later"]


def test_next_and_waiting_print_overdue_first(fake):
    fake["o1"] = FakeIntention(
        id="o1",
        kind="time",
        title="pay rent",
        due_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    for cmd in ("next", "waiting"):
        out = run(cmd).output
        assert "Overdue intentions (1)" in out and "pay rent" in out


def _waiting(item_id, days, owner=""):
    return {
        "id": item_id,
        "text": f"t-{item_id}",
        "delegate_to": owner,
        "created_at": (NOW - timedelta(days=days)).isoformat(),
    }


def test_sweep_is_idempotent_and_selective(fake):
    items = [
        _waiting("a", 20),
        _waiting("b", 20, owner="bob"),
        _waiting("c", 5),
        _waiting("d", 15),
    ]
    assert sorted(gi.sweep_waiting(items, NOW)) == ["gtd-a-stale", "gtd-d-stale"]
    assert gi.sweep_waiting(items, NOW) == []
    assert len(fake) == 2
    # a dismissed one is not resurrected
    fake["gtd-a-stale"].state = "done"
    assert gi.sweep_waiting(items, NOW) == []
    assert fake["gtd-a-stale"].state == "done"


def test_sweep_unavailable_raises(monkeypatch):
    monkeypatch.setattr(gi, "_load_api", lambda: None)
    with pytest.raises(gi.IntentionsUnavailableError):
        gi.sweep_waiting([_waiting("a", 30)], NOW)


def test_sweep_cli(fake, tmp_path):
    wf = tmp_path / "coordination" / "gtd" / "waiting-for.json"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(json.dumps([_waiting("zz", 40)]))
    r = run("sweep-waiting")
    assert r.exit_code == 0 and "1 new" in r.output
    assert "1 new" not in run("sweep-waiting").output
    assert list(fake) == ["gtd-zz-stale"]
