"""SKMEM-P2-06: weekly GTD review seeded as a repeating intention, lazily."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skcapstone import gtd_intentions as gi

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)  # a Saturday


class _Int:
    model_fields = {"interval_s": None}

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _OldInt(_Int):
    model_fields: dict = {}


class _Store:
    def __init__(self, data):
        self.data = data

    def get(self, i):
        return self.data.get(i)

    def save(self, i):
        self.data[i.id] = i

    def close(self):
        pass


@pytest.fixture
def data(monkeypatch, tmp_path):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    d: dict = {}
    api = SimpleNamespace(Intention=_Int, open_store=lambda: _Store(d))
    monkeypatch.setattr(gi, "_load_api", lambda: api)
    return d


def test_seeds_once_and_is_idempotent(data):
    assert gi.seed_weekly_review(NOW) is True
    assert gi.seed_weekly_review(NOW) is False
    i = data[gi.WEEKLY_REVIEW_ID]
    assert i.interval_s == 7 * 86400 and i.kind == "time"
    assert i.due_at == datetime(2026, 11, 8, 17, 0, tzinfo=timezone.utc)


def test_done_review_is_not_resurrected(data):
    gi.seed_weekly_review(NOW)
    data[gi.WEEKLY_REVIEW_ID].state = "cancelled"
    assert gi.seed_weekly_review(NOW) is False


def test_unavailable_or_old_skmemory_is_a_noop(monkeypatch):
    monkeypatch.setattr(gi, "_load_api", lambda: None)
    assert gi.seed_weekly_review(NOW) is False
    d: dict = {}
    api = SimpleNamespace(Intention=_OldInt, open_store=lambda: _Store(d))
    monkeypatch.setattr(gi, "_load_api", lambda: api)
    assert gi.seed_weekly_review(NOW) is False
    assert d == {}
