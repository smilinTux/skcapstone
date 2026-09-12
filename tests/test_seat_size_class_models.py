"""Seat dispatch asks SKGateway for a size, and operator configuration wins."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import skcapstone.seat_cycle_entrypoint as seat_entrypoint
from skcapstone.seat_cycle_entrypoint import (
    resolve_size_class_models,
    role_dispatch_operation,
    seraph_operation,
)

SIZES = ("S", "M", "L", "XL")
NEUTRAL = {"S": "sk-s", "M": "sk-m", "L": "sk-l", "XL": "sk-l"}


@pytest.fixture(autouse=True)
def installed_dispatcher(tmp_path, monkeypatch):
    """Provide the wheel-owned launcher the seat dispatch paths require."""

    bindir = tmp_path / "skenv-bin"
    bindir.mkdir()
    interpreter = bindir / "python3"
    interpreter.touch(mode=0o755)
    dispatcher = bindir / "skfleet-rotate.py"
    dispatcher.touch(mode=0o755)
    monkeypatch.setattr(seat_entrypoint.sys, "executable", str(interpreter))
    return dispatcher


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Start every case from an estate that has configured nothing."""

    for size in SIZES:
        monkeypatch.delenv(f"SKFLEET_MODEL_{size}", raising=False)
        monkeypatch.delenv(f"SKFLEET_CODEX_MODEL_{size}", raising=False)


def _dispatch_environment(monkeypatch, seat: str, home: Path) -> dict[str, str]:
    """Run one seat dispatch and return the environment handed to the child."""

    captured: dict[str, str] = {}

    def run(command, **kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        captured.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: None)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore._read_events", lambda *_: [])
    if seat == "seraph":
        seraph_operation(home)
    else:
        role_dispatch_operation(home, seat)
    return captured


def test_defaults_are_generic_capability_buckets() -> None:
    """An estate on Claude, OpenRouter free, or NIM needs no code change."""

    resolved = resolve_size_class_models({})

    assert {size: resolved[f"SKFLEET_MODEL_{size}"] for size in SIZES} == NEUTRAL
    assert not any(
        vendor in value
        for value in resolved.values()
        for vendor in ("codex", "glm", "kimi", "zai", "qwen", "gpt", "claude")
    )


def test_operator_configuration_is_never_overwritten_by_the_default() -> None:
    """The actual defect: a configured bucket used to be clobbered by a literal."""

    resolved = resolve_size_class_models(
        {"SKFLEET_MODEL_M": "sk-m-internal", "SKFLEET_MODEL_XL": "sk-codex"}
    )

    assert resolved["SKFLEET_MODEL_M"] == "sk-m-internal"
    assert resolved["SKFLEET_MODEL_XL"] == "sk-codex"
    assert resolved["SKFLEET_MODEL_S"] == "sk-s"
    assert resolved["SKFLEET_MODEL_L"] == "sk-l"


def test_deprecated_variable_name_still_configures_a_bucket() -> None:
    resolved = resolve_size_class_models({"SKFLEET_CODEX_MODEL_L": "sk-codex-mid"})

    assert resolved["SKFLEET_MODEL_L"] == "sk-codex-mid"
    assert resolved["SKFLEET_CODEX_MODEL_L"] == "sk-codex-mid"


def test_current_variable_name_outranks_the_deprecated_alias() -> None:
    resolved = resolve_size_class_models(
        {"SKFLEET_MODEL_S": "sk-s-secret", "SKFLEET_CODEX_MODEL_S": "sk-codex-fast"}
    )

    assert resolved["SKFLEET_MODEL_S"] == "sk-s-secret"
    assert resolved["SKFLEET_CODEX_MODEL_S"] == "sk-s-secret"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_value_falls_back_instead_of_dispatching_an_empty_id(blank: str) -> None:
    resolved = resolve_size_class_models({"SKFLEET_MODEL_M": blank})

    assert resolved["SKFLEET_MODEL_M"] == "sk-m"


def test_unknown_size_names_are_ignored() -> None:
    resolved = resolve_size_class_models({}, sizes=("M", "XXL", ""))

    assert resolved == {"SKFLEET_MODEL_M": "sk-m", "SKFLEET_CODEX_MODEL_M": "sk-m"}


