from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fleet/skfleet-pi-model-catalog.py"
ROUTING_DOC = ROOT / "docs/fleet/model-lane-routing.md"
NOW = 2_000_000_000.0


def _module():
    name = "pi_model_catalog_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _document(models=None):
    return {
        "providers": {
            "skgateway": {
                "opaqueReference": "preserve-me",
                "models": models
                or [
                    {"id": "qwen3.8-chiap08", "name": "stale served model"},
                    {"id": "glm-4.6", "name": "concrete served model"},
                ],
            }
        }
    }


def _healthy_backends(*names: str):
    health = {}
    queue = {}
    for name in names:
        health[name] = {
            "status": "up",
            "observed": True,
            "lastCheck": NOW * 1000,
            "quarantined": False,
        }
        queue[name] = {
            "capacityDomain": name,
            "members": [name],
            "max": 2,
            "active": 0,
        }
    return health, queue


def _view(models=None, health=None, queue=None):
    module = _module()
    if health is None or queue is None:
        health, queue = _healthy_backends("local-a", "cloud-b")
    return module.GatewayView(
        models=tuple(models if models is not None else _inventory_rows()),
        health=health,
        queue=queue,
        health_status="ok",
        observed_at=NOW,
    )


def _inventory_rows():
    return [
        {
            "id": "sk-s",
            "provider": "local-a",
            "advertised": True,
            "stale": False,
            "tools": True,
            "card": {"size_class": "S", "reasoning": True},
        },
        {
            "id": "sk-m",
            "provider": "cloud-b",
            "advertised": True,
            "stale": False,
            "tools": True,
            "card": {"size_class": "M", "reasoning": True},
        },
        {
            "id": "sk-l",
            "provider": "cloud-b",
            "advertised": True,
            "stale": False,
            "tools": True,
            "card": {"size_class": "L", "reasoning": True},
        },
        {
            "id": "route-removed",
            "provider": "cloud-b",
            "advertised": False,
            "stale": True,
            "tools": True,
            "card": {"size_class": "L", "reasoning": True},
        },
        {
            "id": "glm-4.6",
            "provider": "cloud-b",
            "advertised": True,
            "stale": False,
            "tools": True,
            "name": "GLM-4.6 via SKGateway (z.ai)",
            "card": {"size_class": "M", "reasoning": True, "context_window": 200000},
        },
    ]


