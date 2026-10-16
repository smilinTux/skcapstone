"""Provider-neutral card routing guard tests.

Covers the exact GLM lane guard built for incident inc-e5ab3dea: provider-
neutral ``sk-s``/``sk-m``/``sk-l``/``sk-xl`` stay valid for ordinary work,
``sk-glm-s``/``sk-glm-m``/``sk-glm-l`` pin a card to GLM at an exact size
with no fallback, provider-family compatibility labels are rejected, and
creation/label amendment share one classifier.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from click.testing import CliRunner

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.mcp_tools import coord_card_tools, coord_tools
from skcapstone.routing_guard import classify_card_routing, routing_transition_allowed


@pytest.fixture(autouse=True)
def _card_store_mirror(monkeypatch):
    """Keep the skcoord CardStore mirroring behaviour the old suite relied on."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")


def test_creation_defaults_ordinary_sklegal_card_to_sk_m(tmp_path) -> None:
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
            "--by",
            "human",
        ],
    )

    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("a0a00001").labels == ["sklegal", "sk-m"]


def test_creation_rejects_provider_family_and_multiple_buckets(tmp_path) -> None:
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
            "--by",
            "human",
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
            "--by",
            "human",
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


def test_qwen_sovereign_and_non_sklegal_labels_stay_outside_the_policy() -> None:
    assert classify_card_routing(["sklegal", "qwen-first"]).valid
    assert classify_card_routing(["sklegal", "qwen-only", "sovereign-qwen"]).valid
    assert classify_card_routing(["infra", "deploy"]).valid
    assert classify_card_routing(["sklegal"]).violations == (
        "missing-logical-bucket:add-one-of-sk-s,sk-m,sk-l,sk-xl,sk-glm-s,sk-glm-m,sk-glm-l",
    )


def test_creation_accepts_exact_glm_selector(tmp_path) -> None:
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
            "SKLegal GLM-only work",
            "--tag",
            "sklegal",
            "--tag",
            "sk-glm-m",
            "--by",
            "human",
        ],
    )

    assert result.exit_code == 0, result.output
    assert CardStore(tmp_path).fold("a0a00004").labels == ["sklegal", "sk-glm-m"]


def test_label_amendment_fails_closed_and_add_is_idempotent(tmp_path) -> None:
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
            "--by",
            "human",
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


def test_label_amendment_enforces_bucket_rules(tmp_path) -> None:
    runner = CliRunner()
    created = runner.invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a0a00006",
            "--title",
            "SKLegal bucket rules",
            "--tag",
            "sklegal",
            "--tag",
            "sk-m",
            "--by",
            "human",
        ],
    )
    assert created.exit_code == 0, created.output

    # Adding a second bucket is ambiguous and fails closed.
    promoted = runner.invoke(
        main,
        ["coord", "label", "a0a00006", "sk-xl", "--home", str(tmp_path)],
    )
    assert promoted.exit_code != 0
    assert "multiple-logical-buckets:sk-m,sk-xl" in promoted.output

    # Removing the only bucket strictly increases violations and fails closed.
    stripped = runner.invoke(
        main,
        ["coord", "label", "a0a00006", "sk-m", "--remove", "--home", str(tmp_path)],
    )
    assert stripped.exit_code != 0
    assert "missing-logical-bucket" in stripped.output

    # A legacy card that carries two buckets (written before the guard, or
    # through raw events) can clean up one step at a time: removing the
    # neutral bucket leaves the exact GLM selector and a valid card.
    CardEventLog(tmp_path).append(
        CardEvent(card_id="a0a00006", action="add_label", label="sk-glm-l", writer="import")
    )
    assert CardStore(tmp_path).fold("a0a00006").labels == ["sklegal", "sk-m", "sk-glm-l"]
    cleaned = runner.invoke(
        main,
        [
            "coord",
            "label",
            "a0a00006",
            "sk-m",
            "--remove",
            "--home",
            str(tmp_path),
            "--agent",
            "human",
        ],
    )
    assert cleaned.exit_code == 0, cleaned.output
    assert CardStore(tmp_path).fold("a0a00006").labels == ["sklegal", "sk-glm-l"]


