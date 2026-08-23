"""The Profile kind on the three discovery surfaces (card cd478e02).

explain self-describes it, apply validates it, and get lists it. A fresh AI
operator finds the kind at runtime through these three, so each one is
tested against the real CLI rather than the module underneath it.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.fleet import store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.explain import explain

VALID_PROFILE = {
    "kind": "profile",
    "name": "worker-gpu",
    "spec": {
        "description": "Serve inference, hold zero sovereign state.",
        "units": {
            "required": ["skai-beellama.service"],
            "allowed": ["skai-beellama.service", "comfyui.service"],
            "mustNot": ["skchat-daemon.service"],
        },
        "stateTier": "control-bus",
        "capauthIdentityClass": "worker",
        "syncFolders": ["skfleet-control"],
    },
}


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


def _write(tmp_path, doc: dict, name: str = "profile.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


# -------------------------------------------------------------- explain ---


def test_profile_kind_is_registered() -> None:
    assert "profile" in explain()["kinds"]


def test_explain_profile_json_has_the_standard_blocks() -> None:
    result = CliRunner().invoke(fleet, ["explain", "profile", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {"spec", "status", "actions"} <= set(payload)
    assert payload["kind"] == "Profile"


def test_explain_documents_both_orthogonal_axes() -> None:
    payload = explain("profile")
    assert "stateTier" in payload["spec"]
    assert "capauthIdentityClass" in payload["spec"]
    # The doc string must say the tier is not derived from the role, because
    # conflating the two axes is the failure this whole kind exists to stop.
    assert "rthogonal" in payload["spec"]["stateTier"]


def test_explain_says_the_drift_condition_is_report_only() -> None:
    payload = explain("profile")
    assert "REPORT ONLY" in payload["conditions"]["ProfileDrift"]


# ---------------------------------------------------------------- apply ---


def test_apply_writes_a_valid_profile(tmp_path, paths) -> None:
    result = CliRunner().invoke(
        fleet, ["apply", "-f", _write(tmp_path, VALID_PROFILE)], env=_env(paths)
    )
    assert result.exit_code == 0, result.output
    assert "applied profile/worker-gpu" in result.output
    written = store.read_spec(paths, "profile", "worker-gpu")
    assert written["spec"]["stateTier"] == "control-bus"


def test_apply_rejects_a_contradictory_profile(tmp_path, paths) -> None:
    doc = json.loads(json.dumps(VALID_PROFILE))
    doc["spec"]["units"]["mustNot"] = ["comfyui.service"]  # also in allowed
    result = CliRunner().invoke(fleet, ["apply", "-f", _write(tmp_path, doc)], env=_env(paths))
    assert result.exit_code != 0
    assert "invalid profile spec" in result.output
    assert "both 'allowed' and 'mustNot'" in result.output
    # A rejected spec must never reach disk.
    assert store.read_spec(paths, "profile", "worker-gpu") is None


def test_apply_rejects_a_profile_missing_its_state_tier(tmp_path, paths) -> None:
    doc = json.loads(json.dumps(VALID_PROFILE))
    del doc["spec"]["stateTier"]
    result = CliRunner().invoke(fleet, ["apply", "-f", _write(tmp_path, doc)], env=_env(paths))
    assert result.exit_code != 0
    assert "stateTier" in result.output


# ------------------------------------------------------------------ get ---


def test_get_profiles_on_an_empty_tree(paths) -> None:
    result = CliRunner().invoke(fleet, ["get", "profiles"], env=_env(paths))
    assert result.exit_code == 0
    assert result.output.strip() == "no profiles"


def test_get_profiles_lists_the_applied_profile(tmp_path, paths) -> None:
    runner = CliRunner()
    runner.invoke(fleet, ["apply", "-f", _write(tmp_path, VALID_PROFILE)], env=_env(paths))
    result = runner.invoke(fleet, ["get", "profiles"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert "NAME\tSTATE-TIER" in result.output
    row = [line for line in result.output.splitlines() if line.startswith("worker-gpu")][0]
    fields = row.split("\t")
    assert fields[1] == "control-bus"
    assert fields[2] == "worker"
    assert fields[3] == "1"  # one required unit
    assert fields[4] == "1"  # one forbidden unit
    assert fields[5] == "-"  # no node bound to this role yet


def test_get_profiles_shows_nodes_bound_by_spec_role(tmp_path, paths, operator) -> None:
    """spec.role is owned by card 8258517f; this surface only reads it."""
    runner = CliRunner()
    runner.invoke(fleet, ["apply", "-f", _write(tmp_path, VALID_PROFILE)], env=_env(paths))
    store.write_spec(paths, "node", "node-100", {"role": "worker-gpu"}, writer=operator)
    store.write_spec(paths, "node", "node-41", {"role": "builder-standby"}, writer=operator)

    result = runner.invoke(fleet, ["get", "profiles"], env=_env(paths))
    row = [line for line in result.output.splitlines() if line.startswith("worker-gpu")][0]
    assert row.split("\t")[5] == "node-100"


def test_a_node_without_spec_role_binds_to_nothing(tmp_path, paths, operator) -> None:
    """Before card 8258517f lands, node specs carry no role. That must read
    as 'unbound', never as an error."""
    runner = CliRunner()
    runner.invoke(fleet, ["apply", "-f", _write(tmp_path, VALID_PROFILE)], env=_env(paths))
    store.write_spec(paths, "node", "node-legacy", {"cordoned": False}, writer=operator)

    result = runner.invoke(fleet, ["get", "profiles"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[1].split("\t")[5] == "-"


def test_an_unreadable_profile_is_surfaced_not_hidden(tmp_path, paths, operator) -> None:
    """A profile nobody can parse is exactly what an operator needs to see,
    so it is listed as INVALID rather than silently dropped."""
    # Written straight through the store, bypassing the apply-time validator.
    store.write_spec(paths, "profile", "broken", {"stateTier": "nonsense"}, writer=operator)
    result = CliRunner().invoke(fleet, ["get", "profiles"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert "INVALID" in result.output


def test_unknown_resource_lists_profiles_as_available(paths) -> None:
    result = CliRunner().invoke(fleet, ["get", "nonsense"], env=_env(paths))
    assert result.exit_code != 0
    assert "profiles" in result.output
