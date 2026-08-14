"""Tests for the suggest_item + queue_item MCP tools."""

from __future__ import annotations

import json

import pytest

from skcapstone import agent_run
from skcapstone.mcp_tools import suggest_tools


def _parse(result):
    return json.loads(result[0].text)


# ── _resolve_card_id ────────────────────────────────────────────────────


def test_resolve_card_id_coord_passthrough():
    assert suggest_tools._resolve_card_id("coord", "task-42") == "task-42"


def test_resolve_card_id_gtd_prefixes():
    assert suggest_tools._resolve_card_id("gtd", "abc123") == "gtd-abc123"


def test_resolve_card_id_gtd_idempotent_when_already_prefixed():
    assert suggest_tools._resolve_card_id("gtd", "gtd-abc123") == "gtd-abc123"


def test_resolve_card_id_itil_passthrough():
    assert suggest_tools._resolve_card_id("itil", "inc-99") == "inc-99"


def test_resolve_card_id_unknown_surface_is_none():
    assert suggest_tools._resolve_card_id("bogus", "abc123") is None


def test_resolve_card_id_blank_id_is_none():
    assert suggest_tools._resolve_card_id("coord", "") is None
    assert suggest_tools._resolve_card_id("coord", "   ") is None


# ── tool registration ───────────────────────────────────────────────────


def test_tools_and_handlers_registered():
    names = {t.name for t in suggest_tools.TOOLS}
    assert names == {"suggest_item", "queue_item"}
    assert "suggest_item" in suggest_tools.HANDLERS
    assert "queue_item" in suggest_tools.HANDLERS


# ── suggest_item ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suggest_item_coord_passes_through_and_returns_result(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    calls = []

    def fake_suggest_next_steps(home, card_id, use_llm=True, timeout=12.0):
        calls.append((home, card_id, use_llm))
        return {"suggestions": [{"text": "do the thing", "mode": "propose"}], "source": "llm"}

    monkeypatch.setattr(agent_run, "suggest_next_steps", fake_suggest_next_steps)

    result = await suggest_tools._handle_suggest_item(
        {"surface": "coord", "id": "task-1", "llm": False}
    )
    data = _parse(result)

    assert data["source"] == "llm"
    assert data["suggestions"][0]["text"] == "do the thing"
    assert len(calls) == 1
    home, card_id, use_llm = calls[0]
    assert home == tmp_path
    assert card_id == "task-1"
    assert use_llm is False


@pytest.mark.asyncio
async def test_suggest_item_gtd_resolves_prefixed_card_id(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    calls = []

    def fake_suggest_next_steps(home, card_id, use_llm=True, timeout=12.0):
        calls.append((home, card_id, use_llm))
        return {"suggestions": [], "source": "heuristic"}

    monkeypatch.setattr(agent_run, "suggest_next_steps", fake_suggest_next_steps)

    result = await suggest_tools._handle_suggest_item({"surface": "gtd", "id": "xyz789"})
    data = _parse(result)

    assert data["source"] == "heuristic"
    assert calls[0][1] == "gtd-xyz789"
    assert calls[0][2] is True  # default llm=True


@pytest.mark.asyncio
async def test_suggest_item_unknown_surface_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)

    def fake_suggest_next_steps(home, card_id, use_llm=True, timeout=12.0):
        raise AssertionError("should not be called for an unknown surface")

    monkeypatch.setattr(agent_run, "suggest_next_steps", fake_suggest_next_steps)

    result = await suggest_tools._handle_suggest_item({"surface": "bogus", "id": "abc"})
    data = _parse(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_suggest_item_blank_id_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    result = await suggest_tools._handle_suggest_item({"surface": "coord", "id": ""})
    data = _parse(result)
    assert "error" in data


# ── queue_item ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queue_item_passes_through_and_returns_result(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    calls = []

    def fake_request_run(
        home, card_id, instruction, agent="lumina", mode="propose", requester="operator"
    ):
        calls.append(
            {
                "home": home,
                "card_id": card_id,
                "instruction": instruction,
                "agent": agent,
                "mode": mode,
                "requester": requester,
            }
        )
        return {"ok": True, "run_id": "run-abc123", "card_id": card_id, "state": "queued"}

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {
            "surface": "itil",
            "id": "inc-7",
            "instruction": "investigate root cause",
            "mode": "dry-run",
            "agent": "opus",
        }
    )
    data = _parse(result)

    assert data == {"ok": True, "run_id": "run-abc123", "card_id": "inc-7", "state": "queued"}
    assert len(calls) == 1
    call = calls[0]
    assert call["home"] == tmp_path
    assert call["card_id"] == "inc-7"
    assert call["instruction"] == "investigate root cause"
    assert call["mode"] == "dry-run"
    assert call["agent"] == "opus"
    # requester is the resolved calling identity, never a hardcoded "operator":
    # it is written into the append-only agent_run_request consent event.
    assert call["requester"] != "operator"
    assert call["requester"]