def test_four_card_sizes_use_exactly_three_reviewed_routes() -> None:
    """XL folds onto L while S and M retain their own reviewed routes."""

    resolved = resolve_size_class_models({})
    buckets = [resolved[f"SKFLEET_MODEL_{size}"] for size in SIZES]

    assert set(buckets) == {"sk-s", "sk-m", "sk-l"}
    assert resolved["SKFLEET_MODEL_L"] == "sk-l"
    assert resolved["SKFLEET_MODEL_XL"] == "sk-l"


@pytest.mark.parametrize("seat", ["tank", "atlas"])
def test_role_dispatch_defaults_to_neutral_buckets(tmp_path, monkeypatch, seat) -> None:
    captured = _dispatch_environment(monkeypatch, seat, tmp_path)

    assert {size: captured[f"SKFLEET_MODEL_{size}"] for size in SIZES} == NEUTRAL
    assert {size: captured[f"SKFLEET_CODEX_MODEL_{size}"] for size in SIZES} == NEUTRAL


@pytest.mark.parametrize("seat", ["tank", "atlas"])
def test_role_dispatch_carries_operator_configuration_to_the_child(
    tmp_path, monkeypatch, seat
) -> None:
    """A systemd drop-in used to be silently defeated by env.update after copy."""

    monkeypatch.setenv("SKFLEET_MODEL_M", "sk-m-internal")
    monkeypatch.setenv("SKFLEET_CODEX_MODEL_XL", "sk-kimi-xl")

    captured = _dispatch_environment(monkeypatch, seat, tmp_path)

    assert captured["SKFLEET_MODEL_M"] == "sk-m-internal"
    assert captured["SKFLEET_CODEX_MODEL_M"] == "sk-m-internal"
    assert captured["SKFLEET_MODEL_XL"] == "sk-kimi-xl"
    assert captured["SKFLEET_MODEL_S"] == "sk-s"


def test_seraph_resolves_only_the_size_it_reviews(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_MODEL_S", "sk-s-internal")

    captured = _dispatch_environment(monkeypatch, "seraph", tmp_path)

    assert captured["SKFLEET_MODEL_S"] == "sk-s-internal"
    assert captured["SKFLEET_CODEX_MODEL_S"] == "sk-s-internal"
    assert "SKFLEET_MODEL_L" not in captured


def test_no_provider_pinned_default_survives_in_the_dispatch_path() -> None:
    source = Path(seat_entrypoint.__file__).read_text(encoding="utf-8")

    assert '"SKFLEET_CODEX_MODEL_S": "sk-codex-mid"' not in source
    assert '"SKFLEET_CODEX_MODEL_XL": "sk-codex-mid"' not in source


def _launch_stdout(model: str, seat: str, card_id: str = "a8100007") -> str:
    return (
        f"LAUNCHED|chiap08|codex-auto-{card_id}|{card_id}|lane=codex|"
        f"model={model}|owner=pi-{seat}-chiap08-{card_id}|claim_revision=revision-1\n"
    )


def _verify(monkeypatch, tmp_path: Path, seat: str, model: str) -> dict[str, object]:
    """Run one role dispatch whose launcher reports the given launch model."""

    card_id = "a8100007"
    card = SimpleNamespace(
        labels=[f"seat-{seat}", "dispatch-approved"],
        status=SimpleNamespace(value="doing"),
        owner=f"pi-{seat}-chiap08-{card_id}",
        meta={"_claim_revision": "revision-1"},
    )

    def run(command, **kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=_launch_stdout(model, seat, card_id))

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore._read_events", lambda *_: [])
    return role_dispatch_operation(tmp_path, seat)


@pytest.mark.parametrize("model", ["sk-s", "sk-m", "sk-l"])
def test_verification_accepts_every_bucket_the_dispatch_asked_for(
    tmp_path, monkeypatch, model: str
) -> None:
    """The launcher reports the job's logical route, so verification must take it."""

    assert _verify(monkeypatch, tmp_path, "tank", model)["reason"] == "tank_dispatch_complete"


def test_verification_accepts_a_configured_provider_pinned_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_MODEL_M", "sk-codex-mid")

    assert (
        _verify(monkeypatch, tmp_path, "atlas", "sk-codex-mid")["reason"]
        == "atlas_dispatch_complete"
    )


def test_verification_still_refuses_a_model_the_dispatch_never_requested(
    tmp_path, monkeypatch
) -> None:
    """The gate stays real: an unrequested model is an invalid receipt."""

    result = _verify(monkeypatch, tmp_path, "tank", "sk-glm-l")

    assert result["reason"] == "tank_dispatch_failed"
    assert result["dispatch_succeeded"] == 0
