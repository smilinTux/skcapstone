from __future__ import annotations

import importlib.util
import json
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


def _document():
    return {
        "providers": {
            "skgateway": {
                "opaqueReference": "preserve-me",
                "models": [
                    {
                        "id": "glm-4.6",
                        "name": "GLM-4.6 via SKGateway (z.ai)",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 200000,
                    },
                    {
                        "id": "glm-4.7",
                        "name": "GLM-4.7 via SKGateway (z.ai)",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": 200000,
                    },
                ],
            }
        }
    }


def test_reconcile_adds_managed_sources_and_six_routes_preserving_secret_fields():
    module = _module()
    original = _document()
    updated, changed = module.reconcile(original)
    models = updated["providers"]["skgateway"]["models"]
    by_id = {item["id"]: item for item in models}
    assert changed == [
        "glm-5.3",
        "kimi-for-coding",
        "kimi-for-coding-highspeed",
        "k3",
        "k3-256k",
        *module.ALIASES,
    ]
    assert updated["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    assert original == _document()
    assert set(module.ALIASES) <= by_id.keys()
    assert by_id["sk-glm-l"]["contextWindow"] == 200000
    assert by_id["sk-zai-s"]["reasoning"] is True
    assert len(models) == 13


def test_reconcile_bootstraps_chiap04_like_catalog_without_copying_host_state():
    module = _module()
    document = _document()
    document["providers"]["skgateway"]["models"] = [
        {
            "id": "host-only-model",
            "opaqueHostField": "preserve-me-too",
        }
    ]
    updated, changed = module.reconcile(document)
    by_id = {item["id"]: item for item in updated["providers"]["skgateway"]["models"]}
    assert changed == [item["id"] for item in module.SOURCE_MODELS] + list(module.ALIASES)
    assert by_id["host-only-model"] == {
        "id": "host-only-model",
        "opaqueHostField": "preserve-me-too",
    }
    assert by_id["glm-4.6"]["contextWindow"] == 200000
    assert by_id["kimi-for-coding"]["contextWindow"] == 262144
    assert by_id["k3"]["contextWindow"] == 1000000


def test_reconcile_refuses_conflicting_managed_source_metadata():
    module = _module()
    document = _document()
    document["providers"]["skgateway"]["models"][0]["contextWindow"] = 1
    with pytest.raises(ValueError, match="conflicting source model metadata: glm-4.6"):
        module.reconcile(document)


@pytest.mark.parametrize("model_id", [[], {}])
def test_reconcile_refuses_non_string_model_ids(model_id):
    module = _module()
    document = _document()
    document["providers"]["skgateway"]["models"].append({"id": model_id})
    with pytest.raises(ValueError, match="every model id must be a non-empty string"):
        module.reconcile(document)


def test_reconcile_is_idempotent_and_refuses_alias_drift():
    module = _module()
    updated, _ = module.reconcile(_document())
    second, changed = module.reconcile(updated)
    assert changed == []
    assert second == updated
    by_id = {item["id"]: item for item in second["providers"]["skgateway"]["models"]}
    by_id["sk-zai-l"]["contextWindow"] = 1
    with pytest.raises(ValueError, match="conflicting logical alias metadata: sk-zai-l"):
        module.reconcile(second)


@pytest.mark.parametrize("model_id", ["glm-4.6", "sk-glm-s"])
def test_reconcile_refuses_duplicate_managed_ids(model_id: str):
    module = _module()
    updated, _ = module.reconcile(_document())
    models = updated["providers"]["skgateway"]["models"]
    models.append(next(item.copy() for item in models if item["id"] == model_id))
    with pytest.raises(ValueError, match=f"duplicate managed model id: {model_id}"):
        module.reconcile(updated)


def test_atomic_write_preserves_mode_and_unrelated_fields(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o600)
    updated, changed, info = module.load_and_reconcile(path)
    assert changed
    module.write_atomic(path, updated, info)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (
        json.loads(path.read_text())["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    )


def test_normalizes_insecure_catalog(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o664)
    module.load_and_reconcile(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rejects_symlink(tmp_path: Path):
    module = _module()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_document()), encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "models.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        module.load_and_reconcile(link)


def test_cli_reports_one_sanitized_line_without_traceback_or_catalog(tmp_path: Path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o664)
    result = subprocess.run(
        [str(SCRIPT), "--catalog", str(path), "--apply"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("PI_MODEL_CATALOG|changed|")
    assert result.stderr == ""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "Traceback" not in result.stderr
    assert "preserve-me" not in result.stderr


def test_cli_sanitizes_non_string_model_id_without_traceback(tmp_path: Path):
    document = _document()
    document["providers"]["skgateway"]["models"].append({"id": []})
    path = tmp_path / "models.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    result = subprocess.run(
        [str(SCRIPT), "--catalog", str(path), "--apply"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "PI_MODEL_CATALOG_ERROR|ValueError|every model id must be a non-empty string"
    ]
    assert "Traceback" not in result.stderr


def test_launcher_reconciles_before_logical_alias_activation(monkeypatch, tmp_path: Path):
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert source.index("def _prepare_pi_glm_catalog") < source.index("LANES=[")
    assert source.index("_prepare_pi_glm_catalog()") < source.index("LANES=[")
    assert '"target":0 if glm_held or not glm_catalog_ready else GLM_TARGET' in source

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
        "PI_MODEL_CATALOG_ERROR|ValueError|catalog must not be accessible by group or other",
    )


def test_launcher_disables_only_glm_when_catalog_reconciliation_fails():
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert '"target":0 if glm_held or not glm_catalog_ready else GLM_TARGET' in source
    codex_stanza = source[source.index("LANES=[") : source.index("_GLM_LEVEL_DEFAULTS")]
    assert '"name":"codex"' in codex_stanza
    assert '"target":TARGET' in codex_stanza


def test_five_host_install_contract_hardens_before_reconcile_and_activation():
    contract = ROUTING_DOC.read_text(encoding="utf-8")
    preserve = contract.index("Preserve the exact catalog bytes and original mode")
    harden = contract.index("atomically replace it with the")
    reconcile = contract.index("Invoke `skfleet-pi-model-catalog.py --apply`")
    activate = contract.index("install or activate the alias-selecting")
    assert preserve < harden < reconcile < activate
    assert "chiap01, chiap02, chiap03, and chiap08" in contract
    assert "`0600` on\nchiap04" in contract
    assert "launcher baseline on chiap02 is distinct" in contract
    assert "exact catalog bytes and original mode" in contract
    assert "must not substitute the chiap02 launcher" in contract
    assert "never\nnormalizes unsafe input itself" in contract
