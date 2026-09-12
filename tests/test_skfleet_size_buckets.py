"""Card size must select a provider-neutral gateway bucket, not a pinned vendor."""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

LAUNCHER = pathlib.Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"
NAMES = {"_LOGICAL_ROUTES", "_SIZE_MODEL_DEFAULTS", "_SIZE_MODELS", "_GLM_SIZE_RE"}
FUNCTIONS = {"_size_model_for", "_lane_model"}


def _namespace() -> dict[str, object]:
    """Execute only the size-bucket declarations from the rotation script."""

    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in NAMES for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS)
    ]
    namespace: dict[str, object] = {"os": os, "re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAUNCHER), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("size", "bucket"),
    [("S", "sk-s"), ("M", "sk-m"), ("L", "sk-l"), ("XL", "sk-l")],
)
def test_each_card_size_defaults_to_its_generic_capability_bucket(
    monkeypatch: pytest.MonkeyPatch, size: str, bucket: str
) -> None:
    for level in ("S", "M", "L", "XL"):
        monkeypatch.delenv("SKFLEET_MODEL_" + level, raising=False)
        monkeypatch.delenv("SKFLEET_CODEX_MODEL_" + level, raising=False)
    namespace = _namespace()

    assert namespace["_size_model_for"]({"title": f"[CARD][{size}] Work"}) == bucket


def test_defaults_name_no_provider_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """An estate on Claude, OpenRouter, or NIM must need no edit to this file."""

    for level in ("S", "M", "L", "XL"):
        monkeypatch.delenv("SKFLEET_MODEL_" + level, raising=False)
        monkeypatch.delenv("SKFLEET_CODEX_MODEL_" + level, raising=False)
    namespace = _namespace()

    defaults = namespace["_SIZE_MODEL_DEFAULTS"]
    assert defaults == namespace["_LOGICAL_ROUTES"]
    assert not any(
        vendor in value
        for value in defaults.values()
        for vendor in ("codex", "glm", "kimi", "zai", "qwen", "gpt", "claude")
    )


@pytest.mark.parametrize("variable", ["SKFLEET_MODEL_{}", "SKFLEET_CODEX_MODEL_{}"])
def test_operator_configuration_wins_over_the_default(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Both the current and the deprecated variable name override the bucket."""

    for level in ("S", "M", "L", "XL"):
        monkeypatch.delenv("SKFLEET_MODEL_" + level, raising=False)
        monkeypatch.delenv("SKFLEET_CODEX_MODEL_" + level, raising=False)
        monkeypatch.setenv(variable.format(level), "sk-codex-mid")
    namespace = _namespace()

    assert namespace["_size_model_for"]({"title": "[CARD][L] Work"}) == "sk-codex-mid"


def test_the_current_variable_name_outranks_the_deprecated_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKFLEET_MODEL_M", "sk-m-internal")
    monkeypatch.setenv("SKFLEET_CODEX_MODEL_M", "sk-codex-mid")
    namespace = _namespace()

    assert namespace["_size_model_for"]({"title": "[CARD][M] Work"}) == "sk-m-internal"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_override_cannot_blank_a_bucket(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A stray empty systemd Environment= line must not dispatch to an empty id."""

    monkeypatch.setenv("SKFLEET_MODEL_XL", blank)
    monkeypatch.delenv("SKFLEET_CODEX_MODEL_XL", raising=False)
    namespace = _namespace()

    assert namespace["_size_model_for"]({"title": "[CARD][XL] Work"}) == "sk-l"


def test_an_unsized_title_falls_back_to_the_lane_default() -> None:
    namespace = _namespace()

    assert namespace["_size_model_for"]({"title": "[CARD] Missing size"}) is None
    assert namespace["_size_model_for"](None) is None
    assert (
        namespace["_lane_model"]({"name": "codex", "model": "lane-default"}, {"title": "unsized"})
        == "lane-default"
    )


def test_xl_uses_the_reviewed_large_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet exposes three reviewed T-shirt routes, so XL folds onto L."""

    for level in ("S", "M", "L", "XL"):
        monkeypatch.delenv("SKFLEET_MODEL_" + level, raising=False)
        monkeypatch.delenv("SKFLEET_CODEX_MODEL_" + level, raising=False)
    namespace = _namespace()
    buckets = namespace["_SIZE_MODELS"]

    assert set(buckets.values()) == {"sk-s", "sk-m", "sk-l"}
    assert namespace["_size_model_for"]({"title": "[CARD][L] Work"}) == "sk-l"
    assert namespace["_size_model_for"]({"title": "[CARD][XL] Work"}) == "sk-l"


def test_the_codex_lane_fallback_is_operator_configurable() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'os.environ.get("SKFLEET_CODEX_LANE_MODEL","sk-codex-mid")' in source
    # sk-codex is the frontier role. A fleet that sizes its own work must never
    # default to it, with or without the rename.
    assert '"model":"sk-codex",' not in source
