"""Tests for the rollout's converge_profile step (skcapstone.fleet.profile_converge)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skcapstone.fleet import installer, profile_converge
from skcapstone.fleet.staged_rollout import _DEPLOY_STEPS, _ROLLBACK_STEPS


@pytest.fixture
def bound(monkeypatch):
    """This node is bound to the `builder-standby` role; wt is required by it."""
    monkeypatch.setattr(
        profile_converge.node_controller,
        "node_views",
        lambda paths: [SimpleNamespace(name="n1", role="builder-standby")],
    )
    monkeypatch.setattr(
        installer, "_profile_spec", lambda paths, role: {"binaries": {"required": ["wt"]}}
    )
    monkeypatch.setattr(profile_converge.install_backends, "default_backends", lambda: {})


def _pinned(names):
    return {n: profile_converge.binaries.CATALOG[n].version for n in names}


def test_applies_profile_with_enable_and_runs_binary_post_install(bound):
    calls = {}

    def run_install(paths, role, **kw):
        calls.update(role=role, **kw)
        return {
            "results": [
                {
                    "status": "ok",
                    "kind": "timer",
                    "name": "skfleet-wt-gc.timer",
                    "detail": "enabled",
                }
            ]
        }

    finished = []
    lines = profile_converge.converge(
        paths=object(),
        node="n1",
        run_install=run_install,
        finish=lambda entry: finished.append(entry.name) or "plugin installed",
        probe=_pinned,
    )
    assert calls["role"] == "builder-standby"
    assert calls["mode"] == "apply" and calls["enable"] and calls["start"]
    assert calls["dry_run"] is False and calls["node"] == "n1"
    assert finished == ["wt"]
    assert "ok timer skfleet-wt-gc.timer: enabled" in lines
    assert "binary wt: plugin installed" in lines


def test_off_pin_binary_is_not_post_installed(bound):
    finished = []
    profile_converge.converge(
        paths=object(),
        node="n1",
        run_install=lambda *a, **k: {"results": []},
        finish=lambda entry: finished.append(entry.name),
        probe=lambda names: {n: "0.0.1" for n in names},
    )
    assert finished == []


@pytest.mark.parametrize(
    ("exc", "word"),
    [
        (installer.Frozen("x"), "frozen"),
        (installer.ActuationNotAllowed("n1"), "actuation not enabled"),
        (installer.ProfileNotApplied("builder-standby"), "not applied"),
    ],
)
def test_gated_refusals_are_skips_not_errors(bound, exc, word):
    def refuse(*a, **k):
        raise exc

    finished = []
    lines = profile_converge.converge(
        paths=object(),
        node="n1",
        run_install=refuse,
        finish=lambda entry: finished.append(entry),
        probe=_pinned,
    )
    assert len(lines) == 1 and word in lines[0] and "skipped" in lines[0]
    assert finished == [], "a gated node must not get post-install side effects either"


def test_node_without_role_is_skipped(monkeypatch):
    monkeypatch.setattr(
        profile_converge.node_controller,
        "node_views",
        lambda paths: [SimpleNamespace(name="n1", role="")],
    )
    lines = profile_converge.converge(
        paths=object(),
        node="n1",
        run_install=lambda *a, **k: pytest.fail("must not install without a role"),
    )
    assert lines == ["n1: no spec.role bound, skipped"]


def test_main_never_fails_the_deploy(monkeypatch, capsys):
    def boom(**kw):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(profile_converge, "converge", boom)
    assert profile_converge.main([]) == 0
    assert "store unreadable" in capsys.readouterr().out


def test_step_runs_after_converge_in_both_directions_and_is_guarded():
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        names = [n for n, _ in steps]
        assert names[-1] == "converge_profile"
        assert names.index("converge") < names.index("converge_profile")
        template = dict(steps)["converge_profile"]
        # A rollback to a revision without the module must be a no-op, not a failure.
        assert template.startswith("if test -f {repo}/src/skcapstone/fleet/profile_converge.py")
