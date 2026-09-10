"""Card size must select the codex role bucket, not the frontier model every time."""

import importlib.machinery
import importlib.util
import pathlib
import sys

LAUNCHER = pathlib.Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"


def _load():
    spec = importlib.util.spec_from_loader(
        "rot", importlib.machinery.SourceFileLoader("rot", str(LAUNCHER))
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rot"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_each_card_size_picks_its_own_codex_bucket() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert '_CODEX_LEVEL_DEFAULTS={"S":"sk-codex-fast","M":"sk-codex-mid",' in source
    assert '"L":"sk-codex","XL":"sk-codex"}' in source


def test_the_codex_lane_default_is_mid_not_frontier() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert '{"name":"codex","prefix":"codex-auto-","model":"sk-codex-mid",' in source
    # sk-codex is the frontier role and the right default for a hand-run pi
    # session. A fleet lane that sizes its own work must never default to it.
    assert '"model":"sk-codex",' not in source


def test_lane_model_routes_codex_through_the_size_map() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'if lane["name"]=="codex":' in source
    assert '_codex_model_for(core) or lane["model"]' in source


def test_an_unsized_title_falls_back_to_the_lane_default() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    # _codex_model_for returns None when no [S]/[M]/[L]/[XL] marker is present,
    # so the `or lane["model"]` fallback is what keeps an unsized card dispatchable.
    assert "return _CODEX_LEVELS.get(match.group(1)) if match else None" in source


def test_buckets_are_roles_the_gateway_registry_actually_defines() -> None:
    # registry.yaml maps: sk-codex-fast -> codex-fast, sk-codex-mid -> codex-mid,
    # sk-codex -> codex-frontier. Raw gpt-* names would bypass that indirection
    # and force a fleet redeploy whenever the gateway re-points a bucket.
    source = LAUNCHER.read_text(encoding="utf-8")
    start = source.index("_CODEX_LEVEL_DEFAULTS")
    block = source[start : start + 200]
    assert "gpt-5" not in block, "buckets must be roles, not raw model names"
