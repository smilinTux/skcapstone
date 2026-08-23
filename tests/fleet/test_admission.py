"""Tests for self-enrollment and admission."""

from __future__ import annotations

import pytest

from skcapstone.fleet import admission, sknoded, store


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None},
    )


def test_join_then_admit_with_preset(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    assert [j["name"] for j in admission.pending_joins(paths)] == ["node-41"]
    spec = admission.admit(paths, "node-41", writer=operator, preset=True)
    assert spec["labels"] == {"heavy-build": "true"}
    assert spec["generation"] == 1
    assert admission.pending_joins(paths) == []  # no longer pending
    # next sknoded pass observes its admission
    assert sknoded.run_once(paths, "node-41")["node"] is True
    assert store.read_node_file(paths, "node-41", "node.json")["observedGeneration"] == 1


def test_admit_requires_join_unless_bootstrap(paths, operator) -> None:
    with pytest.raises(LookupError):
        admission.admit(paths, "node-ghost", writer=operator)
    spec = admission.admit(paths, "node-158", writer=operator, preset=True, bootstrap=True)
    assert spec["labels"]["control-plane"] == "true"


def test_admit_is_idempotent(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    first = admission.admit(paths, "node-41", writer=operator, preset=True)
    again = admission.admit(paths, "node-41", writer=operator, preset=True)
    assert again["generation"] == first["generation"] == 1


def test_auto_admit_only_trusted(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.store.writer_identity", lambda: "capauth:lumina@skworld.io"
    )
    sknoded.run_once(paths, "node-41")
    assert admission.auto_admit(paths, {"capauth:other@x"}, writer=operator) == []
    admitted = admission.auto_admit(paths, {"capauth:lumina@skworld.io"}, writer=operator)
    assert admitted == ["node-41"]
    assert store.read_spec(paths, "node", "node-41") is not None


def test_pending_joins_empty_when_status_dir_missing(paths) -> None:
    assert paths.status.exists() is False
    assert admission.pending_joins(paths) == []


def test_presets_cover_the_four_nodes() -> None:
    # Rekeyed by card 8258517f. This assertion previously read `node-158`,
    # which pinned the defect rather than catching it: no node is ever
    # called that, because paths.self_node_name() derives the name from the
    # hostname, so `admit --preset` silently applied nothing on the control
    # box. The old spelling still resolves through PRESET_ALIASES.
    assert set(admission.PRESETS) == {
        "node-noroc2027",
        "node-41",
        "node-ollama",
        "node-local",
    }
    assert admission.PRESETS["node-ollama"]["taints"][0]["effect"] == "NoSchedule"
    assert admission.PRESETS["node-local"]["taints"][0]["effect"] == "PreferNoSchedule"


def test_the_dead_control_node_key_is_an_alias_not_a_preset() -> None:
    """`node-158` is the key that never fired. It is now an alias, so the
    spelling keeps working while the real key is the one a live node
    actually reports.

    Note `node-41` IS a real live name despite looking address-shaped: that
    box's sknoded.service sets SKFLEET_NODE=node-41, which overrides the
    hostname-derived default. So the rule is not "no numeric keys", it is
    "every key is a name some node actually reports".
    """
    assert "node-158" not in admission.PRESETS
    assert admission.PRESET_ALIASES["node-158"] == "node-noroc2027"
    assert admission.resolve_preset("node-158") is admission.PRESETS["node-noroc2027"]