def test_legacy_cleanup_can_reduce_violations_one_step_at_a_time() -> None:
    before = ["sklegal", "codex-only"]
    after = ["sklegal"]

    assert routing_transition_allowed(before, after)
    assert not classify_card_routing(after).valid
    assert classify_card_routing([*after, "sk-m"]).valid


def test_concurrent_creation_keeps_one_canonical_card(tmp_path) -> None:
    args = [
        "coord",
        "create",
        "--home",
        str(tmp_path),
        "--id",
        "a0a00007",
        "--title",
        "SKLegal concurrent work",
        "--tag",
        "sklegal",
        "--by",
        "human",
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: CliRunner().invoke(main, args), range(2)))

    assert all(result.exit_code == 0 for result in results)
    assert len([card for card in CardStore(tmp_path).list_cards() if card.id == "a0a00007"]) == 1
    assert CardStore(tmp_path).fold("a0a00007").labels == ["sklegal", "sk-m"]


@pytest.mark.asyncio
async def test_mcp_creation_and_amendment_use_the_same_guard(monkeypatch, tmp_path) -> None:
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


def test_sdk_creation_uses_the_same_guard(monkeypatch, tmp_path) -> None:
    from skcapstone import sdk

    monkeypatch.setattr(sdk, "_shared_home", lambda: tmp_path)
    monkeypatch.setattr(sdk, "_agent_name", lambda: "pi-glm-chiap08-f1a9711b")
    monkeypatch.delenv("SKFLEET_SEAT", raising=False)

    created = sdk.coord_create("SKLegal SDK work", tags=["sklegal"])
    card_id = created if isinstance(created, str) else created.id
    assert CardStore(tmp_path).fold(card_id).labels == ["sklegal", "sk-m"]

    with pytest.raises(ValueError, match="provider-family-label:glm-only"):
        sdk.coord_create("SKLegal SDK rejected", tags=["sklegal", "glm-only"])


def test_dispatcher_rejects_malformed_routing_at_the_claim_gate() -> None:
    """The pool scan drops routing-invalid cards with an exact reason prefix.

    Loads `_claimability_reason` from the dispatcher exactly the way the
    claimability harness does, then proves the routing leg runs after the
    dependency gate and fails closed for a provider-family label.
    """
    import ast as ast_module
    from pathlib import Path

    rotate = Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast_module.parse(rotate.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast_module.FunctionDef) and item.name == "_claimability_reason"
    )
    namespace: dict[str, object] = {
        "re": __import__("re"),
        "json": json,
        "os": os,
        "HOST": "chiap08",
        "classify_card_routing": classify_card_routing,
        "_coord_task_claimable": lambda core: core.get("kind", "task") == "task",
        "_NOT_CLAIMABLE": {"not-claimable", "sprint-container", "do-not-claim"},
        "_SENSITIVE_CATEGORY": __import__("re").compile(r"never-matches"),
        "_CATEGORY_OPT_IN": "dispatch-approved",
        "non_implementation": lambda core, labels: False,
        "_dep_satisfied": lambda dep: True,
        "host_pin": lambda core, labels: None,
    }
    exec(
        compile(ast_module.Module(body=[node], type_ignores=[]), str(rotate), "exec"),
        namespace,
    )
    reason = namespace["_claimability_reason"]

    base_state = {
        "title": "t",
        "description": "",
        "acceptance_criteria": [],
        "links": {},
        "labels": [],
        "dependencies": [],
        "owner": "",
        "status": "ready",
        "voided": False,
        "archived": False,
        "awaiting_gates": False,
        "review_seen": False,
        "review_markers": {},
    }
    core = {"kind": "task", "spec_version": 1}

    state = {**base_state, "labels": ["sklegal", "sk-m"]}
    assert reason(core, state) == "claimable"

    state = {**base_state, "labels": ["sklegal", "sk-m", "glm-only"]}
    assert reason(core, state) == "routing:provider-family-label:glm-only"

    state = {**base_state, "labels": ["sklegal"]}
    assert (
        reason(core, state) == "routing:missing-logical-bucket:add-one-of-sk-s,sk-m,sk-l,sk-xl,"
        "sk-glm-s,sk-glm-m,sk-glm-l"
    )

    state = {**base_state, "labels": ["sklegal", "sk-s", "sk-xl"]}
    assert reason(core, state) == "routing:multiple-logical-buckets:sk-s,sk-xl"
