from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/skcapstone/data/sk-pi-gateway-sync.py"
PICKER = ROOT / "src/skcapstone/data/sk-agent-picker.sh"


def _module():
    spec = importlib.util.spec_from_file_location("sk_pi_gateway_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _catalog(tmp_path: Path, models: list[dict] | None = None) -> Path:
    document = {
        "providers": {
            "ollama": {"baseUrl": "http://localhost:11434/v1", "models": [{"id": "dolphin-*"}]},
            "skgateway": {
                "baseUrl": "http://localhost:18780/v1",
                "api": "openai-completions",
                "apiKey": "not-needed",
                "models": (
                    models
                    if models is not None
                    else [
                        {
                            "id": "sk-default",
                            "name": "SKGateway Auto-Router",
                            "reasoning": True,
                            "contextWindow": 131072,
                            "maxTokens": 32768,
                        }
                    ]
                ),
            },
        }
    }
    path = tmp_path / "models.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _gateway_entry(**overrides) -> dict:
    entry = {
        "id": "qwen3.8-27b",
        "provider": "chiap08-qwen38",
        "card": {
            "display_name": "Qwen3.8 27B",
            "context_length": 131072,
            "max_output_tokens": 16384,
            "min_output_tokens": 8192,
            "reasoning": True,
            "vision": True,
        },
        "advertised": True,
        "ctx_tokens": 131072,
        "vision": True,
    }
    entry.update(overrides)
    return entry


def test_card_backed_model_maps_onto_pi_schema():
    module = _module()
    model = module.to_pi_model(_gateway_entry(), {}, 131072)
    assert model == {
        "id": "qwen3.8-27b",
        "name": "Qwen3.8 27B",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 131072,
        "maxTokens": 16384,
        "min_output_tokens": 8192,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


def test_cardless_bucket_keeps_existing_local_metadata():
    """sk-* routes carry no card, so hand-tuned catalog values must survive."""
    module = _module()
    entry = {"id": "sk-default", "kind": "role", "provider": "skgateway"}
    prior = {
        "sk-default": {
            "id": "sk-default",
            "name": "SKGateway Auto-Router",
            "reasoning": True,
            "contextWindow": 131072,
            "maxTokens": 32768,
            "cost": {"input": 3, "output": 15, "cacheRead": 0, "cacheWrite": 0},
        }
    }
    model = module.to_pi_model(entry, prior, 65536)
    assert model["reasoning"] is True
    assert model["maxTokens"] == 32768
    assert model["contextWindow"] == 131072
    assert model["cost"] == {"input": 3, "output": 15, "cacheRead": 0, "cacheWrite": 0}
    assert model["name"] == "sk-default — SKGateway role"


def test_reconcile_replaces_gateway_block_and_leaves_others_alone(tmp_path):
    module = _module()
    document = json.loads(_catalog(tmp_path).read_text())
    entries = [_gateway_entry(), {"id": "sk-l", "kind": "bucket", "model_class": "L"}]
    updated, changed = module.reconcile(document, entries, "skgateway", "http://localhost:18780")
    assert changed is True
    assert [m["id"] for m in updated["providers"]["skgateway"]["models"]] == [
        "qwen3.8-27b",
        "sk-l",
    ]
    assert updated["providers"]["ollama"] == document["providers"]["ollama"]
    # Reconciling the already-updated document is a no-op.
    _, changed_again = module.reconcile(updated, entries, "skgateway", "http://localhost:18780")
    assert changed_again is False


def test_select_honours_stale_and_regex_filters(monkeypatch):
    module = _module()
    entries = [
        _gateway_entry(id="fresh", stale=False),
        _gateway_entry(id="rotten", stale=True),
        _gateway_entry(id="hidden", advertised=False),
    ]
    assert {item["id"] for item in module.select(entries)} == {"fresh", "rotten"}

    monkeypatch.setenv("SK_PI_SYNC_SKIP_STALE", "1")
    assert {item["id"] for item in module.select(entries)} == {"fresh"}

    monkeypatch.delenv("SK_PI_SYNC_SKIP_STALE")
    monkeypatch.setenv("SK_PI_SYNC_ONLY", "^rot")
    assert {item["id"] for item in module.select(entries)} == {"rotten"}


def test_group_readable_catalog_is_refused(tmp_path):
    module = _module()
    path = _catalog(tmp_path)
    path.chmod(0o640)
    with pytest.raises(ValueError, match="group or other"):
        module._secure_regular_file(path)


def test_unreachable_gateway_exits_zero_without_touching_catalog(tmp_path):
    """A dead gateway must never block a Pi launch, nor edit the catalog."""
    path = _catalog(tmp_path)
    before = path.read_text()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--catalog",
            str(path),
            "--gateway",
            "http://127.0.0.1:1",
            "--timeout",
            "1",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "PI_GATEWAY_SYNC_UNAVAILABLE" in result.stderr
    assert path.read_text() == before


def test_write_atomic_preserves_mode_and_backs_up(tmp_path):
    module = _module()
    path = _catalog(tmp_path)
    info = module._secure_regular_file(path)
    original = path.read_text()
    module.back_up(path, info)
    module.write_atomic(path, {"providers": {}}, info)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    backups = list((tmp_path / "backups").glob("models.json.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_picker_wraps_pi_with_the_sync_and_honours_the_opt_out():
    source = PICKER.read_text()
    assert "function skpisync" in source
    assert 'if [[ "${SK_PI_SYNC:-1}" == "1" ]]; then' in source
    assert "export -f skpisync" in source


def test_probe_keeps_only_models_that_answer(monkeypatch):
    module = _module()
    answered = {"sk-default", "ornith-tiny"}
    monkeypatch.setattr(module, "probe_model", lambda base, mid, timeout: mid in answered)
    entries = [{"id": mid} for mid in ["sk-default", "nvidia/gone", "ornith-tiny", "z-ai/eol"]]
    assert set(module.probe_all("http://gw", entries)) == answered


def test_allowlist_round_trip_and_invalidation(tmp_path, monkeypatch):
    module = _module()
    catalog = _catalog(tmp_path)
    module.write_allowlist(catalog, "http://gw", ["sk-default", "ornith-tiny"])

    assert module.read_allowlist(catalog, "http://gw") == {"sk-default", "ornith-tiny"}
    assert stat.S_IMODE(module.allowlist_path(catalog).stat().st_mode) == 0o600
    # A cache written for a different gateway must not be trusted.
    assert module.read_allowlist(catalog, "http://elsewhere") is None
    # Nor one older than the TTL.
    monkeypatch.setenv("SK_PI_SYNC_PROBE_TTL", "1")
    stale = json.loads(module.allowlist_path(catalog).read_text())
    stale["generated"] = 0
    module.allowlist_path(catalog).write_text(json.dumps(stale))
    assert module.read_allowlist(catalog, "http://gw") is None
    # TTL 0 disables expiry.
    monkeypatch.setenv("SK_PI_SYNC_PROBE_TTL", "0")
    assert module.read_allowlist(catalog, "http://gw") == {"sk-default", "ornith-tiny"}


def test_missing_allowlist_reads_as_none(tmp_path):
    module = _module()
    assert module.read_allowlist(_catalog(tmp_path), "http://gw") is None


def test_probe_failure_for_every_model_leaves_catalog_alone(tmp_path, monkeypatch):
    """If nothing answers, that is a gateway problem — do not empty Pi's picker."""
    module = _module()
    catalog = _catalog(tmp_path)
    before = catalog.read_text()
    monkeypatch.setattr(module, "fetch_models", lambda base, timeout: [{"id": "sk-default"}])
    monkeypatch.setattr(module, "probe_model", lambda base, mid, timeout: False)
    monkeypatch.setattr(
        sys, "argv", ["sync", "--catalog", str(catalog), "--gateway", "http://gw", "--probe"]
    )
    assert module.main() == 0
    assert catalog.read_text() == before
    assert not module.allowlist_path(catalog).exists()