def test_reconcile_keeps_only_healthy_logical_routes_and_drops_concrete_ids():
    module = _module()
    original = _document()
    updated, changed = module.reconcile(original, _view(), gateway_revision="a" * 40)
    by_id = {item["id"]: item for item in updated["providers"]["skgateway"]["models"]}
    assert set(by_id) == {"sk-s", "sk-m", "sk-l"}
    assert "glm-4.6" not in by_id
    assert "qwen3.8-chiap08" not in by_id
    assert updated["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    assert original == _document()
    assert "glm-4.6" in changed or "qwen3.8-chiap08" in changed
    sync = updated["providers"]["skgateway"][module.SYNC_KEY]
    assert sync["gateway_revision"] == "a" * 40
    assert sync["schema_version"] == 2


def test_unhealthy_logical_route_is_excluded():
    module = _module()
    health, queue = _healthy_backends("local-a", "cloud-b")
    health["cloud-b"] = {
        "status": "down",
        "observed": True,
        "lastCheck": NOW * 1000,
        "quarantined": False,
    }
    updated, _ = module.reconcile(
        _document(),
        _view(health=health, queue=queue),
        gateway_revision="a" * 40,
    )
    assert {item["id"] for item in updated["providers"]["skgateway"]["models"]} == {"sk-s"}


def test_choose_fallback_never_downgrades_required_size():
    module = _module()
    assert module.choose_fallback_route(["sk-s", "sk-m", "sk-xl"], required_size="L") == "sk-xl"
    assert module.choose_fallback_route(["sk-s", "sk-m"], required_size="L") is None
    assert module.choose_fallback_route(["glm-4.6", "sk-m"], required_size="S") == "sk-m"
    assert module.choose_fallback_route(["served-old"], required_size="S") is None


def test_repair_default_model_refuses_concrete_and_downgrade():
    module = _module()
    settings = {"defaultProvider": "skgateway", "defaultModel": "glm-4.6"}
    updated, fallback = module.repair_default_model(
        settings, ["sk-s", "sk-m", "sk-l"], required_size="M"
    )
    assert fallback == "sk-m"
    assert updated["defaultModel"] == "sk-m"
    with pytest.raises(ValueError, match="no policy-compatible healthy logical route"):
        module.repair_default_model({"defaultModel": "sk-s"}, ["sk-s"], required_size="L")


def test_fingerprint_changes_when_health_or_metadata_changes_not_only_ids():
    module = _module()
    base = _view()
    first = module.inventory_fingerprint(base)
    health, queue = _healthy_backends("local-a", "cloud-b")
    health["cloud-b"]["quarantined"] = True
    second = module.inventory_fingerprint(_view(health=health, queue=queue))
    assert first != second
    rows = _inventory_rows()
    for row in rows:
        if row["id"] == "sk-m":
            row["card"] = {**row["card"], "reasoning": False}
    third = module.inventory_fingerprint(_view(models=rows))
    assert first != third
    same_ids = module.inventory_fingerprint(_view())
    assert same_ids == first


def test_pi_name_or_context_window_change_invalidates_catalog_with_same_ids():
    """Identical logical IDs/revision with changed Pi name/context must rewrite models.json."""
    module = _module()
    revision = "a" * 40
    first, _ = module.reconcile(_document(), _view(), gateway_revision=revision)
    assert {item["id"] for item in first["providers"]["skgateway"]["models"]} == {
        "sk-s",
        "sk-m",
        "sk-l",
    }
    stable, unchanged = module.reconcile(first, _view(), gateway_revision=revision)
    assert unchanged == []
    assert (
        stable["providers"]["skgateway"][module.SYNC_KEY]["inventory_fingerprint"]
        == first["providers"]["skgateway"][module.SYNC_KEY]["inventory_fingerprint"]
    )

    renamed = _inventory_rows()
    for row in renamed:
        if row["id"] == "sk-m":
            row["name"] = "SK-M renamed via SKGateway"
            row["contextWindow"] = 123456
    second, changed = module.reconcile(first, _view(models=renamed), gateway_revision=revision)
    assert changed
    assert (
        second["providers"]["skgateway"][module.SYNC_KEY]["inventory_fingerprint"]
        != first["providers"]["skgateway"][module.SYNC_KEY]["inventory_fingerprint"]
    )
    by_id = {item["id"]: item for item in second["providers"]["skgateway"]["models"]}
    assert by_id["sk-m"]["name"] == "SK-M renamed via SKGateway"
    assert by_id["sk-m"]["contextWindow"] == 123456
    assert {item["id"] for item in second["providers"]["skgateway"]["models"]} == {
        "sk-s",
        "sk-m",
        "sk-l",
    }


def test_reconcile_requires_gateway_revision_and_invalidates_on_change():
    module = _module()
    with pytest.raises(ValueError, match="gateway revision is required"):
        module.reconcile(_document(), _view(), gateway_revision="")
    first, _ = module.reconcile(_document(), _view(), gateway_revision="a" * 40)
    second, changed = module.reconcile(first, _view(), gateway_revision="b" * 40)
    assert changed
    assert second["providers"]["skgateway"][module.SYNC_KEY]["gateway_revision"] == "b" * 40


def test_atomic_write_preserves_mode_and_unrelated_fields(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o600)
    updated, changed, info = module.load_and_reconcile(path, _view(), gateway_revision="c" * 40)
    assert changed
    module.write_atomic(path, updated, info)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["providers"]["skgateway"]["opaqueReference"] == "preserve-me"
    assert {item["id"] for item in saved["providers"]["skgateway"]["models"]} == {
        "sk-s",
        "sk-m",
        "sk-l",
    }


def test_rejects_insecure_catalog(tmp_path: Path):
    module = _module()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="group or other"):
        module.load_and_reconcile(path, _view(), gateway_revision="d" * 40)


