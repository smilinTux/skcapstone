"""Tests for the host-local, read-only fleet liveness publisher."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet_live_publisher import publish_host_snapshot


class _Store:
    """Return fixed exact claim generations for test workers."""

    def fold(self, card_id: str) -> object:
        if card_id == "aaaaaaaa":
            return SimpleNamespace(owner="codex-host-aaaaaaaa", meta={"_claim_revision": "rev-a"})
        return {"owner": "glm-host-bbbbbbbb", "meta": {"_claim_revision": "rev-b"}}


def test_publisher_records_local_processes_and_exact_claim_revisions(tmp_path: Path) -> None:
    """The happy path publishes both worker runtimes with exact generations."""

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "tmux":
            return SimpleNamespace(returncode=0, stdout="codex-auto-aaaaaaaa\nnoise\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="skfleet-worker-glm-bbbbbbbb.service loaded active running worker\n",
            stderr="",
        )

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        store=_Store(),
        runner=runner,
        now=lambda: 1234.5,
    )

    assert target == tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "cards": ["aaaaaaaa", "bbbbbbbb"],
        "host": "chiap01",
        "lanes": {},
        "ts": 1234.5,
        "workers": [
            {"card_id": "aaaaaaaa", "claim_revision": "rev-a", "owner": "codex-host-aaaaaaaa"},
            {"card_id": "bbbbbbbb", "claim_revision": "rev-b", "owner": "glm-host-bbbbbbbb"},
        ],
    }


@pytest.mark.parametrize("failed_probe", ["tmux", "systemctl"])
def test_publisher_fails_closed_when_a_local_process_probe_fails(
    tmp_path: Path, failed_probe: str
) -> None:
    """An incomplete local view never overwrites the last trustworthy snapshot."""
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        failed = command[0] == failed_probe
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="failed")

    with pytest.raises(RuntimeError, match=f"{failed_probe} liveness probe failed"):
        publish_host_snapshot(home=tmp_path, host="chiap01", store=_Store(), runner=runner)

    assert target.read_text(encoding="utf-8") == "previous\n"


def test_no_tmux_server_is_a_truthful_empty_process_view(tmp_path: Path) -> None:
    """A host without tmux still publishes running managed units."""

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "tmux":
            return SimpleNamespace(
                returncode=1, stdout="", stderr="no server running on /tmp/tmux/default"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    target = publish_host_snapshot(home=tmp_path, host="chiap01", store=_Store(), runner=runner)

    assert json.loads(target.read_text(encoding="utf-8"))["cards"] == []


def test_publisher_has_no_dispatch_surface() -> None:
    """The standalone module cannot select, claim, release, or launch work."""
    source = (
        Path(__file__).parents[1] / "src" / "skcapstone" / "fleet_live_publisher.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("coord claim", "release-claim", "skfleet-rotate", "--go", "Popen("):
        assert forbidden not in source


def test_packaged_timer_is_distinct_from_disabled_rotation_and_central_dispatch() -> None:
    """Publication gets its own unit and does not alter either dispatch scheduler."""
    root = Path(__file__).parents[1]
    for unit_root in (root / "systemd", root / "src" / "skcapstone" / "data" / "systemd"):
        service = (unit_root / "skfleet-live-publisher.service").read_text(encoding="utf-8")
        timer = (unit_root / "skfleet-live-publisher.timer").read_text(encoding="utf-8")
        assert "-m skcapstone.fleet_live_publisher" in service
        assert "skfleet-rotate" not in service
        assert "niobe_live_entrypoint" not in service
        assert "Unit=skfleet-live-publisher.service" in timer

    rollout = (root / "docs" / "fleet" / "liveness-publisher-rollout.md").read_text()
    assert "skfleet-rotate.timer remains disabled" in rollout
    assert "skfleet-niobe-live.timer remains the sole centralized dispatcher" in rollout
    assert "Do not execute these commands on this\nsource-repair card" in rollout
