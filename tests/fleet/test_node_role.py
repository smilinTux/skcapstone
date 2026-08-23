"""spec.role on the Node kind, and the set-role action (card 8258517f).

Binding a node to an install profile is the join between the two halves of
the epic: the profile says what a role should have, spec.role says which
role this node is. Everything downstream (the drift report, the converge
gate) reads this field, so it has to round-trip exactly and it has to
survive every other operator action untouched.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import admission, node_controller, store
from skcapstone.fleet.cli import fleet


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


@pytest.fixture
def admitted(paths, operator):
    """One admitted node carrying every other spec field, so we can prove
    set_role preserves them."""
    store.write_spec(
        paths,
        "node",
        "node-ollama",
        {
            "taints": [{"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}],
            "cordoned": True,
            "address": {"hostname": "ollama"},
            "identity": "capauth:lumina@skworld.io",
            "actuate": True,
        },
        writer=operator,
        labels={"gpu": "true"},
    )
    return paths


# ------------------------------------------------------------- set_role ---


def test_set_role_preserves_every_other_field(admitted, operator) -> None:
    before = store.read_spec(admitted, "node", "node-ollama")

    node_controller.set_role(admitted, "node-ollama", "worker-gpu", writer=operator)

    after = store.read_spec(admitted, "node", "node-ollama")
    assert after["spec"]["role"] == "worker-gpu"
    for field in ("taints", "cordoned", "address", "identity", "actuate"):
        assert after["spec"][field] == before["spec"][field], f"set_role clobbered {field}"
    assert after["labels"] == {"gpu": "true"}


def test_set_role_bumps_generation_by_exactly_one(admitted, operator) -> None:
    before = store.read_spec(admitted, "node", "node-ollama")["generation"]
    node_controller.set_role(admitted, "node-ollama", "worker-gpu", writer=operator)
    after = store.read_spec(admitted, "node", "node-ollama")["generation"]
    assert after == before + 1


def test_set_role_is_reassignable(admitted, operator) -> None:
    node_controller.set_role(admitted, "node-ollama", "worker-gpu", writer=operator)
    node_controller.set_role(admitted, "node-ollama", "observer", writer=operator)
    assert store.read_spec(admitted, "node", "node-ollama")["spec"]["role"] == "observer"


def test_set_role_on_an_unknown_node_raises(paths, operator) -> None:
    with pytest.raises(LookupError, match="no such node object"):
        node_controller.set_role(paths, "node-ghost", "worker-gpu", writer=operator)


@pytest.mark.parametrize("bad", ["../escape", "Worker", "/abs", "_hidden", ""])
def test_set_role_rejects_unsafe_names(admitted, operator, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid role name"):
        node_controller.set_role(admitted, "node-ollama", bad, writer=operator)


def test_set_role_does_not_require_the_profile_to_exist(admitted, operator) -> None:
    """Deliberate: requiring the manifest first would deadlock this card
    against the one that authors the manifests."""
    node_controller.set_role(admitted, "node-ollama", "not-authored-yet", writer=operator)
    assert store.read_spec(admitted, "node", "node-ollama")["spec"]["role"] == "not-authored-yet"


# ------------------------------------------------------------- NodeView ---


def test_node_view_round_trips_the_role(admitted, operator) -> None:
    node_controller.set_role(admitted, "node-ollama", "worker-gpu", writer=operator)
    view = {v.name: v for v in node_controller.node_views(admitted)}["node-ollama"]
    assert view.role == "worker-gpu"


def test_a_node_without_a_role_reads_as_unbound_not_an_error(paths, operator) -> None:
    store.write_spec(paths, "node", "node-legacy", {"cordoned": False}, writer=operator)
    view = {v.name: v for v in node_controller.node_views(paths)}["node-legacy"]
    assert view.role == ""


def test_skfleet_nodes_still_renders_with_and_without_a_role(admitted, operator) -> None:
    store.write_spec(admitted, "node", "node-legacy", {"cordoned": False}, writer=operator)
    node_controller.set_role(admitted, "node-ollama", "worker-gpu", writer=operator)

    result = CliRunner().invoke(fleet, ["nodes"], env=_env(admitted))
    assert result.exit_code == 0, result.output
    assert "role=worker-gpu" in result.output
    assert "role=-" in result.output  # the unbound node


# -------------------------------------------------------------- PRESETS ---


def test_presets_key_matches_the_live_control_node_name() -> None:
    """The bug this card fixes: PRESETS was keyed `node-158`, but
    paths.self_node_name() derives from the hostname, so the live control
    node is `node-noroc2027` and `admit --preset` silently applied nothing.
    """
    assert "node-noroc2027" in admission.PRESETS
    assert admission.PRESETS["node-noroc2027"]["labels"]["control-plane"] == "true"


def test_the_old_address_style_key_still_resolves_via_alias() -> None:
    """Rekeyed rather than renamed, so runbooks saying `node-158` keep working."""
    assert admission.resolve_preset("node-158") is admission.PRESETS["node-noroc2027"]
    assert admission.resolve_preset("node-noroc2027") is admission.PRESETS["node-noroc2027"]
    assert admission.resolve_preset("node-nope") is None


def test_every_preset_carries_a_role() -> None:
    for name, preset in admission.PRESETS.items():
        assert "role" in preset, f"{name} preset has no role"


def test_only_the_gpu_node_preset_claims_a_gpu() -> None:
    """node-41 was carrying gpu=true in the live store; the GPU box is .100."""
    gpu_nodes = {n for n, p in admission.PRESETS.items() if p["labels"].get("gpu") == "true"}
    assert gpu_nodes == {"node-ollama"}


# ------------------------------------------------------------- admission ---


def test_admit_with_preset_applies_the_role(paths, operator) -> None:
    spec = admission.admit(paths, "node-ollama", writer=operator, preset=True, bootstrap=True)
    assert spec["spec"]["role"] == "worker-gpu"
    assert spec["labels"]["gpu"] == "true"


def test_admit_with_preset_on_the_control_node_is_no_longer_a_silent_noop(paths, operator) -> None:
    spec = admission.admit(paths, "node-noroc2027", writer=operator, preset=True, bootstrap=True)
    assert spec["labels"] == {
        "always-on": "true",
        "dev-primary": "true",
        "control-plane": "true",
    }
    assert spec["spec"]["role"] == "control"


def test_explicit_role_wins_over_the_preset(paths, operator) -> None:
    spec = admission.admit(
        paths, "node-ollama", writer=operator, role="observer", preset=True, bootstrap=True
    )
    assert spec["spec"]["role"] == "observer"


def test_admit_without_a_role_is_unbound(paths, operator) -> None:
    spec = admission.admit(paths, "node-plain", writer=operator, bootstrap=True)
    assert spec["spec"]["role"] == ""


# ------------------------------------------------------------------ CLI ---


def test_cli_set_role_round_trips(admitted) -> None:
    runner = CliRunner()
    result = runner.invoke(fleet, ["set-role", "node-ollama", "worker-gpu"], env=_env(admitted))
    assert result.exit_code == 0, result.output
    assert "role=worker-gpu" in result.output

    described = runner.invoke(fleet, ["describe", "node", "node-ollama"], env=_env(admitted))
    assert json.loads(described.output)["spec"]["spec"]["role"] == "worker-gpu"


def test_cli_set_role_rejects_an_unsafe_name(admitted) -> None:
    result = CliRunner().invoke(fleet, ["set-role", "node-ollama", "../evil"], env=_env(admitted))
    assert result.exit_code != 0
    assert "invalid role name" in result.output


def test_cli_set_role_on_a_missing_node_is_a_clean_error(paths) -> None:
    result = CliRunner().invoke(fleet, ["set-role", "node-ghost", "control"], env=_env(paths))
    assert result.exit_code != 0
    assert "no such node object" in result.output


def test_cli_admit_reports_the_bound_role(paths) -> None:
    result = CliRunner().invoke(
        fleet, ["admit", "node-ollama", "--preset", "--bootstrap"], env=_env(paths)
    )
    assert result.exit_code == 0, result.output
    assert "role=worker-gpu" in result.output


# ------------------------------------------------------- role gate (fdd17a01) ---


def test_role_gate_is_off_by_default(monkeypatch) -> None:
    """A fleet that is harder to join has failed at its own job. The gate
    stays off until every live node carries a role."""
    monkeypatch.delenv("SKFLEET_REQUIRE_ROLE", raising=False)
    assert admission.role_gate_on() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "on", " on "])
def test_role_gate_reads_the_env_flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", value)
    assert admission.role_gate_on() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "shadow"])
def test_only_explicit_truthy_values_arm_the_gate(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", value)
    assert admission.role_gate_on() is False


def test_admit_without_a_role_is_refused_when_the_gate_is_on(paths, operator, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", "1")
    with pytest.raises(admission.RoleRequiredError, match="has no role"):
        admission.admit(paths, "node-fresh", writer=operator, bootstrap=True)
    # The refusal must leave nothing behind: a half-admitted node is worse
    # than a rejected one.
    assert store.read_spec(paths, "node", "node-fresh") is None


def test_admit_without_a_role_is_allowed_when_the_gate_is_off(
    paths, operator, monkeypatch
) -> None:
    monkeypatch.delenv("SKFLEET_REQUIRE_ROLE", raising=False)
    spec = admission.admit(paths, "node-fresh", writer=operator, bootstrap=True)
    assert spec["spec"]["role"] == ""


def test_the_gate_accepts_a_role_from_preset(paths, operator, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", "1")
    spec = admission.admit(paths, "node-ollama", writer=operator, preset=True, bootstrap=True)
    assert spec["spec"]["role"] == "worker-gpu"


def test_the_gate_accepts_an_explicit_role(paths, operator, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", "1")
    spec = admission.admit(paths, "node-fresh", writer=operator, role="observer", bootstrap=True)
    assert spec["spec"]["role"] == "observer"


def test_node_local_preset_has_no_role_so_the_gate_still_refuses_it(
    paths, operator, monkeypatch
) -> None:
    """node-local is an interactive box with no fleet role. With the gate on
    it must be given one explicitly rather than sliding through on a preset
    that supplies an empty string."""
    monkeypatch.setenv("SKFLEET_REQUIRE_ROLE", "1")
    with pytest.raises(admission.RoleRequiredError):
        admission.admit(paths, "node-local", writer=operator, preset=True, bootstrap=True)
