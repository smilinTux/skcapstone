"""Provider-neutral card routing guard tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner

from skcapstone.card import CardEventLog
from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.mcp_tools import coord_card_tools, coord_tools
from skcapstone.routing_guard import classify_card_routing, routing_transition_allowed


def test_creation_defaults_ordinary_sklegal_card_to_sk_m(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00001",
            "--title",
            "SKLegal ordinary work",
            "--tag",
            "sklegal",
        ],
    )

    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("a0a00001").labels == ["sklegal", "sk-m"]


def test_creation_rejects_provider_family_and_multiple_buckets(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    runner = CliRunner()

    provider = runner.invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00002",
            "--title",
            "SKLegal provider-bound work",
            "--tag",
            "sklegal",
            "--tag",
            "codex-only",
        ],
    )
    multiple = runner.invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00003",
            "--title",
            "SKLegal ambiguous work",
            "--tag",
            "sklegal",
            "--tag",
            "sk-s",
            "--tag",
            "sk-xl",
        ],
    )

    assert provider.exit_code != 0
    assert "provider-family-label:codex-only" in provider.output
    assert multiple.exit_code != 0
    assert "multiple-logical-buckets:sk-s,sk-xl" in multiple.output
    assert CardStore(tmp_path).fold("a0a00002") is None
    assert CardStore(tmp_path).fold("a0a00003") is None


@pytest.mark.parametrize("label", ["sk-glm-s", "sk-glm-m", "sk-glm-l"])
def test_exact_glm_bucket_is_valid_and_exclusive(label: str) -> None:
    result = classify_card_routing(["sklegal", label])

    assert result.valid
    assert result.labels == ("sklegal", label)
    assert not classify_card_routing(["sklegal", label, "sk-m"]).valid


@pytest.mark.parametrize(
    "label",
    [
        "astra",
        "claude-only",
        "codex-specific",
        "fable-lane",
        "glm-only",
        "kimi-suitable",
    ],
)
def test_all_required_provider_family_spellings_are_rejected(label: str) -> None:
    result = classify_card_routing(["sklegal", "sk-m", label])

    assert result.diagnostic == f"provider-family-label:{label}"


def test_qwen_and_review_seat_labels_remain_exact(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    parent = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00000",
            "--title",
            "SKLegal source work",
            "--tag",
            "sklegal",
        ],
    )
    assert parent.exit_code == 0, parent.output

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00004",
            "--title",
            "[REVIEW] SKLegal sovereign review",
            "--tag",
            "sklegal",
            "--tag",
            "qwen-first",
            "--tag",
            "review",
            "--tag",
            "seat-link",
            "--tag",
            "parent-a0a00000",
        ],
    )

    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("a0a00004").labels == [
        "sklegal",
        "qwen-first",
        "review",
        "seat-link",
        "parent-a0a00000",
    ]


def test_label_amendment_fails_closed_and_cleanup_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    runner = CliRunner()
    created = runner.invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00005",
            "--title",
            "SKLegal amendment",
            "--tag",
            "sklegal",
        ],
    )
    assert created.exit_code == 0, created.output

    rejected = runner.invoke(
        main,
        ["coord", "label", "a0a00005", "glm-only", "--home", str(tmp_path)],
    )
    repeated = runner.invoke(
        main,
        ["coord", "label", "a0a00005", "sk-m", "--home", str(tmp_path)],
    )

    assert rejected.exit_code != 0
    assert "provider-family-label:glm-only" in rejected.output
    assert "already present" in repeated.output
    assert not CardEventLog(tmp_path).read_all()


def test_legacy_cleanup_can_reduce_violations_one_step_at_a_time() -> None:
    before = ["sklegal", "codex-only"]
    after = ["sklegal"]

    assert routing_transition_allowed(before, after)
    assert not classify_card_routing(after).valid
    assert classify_card_routing([*after, "sk-m"]).valid


def test_concurrent_creation_keeps_one_canonical_card(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    args = [
        "coord",
        "create",
        "--home",
        str(tmp_path),
        "--id",
        "a0a00006",
        "--title",
        "SKLegal concurrent work",
        "--tag",
        "sklegal",
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: CliRunner().invoke(main, args), range(2)))

    assert all(result.exit_code == 0 for result in results)
    assert len([card for card in CardStore(tmp_path).list_cards() if card.id == "a0a00006"]) == 1
    assert CardStore(tmp_path).fold("a0a00006").labels == ["sklegal", "sk-m"]


@pytest.mark.asyncio
async def test_mcp_creation_and_amendment_use_the_same_guard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.setattr(coord_tools, "_home", lambda: tmp_path)
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)

    created = await coord_tools._handle_coord_create(
        {"title": "SKLegal MCP work", "tags": ["sklegal"]}
    )
    created_payload = json.loads(created[0].text)
    card_id = created_payload["task_id"]
    assert CardStore(tmp_path).fold(card_id).labels == ["sklegal", "sk-m"]

    rejected = await coord_card_tools._handle_coord_label(
        {"task_id": card_id, "label": "astra-only"}
    )
    assert "provider-family-label:astra-only" in json.loads(rejected[0].text)["error"]
    assert not CardEventLog(tmp_path).read_all()
