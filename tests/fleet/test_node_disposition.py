"""Tests for the node unit-disposition generator (cards 5ad840ac, bf83eed2).

Runs entirely against fixture inventories. There is no ssh anywhere in this
path, and no live node is contacted: collection is a separate, read-only
step whose output is checked in.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "gen-node-disposition.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_node_disposition", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_module()


FIXTURE_USER = {
    "skai-beellama.service": "enabled",
    "skvoice.service": "enabled",
    "comfyui.service": "enabled",
    "gpg-agent.socket": "enabled",
    "session-migration.service": "enabled",
}
FIXTURE_SYSTEM = {
    "ollama.service": "enabled",
    "nfs-server.service": "enabled",
    "docker.service": "enabled",
    "snapd.service": "enabled",
    "var-lib-snapd-snap-core24-1643.mount": "enabled",
    "lxd-installer.socket": "enabled",
    "nvidia-suspend.service": "enabled",
}
FIXTURE_CONTROL = {"ollama.service": "enabled", "docker.service": "enabled"}


@pytest.fixture
def fixture_inventories(tmp_path, monkeypatch):
    """Point the generator at fixture inventories instead of the real ones."""

    def write(node: str, scope: str, units: dict) -> None:
        (tmp_path / f"{node}-{scope}-units.json").write_text(
            json.dumps({"node": node, "scope": scope, "units": units})
        )

    write("node-100", "user", FIXTURE_USER)
    write("node-100", "system", FIXTURE_SYSTEM)
    write("node-fixture-control", "user", {})
    write("node-fixture-control", "system", FIXTURE_CONTROL)
    monkeypatch.setattr(gen, "INVENTORY_DIR", tmp_path)
    return tmp_path


# ------------------------------------------------------------- classify ---


def test_keep_rules_carry_a_reason() -> None:
    for unit in ("ollama.service", "skai-beellama.service", "comfyui.service"):
        disposition, rationale = gen.classify(unit, "system", "worker-gpu")
        assert disposition == "keep"
        assert rationale, f"{unit} kept with no reason"


def test_evidence_backed_verdicts_say_so() -> None:
    """The three judgement calls had to be resolved with evidence, not
    opinion, so the rationale must carry it."""
    for unit in ("nfs-server.service", "docker.service", "skvoice.service"):
        _, rationale = gen.classify(unit, "system", "worker-gpu")
        assert "EVIDENCE-BACKED" in rationale, unit


def test_snapd_mount_units_are_baseline_not_review_material() -> None:
    """58 of .41's 108 enabled units are snap mounts. If they land in the
    review list the table is noise and Chef cannot use it."""
    disposition, _ = gen.classify(
        "var-lib-snapd-snap-core24-1643.mount", "system", "builder-standby"
    )
    assert disposition == "out-of-scope"


def test_always_in_scope_beats_a_broad_baseline_glob() -> None:
    """systemd-oomd matches `systemd-*` but it is the unit behind the .41
    freezes, so it must stay in the review."""
    disposition, _ = gen.classify("systemd-oomd.service", "system", "builder-standby")
    assert disposition == "standby"
    # ...while its siblings stay filtered.
    assert (
        gen.classify("systemd-resolved.service", "system", "builder-standby")[0] == "out-of-scope"
    )


def test_lxd_installer_is_baseline_but_lxd_itself_is_not() -> None:
    """A broad `lxd*` glob dragged Ubuntu's install-on-demand shim into scope
    on a GPU worker and proposed disabling it with no reason."""
    assert gen.classify("lxd-installer.socket", "system", "worker-gpu")[0] == "out-of-scope"
    assert gen.classify("lxd.service", "system", "builder-standby")[0] == "standby"


def test_hardware_baseline_is_out_of_scope() -> None:
    assert gen.classify("nvidia-suspend.service", "system", "worker-gpu")[0] == "out-of-scope"


def test_builder_standby_defaults_to_standby_never_disable() -> None:
    """.41 is not being slimmed by this card. Nothing on it may default into
    a removal proposal."""
    disposition, _ = gen.classify("some-unknown-sk-thing.service", "user", "builder-standby")
    assert disposition == "standby"


# ----------------------------------------------------------------- rows ---


def test_every_enabled_unit_gets_exactly_one_row(fixture_inventories) -> None:
    rows = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    assert len(rows) == len(FIXTURE_USER) + len(FIXTURE_SYSTEM)
    assert len({(r["scope"], r["unit"]) for r in rows}) == len(rows)


def test_rows_are_complete_and_deterministic(fixture_inventories) -> None:
    first = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    second = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    assert first == second
    for row in first:
        assert row["unit"]
        assert row["scope"] in {"user", "system"}
        assert row["present_on_control"] in {"yes", "no"}
        assert row["disposition"] in {"keep", "disable", "standby", "out-of-scope"}


def test_present_on_control_compares_against_the_reference(fixture_inventories) -> None:
    rows = {r["unit"]: r for r in gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)}
    assert rows["ollama.service"]["present_on_control"] == "yes"
    assert rows["skvoice.service"]["present_on_control"] == "no"


# --------------------------------------------------------------- revert ---


def test_revert_enables_exactly_what_the_plan_disables(fixture_inventories) -> None:
    rows = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    script = gen.render_revert("node-100", rows)
    planned = sorted(r["unit"] for r in rows if r["disposition"] == "disable")
    reverted = sorted(
        line.split()[-1] for line in script.splitlines() if line.startswith("systemctl")
    )
    assert planned == reverted
    assert planned, "the fixture must exercise at least one disable"


def test_revert_uses_the_right_systemd_scope(fixture_inventories) -> None:
    rows = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    script = gen.render_revert("node-100", rows)
    for line in script.splitlines():
        if line.startswith("systemctl"):
            # Every disable in the fixture is user scope; a system-scope
            # enable here would try to re-enable the wrong unit manager.
            assert "--user" in line, line


def test_revert_never_contains_a_disable_verb(fixture_inventories) -> None:
    """A revert script that can disable things is not a revert script."""
    rows = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    script = gen.render_revert("node-100", rows)
    assert " disable " not in script
    assert " stop " not in script


# ------------------------------------------------------------- markdown ---


def test_markdown_row_count_matches_the_unit_count(fixture_inventories) -> None:
    rows = gen.build_rows("node-100", "worker-gpu", FIXTURE_CONTROL)
    md = gen.render_markdown("node-100", "worker-gpu", rows)
    # The unit table has five columns; the summary table above it has two,
    # so count by shape rather than by prefix.
    body = [line for line in md.splitlines() if line.startswith("| `") and line.count("|") == 6]
    assert len(body) == len(rows)


def test_markdown_reports_the_real_control_node_name(fixture_inventories) -> None:
    """There is no node-158: paths.self_node_name() derives node-noroc2027
    from the hostname, which is why admission.PRESETS keys are dead."""
    md = gen.render_markdown("node-100", "worker-gpu", [])
    assert "node-noroc2027" in md
    # node-158 may appear only in the sentence saying it does not exist.
    for line in md.splitlines():
        if "node-158" in line:
            assert "no `node-158`" in line, line


# ---------------------------------------------------------------- guard ---


def test_a_disable_with_no_rationale_is_rejected(
    monkeypatch, fixture_inventories, tmp_path
) -> None:
    """The guard that caught lxd-installer.socket. A silent fallthrough into
    `disable` is how a load-bearing unit gets switched off because no rule
    happened to name it."""
    # Drop the rules that explain skvoice and session-migration. They then
    # fall through to the worker-gpu default of `disable` with no reason,
    # which is precisely the failure mode the guard exists to catch.
    monkeypatch.setattr(gen, "DISABLE_RULES", {}, raising=True)
    monkeypatch.setattr(gen, "NODE_ROLES", {"node-100": "worker-gpu"})
    monkeypatch.setattr(
        "sys.argv",
        [
            "gen",
            "--node",
            "node-100",
            "--out",
            str(tmp_path / "out.md"),
            "--reference",
            "node-fixture-control",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        gen.main()
    assert "no rationale" in str(exc.value)
    assert "skvoice.service" in str(exc.value)


def test_main_writes_all_three_artifacts(monkeypatch, fixture_inventories, tmp_path) -> None:
    md, js, rev = tmp_path / "d.md", tmp_path / "d.json", tmp_path / "revert.sh"
    monkeypatch.setattr(
        "sys.argv",
        [
            "gen",
            "--node",
            "node-100",
            "--out",
            str(md),
            "--json-out",
            str(js),
            "--revert-out",
            str(rev),
            "--reference",
            "node-fixture-control",
        ],
    )
    assert gen.main() == 0
    assert md.exists() and js.exists() and rev.exists()
    payload = json.loads(js.read_text())
    assert payload["unitCount"] == len(FIXTURE_USER) + len(FIXTURE_SYSTEM)
    assert rev.stat().st_mode & 0o111, "revert script must be executable"


def test_unknown_node_is_refused(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("sys.argv", ["gen", "--node", "node-nope", "--out", str(tmp_path / "x")])
    with pytest.raises(SystemExit) as exc:
        gen.main()
    assert "unknown node" in str(exc.value)


def test_missing_inventory_explains_how_to_collect_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gen, "INVENTORY_DIR", tmp_path)
    with pytest.raises(SystemExit) as exc:
        gen.load_inventory("node-100", "user")
    assert "list-unit-files" in str(exc.value)


def test_no_ssh_anywhere_in_the_generator() -> None:
    """Collection is a separate read-only step. Keeping ssh out of the
    generator is what makes the test path and the live path identical."""
    source = SCRIPT.read_text(encoding="utf-8")
    # No way to run anything at all. `sshd.service` appears as a unit NAME in
    # the rules table, which is data, so match on invocation shapes instead
    # of on the substring "ssh".
    for token in (
        "import subprocess",
        "paramiko",
        "os.system",
        "Popen",
        "check_output",
        "os.exec",
    ):
        assert token not in source, f"{token!r} found in the generator"
    for shape in ('"ssh"', "'ssh'", '"ssh ', "ssh -o", "ssh cbrd21@"):
        assert shape not in source, f"ssh invocation {shape!r} found in the generator"
