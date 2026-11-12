import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "skfleet_hygiene", Path(__file__).parents[1] / "scripts/fleet/skfleet-hygiene.py"
)
hygiene = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hygiene
spec.loader.exec_module(hygiene)


def test_failed_selection_excludes_active_and_unrelated_units():
    units = hygiene.parse_failed_units(
        "skfleet-worker-dead.service loaded failed failed old\n"
        "skfleet-worker-live.service loaded active running live\n"
        "other.service loaded failed failed other\n"
    )
    assert [u.name for u in units] == ["skfleet-worker-dead.service"]


def test_session_selection_is_bounded():
    assert hygiene.parse_sessions("codex-auto-dead\nuser-session\ncodex-auto-live\n") == [
        "codex-auto-dead",
        "codex-auto-live",
    ]


def test_live_worker_is_not_orphan():
    sessions = "codex-auto-dead\ncodex-auto-live\n"
    panes = {"codex-auto-dead": "101\n", "codex-auto-live": "202\n"}
    processes = "101 1 bash\n202 1 skfleet-worker-wrapper.py\n303 202 codex --run\n"

    def run(args):
        if args[:3] == ["tmux", "list-panes", "-t"]:
            return panes[args[3]]
        if args[:2] == ["ps", "-eo"]:
            return processes
        raise AssertionError(args)

    orphans = hygiene.find_orphans(hygiene.parse_sessions(sessions), runner=run)
    assert [x.name for x in orphans] == ["codex-auto-dead"]


def test_cleanup_requires_explicit_flag(monkeypatch, capsys):
    monkeypatch.setattr(
        hygiene, "report", lambda: ([hygiene.FailedUnit("skfleet-worker-dead.service")], [])
    )
    calls = []
    monkeypatch.setattr(hygiene.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    assert hygiene.main([]) == 0
    assert calls == []
    assert "failed unit" in capsys.readouterr().out


def test_cleanup_targets_only_selected_items(monkeypatch):
    monkeypatch.setattr(
        hygiene,
        "report",
        lambda: (
            [hygiene.FailedUnit("skfleet-worker-dead.service")],
            [hygiene.OrphanSession("codex-auto-dead")],
        ),
    )
    calls = []
    monkeypatch.setattr(hygiene.subprocess, "run", lambda *args, **kwargs: calls.append(args[0]))
    assert hygiene.main(["--cleanup"]) == 0
    assert calls == [
        ["systemctl", "--user", "reset-failed", "skfleet-worker-dead.service"],
        ["tmux", "kill-session", "-t", "codex-auto-dead"],
    ]
