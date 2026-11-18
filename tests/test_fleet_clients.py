"""skfleet-clients: spec render, host agent merge/install/rollback, ring halts."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from skcapstone.fleet import clients as spec_mod
from skcapstone.fleet import clients_ring as ring

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src/skcapstone/data/skfleet-clients-agent.py"

ROWS = [
    {"id": "sk-m", "owned_by": "skgateway", "advertised": True, "stale": False},
    {"id": "sk-x", "owned_by": "skgateway", "advertised": None},
    {
        "id": "gpt-a",
        "owned_by": "codex",
        "advertised": True,
        "vision": True,
        "card": {"display_name": "GPT A", "context_length": 272000},
    },
    {"id": "gpt-old", "owned_by": "codex", "advertised": True, "stale": True},
    {"id": "gpt-skip", "owned_by": "codex", "advertised": True},
]


def write_spec(root: Path, *, ring_hosts=("cana", "h1", "h2"), release_url="", sha="") -> Path:
    c = root / "clients"
    (c / "templates").mkdir(parents=True)
    pins = {
        "schema": 1,
        "install_root": "~/opt",
        "bin_dir": "~/.local/bin",
        "gateway": "http://gw:1",
        "canary": ring_hosts[0],
        "ring": list(ring_hosts),
        "notify": ["jarvis"],
        "clients": {
            "pi": {
                "version": "1.0.0",
                "source": "release",
                "url": release_url or "file:///x",
                "sha256": sha or "0" * 64,
                "entry": "pi",
                "env": {"PI_SKIP_VERSION_CHECK": "1"},
            },
        },
    }
    (c / "pins.yaml").write_text(json.dumps(pins))
    tpl = {
        "client": "pi",
        "file": "~/.pi/agent/models.json",
        "format": "json",
        "preserve": ["apiKey", "skfleet_catalog_sync"],
        "providers": {
            "providers.skgateway": {
                "baseUrl": "{gateway}/v1",
                "apiKey": "$secret:skgateway",
                "models": [
                    {"id": "sk-m", "name": "M"},
                    {"id": "gpt-old", "name": "old"},
                    {"id": "gone", "name": "gone"},
                ],
                "auto_add": {"owners": ["skgateway", "codex"], "exclude": ["gpt-skip"]},
            }
        },
    }
    (c / "templates" / "pi-models.yaml").write_text(json.dumps(tpl))
    return root


def test_render_keeps_curated_drops_retired_and_adds_new(tmp_path):
    spec = spec_mod.load_spec(write_spec(tmp_path))
    desired = spec_mod.render_host(spec, "cana", ROWS)
    block = desired["files"][0]["set"]["providers.skgateway"]
    assert block["baseUrl"] == "http://gw:1/v1"
    assert block["apiKey"] == "$secret:skgateway"
    ids = [m["id"] for m in block["models"]]
    # curated sk-m kept; stale gpt-old and vanished "gone" dropped; sk-x is not
    # advertised; gpt-skip excluded; gpt-a auto-added with card metadata.
    assert ids == ["sk-m", "gpt-a"]
    assert block["models"][1] == {
        "id": "gpt-a",
        "name": "GPT A",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 272000,
    }
    assert desired["pi_models"] == [["skgateway", "sk-m"], ["skgateway", "gpt-a"]]


def test_spec_rejects_ring_without_canary_first(tmp_path):
    root = write_spec(tmp_path)
    pins = json.loads((root / "clients/pins.yaml").read_text())
    pins["canary"] = "h1"
    (root / "clients/pins.yaml").write_text(json.dumps(pins))
    with pytest.raises(spec_mod.SpecError):
        spec_mod.load_spec(root)


def test_model_drop_fraction(tmp_path):
    spec = spec_mod.load_spec(write_spec(tmp_path))
    full = spec_mod.render_host(spec, "cana", ROWS)
    half = spec_mod.render_host(spec, "cana", [r for r in ROWS if r["id"] != "gpt-a"])
    assert spec_mod.model_drop(full, half) == 0.5
    assert spec_mod.model_drop(None, half) == 0.0


# ------------------------------------------------------------------ agent


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    loaded = importlib.util.spec_from_file_location("skfleet_clients_agent_t", AGENT)
    mod = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(mod)
    mod.resolve = lambda name: [str(home / ".local/bin" / name)]
    return mod


def test_merge_preserves_live_secret_and_unmanaged_keys(tmp_path, agent):
    models = agent.HOME / ".pi/agent/models.json"
    models.parent.mkdir(parents=True)
    models.write_text(
        json.dumps(
            {
                "providers": {
                    "skgateway": {
                        "baseUrl": "http://chiap01:18790/v1",
                        "apiKey": "LIVEKEY",
                        "skfleet_catalog_sync": {"v": 2},
                        "models": [],
                    },
                    "local": {"baseUrl": "http://x", "models": [{"id": "keep"}]},
                }
            }
        )
    )
    spec = spec_mod.load_spec(write_spec(tmp_path))
    fspec = spec_mod.render_host(spec, "cana", ROWS)["files"][0]
    new, old, missing, _ = agent.render_file(fspec, agent.gateway_secrets())
    doc = json.loads(new)
    assert missing == []
    assert doc["providers"]["skgateway"]["apiKey"] == "LIVEKEY"
    assert doc["providers"]["skgateway"]["skfleet_catalog_sync"] == {"v": 2}
    assert doc["providers"]["local"]["models"] == [{"id": "keep"}]
    summary = agent.config_summary(fspec, new, old)
    assert summary["changed"] == ["providers.skgateway"]
    assert "LIVEKEY" not in json.dumps(summary)


def test_missing_secret_is_reported_not_written(tmp_path, agent):
    spec = spec_mod.load_spec(write_spec(tmp_path))
    fspec = spec_mod.render_host(spec, "cana", ROWS)["files"][0]
    _, _, missing, _ = agent.render_file(fspec, {})
    assert missing == ["skgateway"]


def test_merge_toml_sets_top_level_only(agent):
    text = 'model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n'
    out = agent.merge_toml(text, {"check_for_update_on_startup": False, "model": "y"})
    assert out.splitlines()[0] == "check_for_update_on_startup = false"
    assert 'model = "y"' in out and '[projects."/a"]' in out
    assert agent.merge_toml(out, {"check_for_update_on_startup": False}) == out


def _release(tmp_path: Path) -> tuple[str, str]:
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho pi 1.0.0\n"
        info = tarfile.TarInfo("pi")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    path = tmp_path / "pi.tar.gz"
    path.write_bytes(blob.getvalue())
    return path.as_uri(), hashlib.sha256(blob.getvalue()).hexdigest()


def test_apply_gate_rollback_roundtrip(tmp_path, agent):
    url, sha = _release(tmp_path)
    spec = spec_mod.load_spec(write_spec(tmp_path / "repo", release_url=url, sha=sha))
    models = agent.HOME / ".pi/agent/models.json"
    models.parent.mkdir(parents=True)
    original = json.dumps(
        {
            "providers": {
                "skgateway": {"baseUrl": "http://chiap01:18790/v1", "apiKey": "K", "models": []}
            }
        }
    )
    models.write_text(original)
    old_bin = agent.HOME / ".local/bin/pi"
    old_bin.parent.mkdir(parents=True)
    old_bin.write_text("#!/bin/sh\necho pi 0.9.0\n")
    old_bin.chmod(0o755)

    desired = spec_mod.render_host(spec, "cana", ROWS)
    desired["pi_models"] = []  # no real pi binary to list models
    report = agent.apply(desired)
    assert report["errors"] == [] and report["installs"] == {"pi": "installed"}
    shim = old_bin.read_text()
    assert "PI_SKIP_VERSION_CHECK='1'" in shim and "/opt/pi/1.0.0/pi" in shim
    assert json.loads(models.read_text())["providers"]["skgateway"]["apiKey"] == "K"
    gated = agent.gate(desired)
    assert gated["ok"], gated["failures"]
    assert agent.apply(desired)["installs"] == {"pi": "present"}

    agent.rollback({})
    assert old_bin.read_text() == "#!/bin/sh\necho pi 0.9.0\n"
    assert models.read_text() == original


def test_release_sha_mismatch_fails_install(tmp_path, agent):
    url, _ = _release(tmp_path)
    spec = spec_mod.load_spec(write_spec(tmp_path / "repo", release_url=url, sha="1" * 64))
    report = agent.apply(spec_mod.render_host(spec, "cana", ROWS))
    assert report["errors"] and "sha256 mismatch" in report["errors"][0]


# ------------------------------------------------------------------ ring


class FakeAgent:
    def __init__(self, fail: dict | None = None):
        self.fail = fail or {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, host, action, payload, timeout=0):
        self.calls.append((host, action))
        if self.fail.get(host) == action:
            return {"ok": False, "failures": [f"{action} broke"], "errors": [f"{action} broke"]}
        return {"ok": True, "errors": [], "results": {}}


def _tick(tmp_path, agent, mails, **kw):
    repo = tmp_path / "repo"
    spec = spec_mod.load_spec(repo if repo.exists() else write_spec(repo))
    return ring.tick(
        spec,
        ROWS,
        state=ring.RingState(tmp_path / "state"),
        agent=agent,
        probe=kw.pop("probe", lambda h: True),
        busy=lambda h: "",
        mail=lambda to, s, b: mails.append(s),
        **kw,
    )


def test_ring_rolls_canary_first_with_smoke_then_noops(tmp_path):
    fake, mails = FakeAgent(), []
    out = _tick(tmp_path, fake, mails)
    assert out.status == "rolled" and out.rolled == ["cana", "h1", "h2"]
    assert ("cana", "smoke") in fake.calls and ("h1", "smoke") not in fake.calls
    assert fake.calls[0] == ("cana", "apply")
    assert mails == ["skfleet-clients ROLLED"]
    assert _tick(tmp_path, fake, mails).status == "noop"


def test_ring_halts_at_first_failure_and_rolls_that_host_back(tmp_path):
    fake, mails = FakeAgent({"h1": "gate"}), []
    out = _tick(tmp_path, fake, mails)
    assert out.status == "halted" and out.halted_at == "h1" and out.rolled == ["cana"]
    assert ("h1", "rollback") in fake.calls
    assert not any(h == "h2" for h, _ in fake.calls)
    assert mails == ["skfleet-clients HALTED at h1"]
    # Same state is not retried (or re-mailed) within the hour.
    again = _tick(tmp_path, FakeAgent(), mails)
    assert again.status == "halted" and mails == ["skfleet-clients HALTED at h1"]
    assert _tick(tmp_path, FakeAgent(), mails, force=True).status == "rolled"


def test_ring_skips_unreachable_and_reports_partial(tmp_path):
    fake, mails = FakeAgent(), []
    out = _tick(tmp_path, fake, mails, probe=lambda h: h != "h1")
    assert out.status == "partial" and out.skipped == ["h1"] and out.rolled == ["cana", "h2"]
    assert mails == []
    out = _tick(tmp_path, fake, mails)
    assert out.status == "rolled"
    assert [c for c in fake.calls if c[0] == "cana"].count(("cana", "apply")) == 1


def test_ring_halts_on_large_model_drop(tmp_path):
    fake, mails = FakeAgent(), []
    _tick(tmp_path, fake, mails)
    spec = spec_mod.load_spec(tmp_path / "repo")
    out = ring.tick(
        spec,
        [r for r in ROWS if r["id"] == "sk-m"],
        state=ring.RingState(tmp_path / "state"),
        agent=fake,
        probe=lambda h: True,
        busy=lambda h: "",
        mail=lambda to, s, b: mails.append(s),
    )
    assert out.status == "halted" and "drop" in out.reason


def test_bump_pins_and_gateway_env():
    pins = (
        "clients:\n  pi:\n    version: 1.0.0\n  opencode:\n    version: 2.2.5\n"
        "    sha256: aaa\n  codex:\n    version: 0.1.0\n"
    )
    out = ring.bump_pins_text(pins, "opencode", "2.3.2", "bbb")
    assert "    version: 2.3.2\n    sha256: bbb\n" in out
    assert "  pi:\n    version: 1.0.0" in out and "  codex:\n    version: 0.1.0" in out
    unit = "Environment=SKGATEWAY_CODEX_CLI_VERSION=0.170.0\nEnvironment=X=1\n"
    assert ring.bump_gateway_env(unit, "SKGATEWAY_CODEX_CLI_VERSION", "0.171.0").startswith(
        "Environment=SKGATEWAY_CODEX_CLI_VERSION=0.171.0\n"
    )