@pytest.mark.asyncio
async def test_queue_item_defaults_mode_and_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    calls = []

    def fake_request_run(
        home, card_id, instruction, agent="lumina", mode="propose", requester="operator"
    ):
        calls.append({"agent": agent, "mode": mode})
        return {"ok": True, "run_id": "run-1", "card_id": card_id, "state": "queued"}

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {"surface": "gtd", "id": "abc", "instruction": "draft it"}
    )
    data = _parse(result)

    assert data["ok"] is True
    assert calls[0]["agent"] == "lumina"
    assert calls[0]["mode"] == "propose"


@pytest.mark.asyncio
async def test_queue_item_gtd_resolves_prefixed_card_id(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    calls = []

    def fake_request_run(
        home, card_id, instruction, agent="lumina", mode="propose", requester="operator"
    ):
        calls.append(card_id)
        return {"ok": True, "run_id": "run-1", "card_id": card_id, "state": "queued"}

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    await suggest_tools._handle_queue_item(
        {"surface": "gtd", "id": "gtd-already", "instruction": "do it"}
    )
    assert calls[0] == "gtd-already"


@pytest.mark.asyncio
async def test_queue_item_unknown_surface_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)

    def fake_request_run(*args, **kwargs):
        raise AssertionError("should not be called for an unknown surface")

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {"surface": "bogus", "id": "abc", "instruction": "do it"}
    )
    data = _parse(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_queue_item_blank_instruction_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)

    def fake_request_run(*args, **kwargs):
        raise AssertionError("should not be called with a blank instruction")

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {"surface": "coord", "id": "task-1", "instruction": "   "}
    )
    data = _parse(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_queue_item_refuses_execute_mode(tmp_path, monkeypatch):
    """Execute-tier queueing must never pass through the ungated MCP surface.

    Regression pin for the priv-esc sibling of the assistant-surface fix: this
    handler verifies no capability at all, so a model-supplied mode="execute"
    would have reached request_run at VERIFIED tier on a caller proven at
    nothing.
    """
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)

    def fake_request_run(*args, **kwargs):
        raise AssertionError("execute must never reach request_run from MCP")

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {
            "surface": "coord",
            "id": "task-1",
            "instruction": "ship it",
            "mode": "execute",
        }
    )
    data = _parse(result)
    assert "error" in data
    assert "execute" in data["error"]


@pytest.mark.asyncio
async def test_queue_item_schema_does_not_advertise_execute():
    """The tool schema must not offer a mode the handler refuses."""
    queue_tool = next(t for t in suggest_tools.TOOLS if t.name == "queue_item")
    modes = queue_tool.inputSchema["properties"]["mode"]["enum"]
    assert "execute" not in modes
    assert "propose" in modes


@pytest.mark.asyncio
async def test_queue_item_does_not_hardcode_operator_requester(tmp_path, monkeypatch):
    """Consent is attributed to the calling agent, never a blanket 'operator'.

    requester lands in the append-only agent_run_request event, so hardcoding
    it made every MCP-originated run indistinguishable from a human action.
    """
    monkeypatch.setattr(suggest_tools, "_shared_root", lambda: tmp_path)
    seen = {}

    def fake_request_run(*args, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "run_id": "r1", "card_id": "task-1"}

    monkeypatch.setattr(agent_run, "request_run", fake_request_run)

    result = await suggest_tools._handle_queue_item(
        {"surface": "coord", "id": "task-1", "instruction": "look into it"}
    )
    _parse(result)
    assert seen["requester"] != "operator"
