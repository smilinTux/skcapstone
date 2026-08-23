"""Regression tests for MCP comm tools DeliveryReport handling (card 2124241c).

The handlers used to read ``report.success``, but skcomms' DeliveryReport
aggregate has no such attribute (per-attempt SendResult does) — every send
through the MCP surface died with AttributeError once routing returned.
"""

from __future__ import annotations

import json

import pytest
from skcomms.transport import DeliveryReport, SendResult

from skcapstone.mcp_tools import comm_tools, skcomms_tools


class _StubComms:
    """Minimal stand-in for skcomms.core.SKComms returning a fixed report."""

    def __init__(self, report: DeliveryReport):
        self._report = report

    def send(self, recipient, message):
        return self._report


def _report(delivered: bool = True) -> DeliveryReport:
    return DeliveryReport(
        envelope_id="env-1",
        delivered=delivered,
        attempts=[SendResult(success=delivered, transport_name="file", envelope_id="env-1")],
    )


@pytest.fixture
def stub_from_config(monkeypatch):
    from skcomms.core import SKComms

    monkeypatch.setattr(SKComms, "from_config", classmethod(lambda cls: _StubComms(_report())))


def _payload(contents) -> dict:
    assert len(contents) == 1
    return json.loads(contents[0].text)


@pytest.mark.asyncio
async def test_send_message_uses_delivered_not_success(stub_from_config):
    out = _payload(await comm_tools._handle_send_message({"recipient": "lumina", "message": "hi"}))
    assert out["sent"] is True
    assert out["confirmed"] is True
    assert out["transport"] == "file"
    assert out["attempts"][0]["success"] is True


@pytest.mark.asyncio
async def test_send_message_requires_recipient_and_message():
    out = await comm_tools._handle_send_message({"recipient": "", "message": ""})
    assert "error" in json.loads(out[0].text)


@pytest.mark.asyncio
async def test_comm_notify_uses_delivered_not_success(stub_from_config):
    out = _payload(
        await skcomms_tools._handle_comm_notify({"recipient": "lumina", "message": "hi"})
    )
    assert out["sent"] is True
    assert out["confirmed"] is True
    assert out["urgency"] == "normal"
    assert out["attempts"][0]["transport"] == "file"
