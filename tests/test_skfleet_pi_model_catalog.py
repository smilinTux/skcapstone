from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fleet/skfleet-pi-model-catalog.py"
ROUTING_DOC = ROOT / "docs/fleet/model-lane-routing.md"


def _module():
    spec = importlib.util.spec_from_file_location("pi_model_catalog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _document(models=None):
    return {
        "providers": {
            "skgateway": {
                "opaqueReference": "preserve-me",
                "models": models
                or [
                    {
                        "id": "qwen3.8-chiap08",
                        "name": "stale served model",
                        "contextWindow": 131072,
                    },
                    {
                        "id": "glm-4.6",
                        "name": "GLM-4.6 via SKGateway (z.ai)",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 200000,
                    },
                ],
            }
        }
    }


def _inventory():
    return [
        {
            "id": "sk-s",
            "advertised": True,
            "stale": False,
            "tools": True,
            "card": {"size_class": "S", "reasoning": True},
        },
        {
            "id": "sk-m",
            "advertised": True,
            "stale": False,
            "tools": True,
            "card": {"size_class": "M", "reasoning": True},
        },
        {
            "id": "route-removed",
            "advertised": False,
            "stale": True,
            "tools": True,
            "card": {"size_class": "L", "reasoning": True},
        },
        {
            "id": "glm-4.6",
            "advertised": True,
            "stale": False,
            "tools": True,
            "name": "GLM-4.6 via SKGateway (z.ai)",
            "card": {"size_class": "M", "reasoning": True, "context_window": 200000},
        },
    ]


def test_reconcile_keeps_only_currently_advertised_routes_and_drops_stale_served_names():
    module = _module()
    original = _document()
    updated, changed = module.reconcile(original, _inventory(), gateway_revision="a" * 40)
    models = updated["providers"]["skgateway"]["models"]
    by_id = {item["id"]: item for item in models}
    assert "qwen3.8-chiap08" not in by_id
    assert "route-removed" not in by_id
    assert set(by_id) == {"glm-4.6", "sk-m", "sk-s"}
    assert updated["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    assert original == _document()
    assert "qwen3.8-chiap08" in changed
    sync = updated["providers"]["skgateway"][module.SYNC_KEY]
    assert sync["gateway_revision"] == "a" * 40
    assert sync["invalidated"] is True
    assert by_id["sk-s"]["reasoning"] is True


def test_reconcile_invalidates_when_gateway_revision_or_inventory_changes():
    module = _module()
    first, _ = module.reconcile(_document(), _inventory(), gateway_revision="a" * 40)
    second, changed = module.reconcile(first, _inventory(), gateway_revision="b" * 40)
    assert changed
    assert second["providers"]["skgateway"][module.SYNC_KEY]["gateway_revision"] == "b" * 40
    slim = [row for row in _inventory() if row["id"] in {"sk-s", "sk-m"}]
    third, removed = module.reconcile(second, slim, gateway_revision="b" * 40)
    assert "glm-4.6" not in {item["id"] for item in third["providers"]["skgateway"]["models"]}
    assert "glm-4.6" in removed


def test_choose_fallback_route_walks_size_capacity_without_served_names():
    module = _module()
    assert module.choose_fallback_route(["glm-4.6", "sk-m", "sk-xl"], required_size="S") == "sk-m"
    assert module.choose_fallback_route(["sk-xl", "sk-l"], required_size="M") == "sk-l"
    assert module.choose_fallback_route(["served-old"], required_size="S") == "served-old"
    assert module.choose_fallback_route([], required_size="S") is None


def test_repair_default_model_replaces_stale_selection():
    module = _module()
    settings = {"defaultProvider": "skgateway", "defaultModel": "qwen3.8-chiap08"}
    updated, fallback = module.repair_default_model(settings, ["sk-s", "sk-m"], required_size="S")
    assert fallback == "sk-s"
    assert updated["defaultModel"] == "sk-s"
    assert settings["defaultModel"] == "qwen3.8-chiap08"
    same, again = module.repair_default_model(updated, ["sk-s", "sk-m"], required_size="S")
    assert again is None
    assert same["defaultModel"] == "sk-s"


def test_reconcile_refuses_empty_or_malformed_inventory_and_ids():
    module = _module()
    with pytest.raises(ValueError, match="no selectable routes"):
        module.reconcile(_document(), [{"id": "dead", "advertised": False, "stale": True}])
    document = _document()
    document["providers"]["skgateway"]["models"].append({"id": []})
    with pytest.raises(ValueError, match="every model id must be a non-empty string"):
        module.reconcile(document, _inventory())


def test_atomic_write_preserves_mode_and_unrelated_fields(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o600)
    updated, changed, info = module.load_and_reconcile(
        path, _inventory(), gateway_revision="c" * 40
    )
    assert changed
    module.write_atomic(path, updated, info)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    assert "qwen3.8-chiap08" not in {
        item["id"] for item in saved["providers"]["skgateway"]["models"]
    }


def test_rejects_insecure_catalog(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="group or other"):
        module.load_and_reconcile(path, _inventory())


def test_rejects_symlink(tmp_path: Path):
    module = _module()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_document()), encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "models.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        module.load_and_reconcile(link, _inventory())


def test_cli_reproduces_stale_default_404_path_and_repairs_without_host_literals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    catalog = tmp_path / "models.json"
    settings = tmp_path / "settings.json"
    inventory = tmp_path / "inventory.json"
    catalog.write_text(json.dumps(_document()), encoding="utf-8")
    catalog.chmod(0o600)
    settings.write_text(
        json.dumps({"defaultProvider": "skgateway", "defaultModel": "qwen3.8-chiap08"}),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    inventory.write_text(json.dumps({"data": _inventory()}), encoding="utf-8")
    monkeypatch.delenv("SKFLEET_GATEWAY_URL", raising=False)
    dry = subprocess.run(
        [
            str(SCRIPT),
            "--catalog",
            str(catalog),
            "--settings",
            str(settings),
            "--inventory",
            str(inventory),
            "--gateway-revision",
            "d" * 40,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 1
    assert dry.stderr == ""
    assert "PI_MODEL_CATALOG|changed|" in dry.stdout
    assert "qwen3.8-chiap08" in catalog.read_text()
    assert json.loads(settings.read_text())["defaultModel"] == "qwen3.8-chiap08"

    applied = subprocess.run(
        [
            str(SCRIPT),
            "--catalog",
            str(catalog),
            "--settings",
            str(settings),
            "--inventory",
            str(inventory),
            "--gateway-revision",
            "d" * 40,
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0
    saved_catalog = json.loads(catalog.read_text())
    saved_settings = json.loads(settings.read_text())
    assert {item["id"] for item in saved_catalog["providers"]["skgateway"]["models"]} == {
        "glm-4.6",
        "sk-m",
        "sk-s",
    }
    assert saved_settings["defaultModel"] == "sk-s"
    assert "chiap01" not in SCRIPT.read_text(encoding="utf-8")
    assert "qwen3.8-chiap08" not in SCRIPT.read_text(encoding="utf-8")


def test_cli_reports_one_sanitized_line_without_traceback_or_catalog(tmp_path: Path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o664)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"data": _inventory()}), encoding="utf-8")
    result = subprocess.run(
        [str(SCRIPT), "--catalog", str(path), "--inventory", str(inventory), "--apply"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "PI_MODEL_CATALOG_ERROR|ValueError|" "catalog must not be accessible by group or other"
    ]
    assert "Traceback" not in result.stderr
    assert "preserve-me" not in result.stderr


def test_launcher_reconciles_before_logical_alias_activation(monkeypatch, tmp_path: Path):
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert source.index("def _prepare_pi_glm_catalog") < source.index("LANES=[")
    assert source.index("_prepare_pi_glm_catalog()") < source.index("LANES=[")
    assert '"target":0 if glm_held or not glm_catalog_ready else GLM_TARGET' in source
    assert "SKFLEET_GATEWAY_URL" in source[source.index("def _prepare_pi_glm_catalog") :]

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="PI_MODEL_CATALOG|current", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    namespace = {
        "Path": Path,
        "HOME": str(tmp_path),
        "__file__": str(ROOT / "scripts/fleet/skfleet-rotate.py"),
        "subprocess": subprocess,
        "sys": __import__("sys"),
        "os": os,
        "_GATEWAY_ENDPOINT": "https://gateway.example/v1",
    }
    tree = __import__("ast").parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, __import__("ast").FunctionDef)
        and node.name == "_prepare_pi_glm_catalog"
    )
    exec(
        compile(__import__("ast").Module(body=[function], type_ignores=[]), "rotate", "exec"),
        namespace,
    )
    assert namespace["_prepare_pi_glm_catalog"]() == (
        True,
        "PI_MODEL_CATALOG|current",
    )
    assert calls[0][0][-1] == "--apply"
    assert calls[0][1]["env"]["SKFLEET_GATEWAY_URL"] == "https://gateway.example/v1"

    def refuse(argv, **kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout="",
            stderr=(
                "PI_MODEL_CATALOG_ERROR|ValueError|"
                "catalog must not be accessible by group or other\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", refuse)
    assert namespace["_prepare_pi_glm_catalog"]() == (
        False,
        "PI_MODEL_CATALOG_ERROR|ValueError|" "catalog must not be accessible by group or other",
    )


def test_launcher_disables_only_glm_when_catalog_reconciliation_fails():
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert '"target":0 if glm_held or not glm_catalog_ready else GLM_TARGET' in source
    codex_stanza = source[source.index("LANES=[") : source.index("_GLM_LEVEL_DEFAULTS")]
    assert '"name":"codex"' in codex_stanza
    assert '"target":TARGET' in codex_stanza


def test_install_contract_requires_current_advertised_inventory_before_activation():
    contract = ROUTING_DOC.read_text(encoding="utf-8")
    preserve = contract.index("Preserve the exact catalog bytes and original mode")
    harden = contract.index("atomically replace it with the")
    reconcile = contract.index("Invoke `skfleet-pi-model-catalog.py --apply`")
    activate = contract.index("install or activate the alias-selecting")
    assert preserve < harden < reconcile < activate
    assert "currently advertised" in contract
    assert "gateway revision" in contract
    assert "defaultModel" in contract
    assert "never\nnormalizes unsafe input itself" in contract
