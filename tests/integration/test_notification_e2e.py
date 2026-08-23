"""
tests/integration/test_notification_e2e.py

End-to-end notification test: message -> consciousness response -> desktop popup.

Pipeline under test
-------------------
    1. A message envelope arrives at ``ConsciousnessLoop.process_envelope()``.
    2. The message is classified and routed to ``LLMBridge.generate()`` (mocked).
    3. The generated reply is sent back through SKComms (mocked).
    4. The interaction is recorded as a memory (``memory_engine.store``).
    5. The response handler ``_notify_response()`` fires a desktop popup through
       the REAL ``skcapstone.notifications`` path - gated by
       ``SKCAPSTONE_DESKTOP_NOTIFY`` - which ends in a ``notify-send`` subprocess
       call (the only OS boundary mocked).

Why integration (not a pure unit test)
--------------------------------------
    This drives the live consciousness response path (classify -> generate ->
    send -> memory -> notify) end-to-end rather than isolating a single method.
    Only two boundaries are mocked: the OS ``notify-send`` subprocess and the
    LLM backend.  Everything between (``process_envelope`` -> ``_notify_response``
    -> ``notifications.notify`` -> ``NotificationManager.notify`` ->
    ``_notify_linux``) runs for real.  It is deterministic and makes no network
    calls, but exercises enough moving parts that the ``integration`` marker is
    the honest home for it.

Related coordination task
-------------------------
    [040fd134] - Add end-to-end notification test: message -> popup.
                 Triage: only unit tests existed (tests/test_notifications.py,
                 which mock subprocess in isolation); nothing drove the full
                 message -> consciousness response -> notify pipeline.

Running
-------
    # This test:
    pytest tests/integration/test_notification_e2e.py -v -m integration

    # Fast CI unit run (excludes this file):
    pytest -m "not integration" tests/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(
    tmp_path: Path,
    *,
    auto_memory: bool = True,
    mock_generate: str = "Consciousness reply - the pipeline is alive.",
) -> tuple[Any, MagicMock]:
    """Construct a ConsciousnessLoop wired for notification testing.

    Returns:
        (loop, mock_skcomms) - the loop has a mock LLMBridge and mock SKComms
        injected, so the only real external boundary left is the notify-send
        subprocess (mocked separately by the test).
    """
    from skcapstone.consciousness_loop import (
        ConsciousnessConfig,
        ConsciousnessLoop,
        LLMBridge,
    )

    home = tmp_path / "home"
    shared_root = tmp_path / "shared"
    home.mkdir(parents=True, exist_ok=True)
    shared_root.mkdir(parents=True, exist_ok=True)

    config = ConsciousnessConfig(
        auto_memory=auto_memory,
        auto_ack=False,
        use_inotify=False,
        # Leave the INCOMING-message desktop popup off so the only notify-send
        # call the test sees is the one from _notify_response ("Agent response").
        desktop_notifications=False,
    )

    # Avoid any network probe during construction.
    with patch.object(LLMBridge, "_probe_ollama", return_value=False):
        loop = ConsciousnessLoop(config, home=home, shared_root=shared_root)

    # Mock the LLM backend - deterministic reply, no network.
    mock_bridge = MagicMock()
    mock_bridge.generate.return_value = mock_generate
    mock_bridge.available_backends = {"passthrough": True}
    loop._bridge = mock_bridge

    # Keep prompt building fast and deterministic (a cold build hits disk for
    # identity / soul / snapshots and can take seconds).
    loop._prompt_builder.build = MagicMock(return_value="test system prompt")

    # Mock SKComms so responses are captured without real transport.
    mock_skcomms = MagicMock()
    loop.set_skcomms(mock_skcomms)

    return loop, mock_skcomms


def _make_envelope(content: str = "Hello agent, please respond.", sender: str = "notif-e2e-peer"):
    """Build a minimal text envelope accepted by process_envelope()."""
    from skcapstone.consciousness_loop import _SimpleEnvelope

    return _SimpleEnvelope(
        {
            "sender": sender,
            "payload": {"content": content, "content_type": "text"},
        }
    )


@pytest.fixture
def linux_notify_manager(monkeypatch):
    """Install a deterministic Linux NotificationManager as the module singleton.

    The manager caches ``platform.system()`` at construction, and the module
    keeps a lazy singleton.  We inject a fresh Linux manager with zero debounce
    so ``notifications.notify()`` deterministically takes the ``notify-send``
    subprocess path regardless of the host OS or a previously cached singleton.

    We also force the gi.repository.Notify path to report "unavailable" so the
    manager always falls through to the notify-send subprocess we mock, and we
    neutralise the notification bookkeeping side-effect (which would otherwise
    try to write under the real AGENT_HOME).

    Yields:
        The MagicMock standing in for ``notifications.subprocess.run``.
    """
    from skcapstone import notifications

    mgr = notifications.NotificationManager(debounce_seconds=0.0)
    mgr._system = "Linux"
    # gi.Notify unavailable -> fall through to notify-send subprocess path.
    monkeypatch.setattr(mgr, "_notify_linux_gi", lambda *a, **k: False)
    monkeypatch.setattr(notifications, "_manager", mgr)

    # Record the notification bookkeeping call without touching the real home.
    notif_log = MagicMock()
    monkeypatch.setattr(notifications, "_store_notification_memory", notif_log)

    # Mock the OS notify-send subprocess - the single OS boundary under test.
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(notifications.subprocess, "run", mock_run)

    # Enable the opt-in desktop-notification gate (conftest forces it off).
    monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "1")

    # expose the log mock alongside the run mock for assertions
    mock_run.notif_log = notif_log
    return mock_run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMessageToPopupE2E:
    """Drive message -> consciousness response -> desktop popup end-to-end."""

    def test_message_produces_response_notification(self, tmp_path, linux_notify_manager):
        """A processed message fires an 'Agent response' desktop popup.

        Exercises the REAL wiring: process_envelope -> _notify_response ->
        notifications.notify -> NotificationManager.notify -> _notify_linux,
        ending in the mocked notify-send subprocess.
        """
        mock_run = linux_notify_manager
        reply = "Consciousness reply - the pipeline is alive."
        loop, mock_skcomms = _make_loop(tmp_path, mock_generate=reply)

        result = loop.process_envelope(_make_envelope("hello there"))

        # The pipeline generated and returned the reply.
        assert result == reply

        # The desktop popup fired via notify-send with the response title/body.
        notify_send_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "notify-send"
        ]
        assert (
            notify_send_calls
        ), f"notify-send was not invoked. subprocess.run calls: {mock_run.call_args_list}"
        argv = notify_send_calls[0].args[0]
        assert "Agent response" in argv, f"Popup title missing 'Agent response': {argv}"
        # Body is the first 120 chars of the reply.
        assert reply[:120] in argv, f"Popup body missing the reply text: {argv}"

        # The notification dispatch was recorded (bookkeeping log fired).
        assert mock_run.notif_log.called, "Notification dispatch was not recorded"

    def test_interaction_recorded_as_memory(self, tmp_path, linux_notify_manager):
        """The message/response interaction is persisted as a memory record."""
        reply = "Recorded reply."
        loop, _ = _make_loop(tmp_path, auto_memory=True, mock_generate=reply)

        with patch("skcapstone.memory_engine.store") as mock_store:
            loop.process_envelope(_make_envelope("remember this", sender="mem-peer"))

        assert mock_store.called, "Interaction was not recorded via memory_engine.store"
        kwargs = mock_store.call_args.kwargs
        assert "conversation" in kwargs.get(
            "tags", []
        ), f"Interaction memory missing 'conversation' tag: {kwargs.get('tags')}"
        assert "peer:mem-peer" in kwargs.get(
            "tags", []
        ), f"Interaction memory missing sender tag: {kwargs.get('tags')}"
        # Both the incoming message and the reply are captured in the summary.
        assert "remember this" in kwargs.get("content", "")
        assert reply in kwargs.get("content", "")

    def test_gate_disabled_suppresses_popup(self, tmp_path, monkeypatch):
        """With the opt-in gate OFF, no notify-send subprocess is dispatched.

        This is the guard half of the fail-before/pass-after pair: the same
        pipeline that pops a notification when SKCAPSTONE_DESKTOP_NOTIFY=1 stays
        silent when it is 0 (the conftest default background agents run under).
        """
        from skcapstone import notifications

        mgr = notifications.NotificationManager(debounce_seconds=0.0)
        mgr._system = "Linux"
        monkeypatch.setattr(mgr, "_notify_linux_gi", lambda *a, **k: False)
        monkeypatch.setattr(notifications, "_manager", mgr)
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(notifications.subprocess, "run", mock_run)

        # Gate explicitly OFF (conftest already forces "0", but be explicit).
        monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "0")

        loop, _ = _make_loop(tmp_path, mock_generate="silent reply")
        result = loop.process_envelope(_make_envelope("no popup please"))

        assert result == "silent reply", "Pipeline still produces a reply when gate is off"
        notify_send_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "notify-send"
        ]
        assert (
            not notify_send_calls
        ), f"notify-send fired despite the gate being disabled: {notify_send_calls}"