def test_cli_repairs_stale_default_using_healthy_logical_routes(tmp_path: Path):
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
    health, queue = _healthy_backends("local-a", "cloud-b")
    # Align lastCheck with wall-clock observed_at used by the CLI inventory loader.
    now_ms = int(__import__("time").time() * 1000)
    for row in health.values():
        row["lastCheck"] = now_ms
    inventory.write_text(
        json.dumps({"data": _inventory_rows(), "health": health, "queue": queue}),
        encoding="utf-8",
    )
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
            "e" * 40,
            "--required-size",
            "S",
            "--apply",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    saved_catalog = json.loads(catalog.read_text())
    saved_settings = json.loads(settings.read_text())
    assert {item["id"] for item in saved_catalog["providers"]["skgateway"]["models"]} == {
        "sk-s",
        "sk-m",
        "sk-l",
    }
    assert saved_settings["defaultModel"] == "sk-s"
    source = SCRIPT.read_text(encoding="utf-8")
    assert "chiap01" not in source
    assert "qwen3.8-chiap08" not in source


def test_cli_reports_sanitized_error_without_traceback(tmp_path: Path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    path.chmod(0o664)
    inventory = tmp_path / "inventory.json"
    health, queue = _healthy_backends("local-a")
    now_ms = int(__import__("time").time() * 1000)
    for row in health.values():
        row["lastCheck"] = now_ms
    inventory.write_text(
        json.dumps({"data": _inventory_rows(), "health": health, "queue": queue}),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            str(SCRIPT),
            "--catalog",
            str(path),
            "--inventory",
            str(inventory),
            "--gateway-revision",
            "f" * 40,
            "--apply",
        ],
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


def test_launcher_requires_gateway_url_passes_revision_and_has_no_host_default(
    monkeypatch, tmp_path: Path
):
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert 'os.environ.get("SKFLEET_GATEWAY_URL","http://chiap01:18790")' not in source
    assert 'os.environ.get("SKFLEET_GATEWAY_URL") or ""' in source
    assert "--gateway-revision" in source
    assert "active_gateway_revision" in source

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="PI_MODEL_CATALOG|current", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    class _FakeHealth:
        @staticmethod
        def active_gateway_revision(endpoint):
            assert endpoint == "https://gateway.example"
            return "a" * 40

    import sys

    monkeypatch.setitem(sys.modules, "skcapstone.fleet_lane_health", _FakeHealth)
    namespace = {
        "Path": Path,
        "HOME": str(tmp_path),
        "__file__": str(ROOT / "scripts/fleet/skfleet-rotate.py"),
        "subprocess": subprocess,
        "sys": sys,
        "os": os,
        "_GATEWAY_ENDPOINT": "https://gateway.example",
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
    assert namespace["_prepare_pi_glm_catalog"]() == (True, "PI_MODEL_CATALOG|current")
    assert calls[0][0][-2:] == ["--gateway-revision", "a" * 40]
    assert calls[0][1]["env"]["SKFLEET_GATEWAY_URL"] == "https://gateway.example"

    namespace["_GATEWAY_ENDPOINT"] = ""
    assert namespace["_prepare_pi_glm_catalog"]() == (False, "SKFLEET_GATEWAY_URL is required")


def test_launcher_disables_only_glm_when_catalog_reconciliation_fails():
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    assert '"target":0 if glm_held or not glm_catalog_ready else GLM_TARGET' in source


def test_install_contract_requires_healthy_logical_inventory_before_activation():
    contract = ROUTING_DOC.read_text(encoding="utf-8")
    assert "currently advertised" in contract
    assert "gateway revision" in contract
    assert "defaultModel" in contract
    assert "Invoke `skfleet-pi-model-catalog.py --apply`" in contract
