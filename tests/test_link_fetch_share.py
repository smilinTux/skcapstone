from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from skcapstone import link_fetch_share as share
from skcapstone.link_fetch_share import SharedFetch


class Connector:
    def __init__(self):
        self.calls = []

    def list_open(self, repository):
        self.calls.append(repository)
        return [{"number": 1, "repository": repository}]


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    for key, value in share.fresh_run_env(directory, "run-a").items():
        monkeypatch.setenv(key, value)
    return directory / "fetch.json"


def test_second_stage_reuses_first_stage_fetch(run_env):
    first, second = Connector(), Connector()
    a = SharedFetch(first, "lineage").list_open("o/r")
    b = SharedFetch(second, "producer").list_open("o/r")
    assert a == b and first.calls == ["o/r"] and second.calls == []


def test_no_environment_means_live_fetch_every_time(monkeypatch):
    monkeypatch.delenv(share.ENV_PATH, raising=False)
    monkeypatch.delenv(share.ENV_RUN, raising=False)
    c = Connector()
    SharedFetch(c, "a").list_open("o/r")
    SharedFetch(c, "b").list_open("o/r")
    assert c.calls == ["o/r", "o/r"]


def test_other_run_id_never_reads_this_cache(run_env, monkeypatch):
    SharedFetch(Connector(), "lineage").list_open("o/r")
    monkeypatch.setenv(share.ENV_RUN, "run-b")
    c = Connector()
    SharedFetch(c, "producer").list_open("o/r")
    assert c.calls == ["o/r"]


def test_stale_and_future_entries_are_refetched(run_env):
    now = datetime.now(timezone.utc).timestamp()
    SharedFetch(Connector(), "lineage", clock=lambda: now - 3600).list_open("o/r")
    c = Connector()
    SharedFetch(c, "producer", clock=lambda: now).list_open("o/r")
    assert c.calls == ["o/r"]
    SharedFetch(Connector(), "lineage", clock=lambda: now + 3600).list_open("x/y")
    c = Connector()
    SharedFetch(c, "producer", clock=lambda: now).list_open("x/y")
    assert c.calls == ["x/y"]


def test_untrusted_cache_files_are_ignored(run_env):
    SharedFetch(Connector(), "lineage").list_open("o/r")
    os.chmod(run_env, 0o644)  # group or other access: not trusted
    c = Connector()
    SharedFetch(c, "producer").list_open("o/r")
    assert c.calls == ["o/r"]
    os.chmod(run_env, 0o600)
    target = run_env.with_name("real.json")
    run_env.rename(target)
    run_env.symlink_to(target)  # symlink: not trusted
    c = Connector()
    SharedFetch(c, "producer").list_open("o/r")
    assert c.calls == ["o/r"]


def test_failed_fetch_is_not_cached(run_env):
    class Broken(Connector):
        def list_open(self, repository):
            raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        SharedFetch(Broken(), "lineage").list_open("o/r")
    c = Connector()
    SharedFetch(c, "producer").list_open("o/r")
    assert c.calls == ["o/r"]


def test_budget_line_reports_requests_and_wall_time(run_env, capsys):
    shared = SharedFetch(Connector(), "lineage")
    shared.list_open("o/r")
    SharedFetch(Connector(), "producer").list_open("o/r")
    shared.log_budget()
    err = capsys.readouterr().err
    assert "link_fetch_budget stage=lineage forge_requests=0 repos_live=1" in err
    assert "wall_seconds=" in err
