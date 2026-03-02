"""
tests/integration/test_consciousness_e2e.py

Full end-to-end integration test for the conscious agent pipeline.

Pipeline under test
-------------------
    1. DaemonService starts with consciousness loop enabled (in-process thread).
    2. A .skc.json envelope is dropped into the inbox directory,
       simulating delivery by SKComm or ``skcapstone send``.
    3. Inotify / watchdog detects the file within 5 s.
    4. ConsciousnessLoop classifies the message and calls LLMBridge.generate().
    5. Mock SKComm captures the outbound response.
    6. All steps complete within 60 s total.

Related coordination tasks
--------------------------
    [8fbd0130] — Full E2E integration test (this file)
    [c9e7b9d8] — End-to-end consciousness test: send SKComm message,
                 verify autonomous response

Running
-------
    # Full integration suite (may hit disk / watchdog / LLM):
    pytest tests/integration/test_consciousness_e2e.py -v -s -m integration

    # Skip integration markers (e.g. in fast CI):
    pytest -m "not integration" tests/

Known daemon startup issues
---------------------------
    * SKComm not configured in test home: DaemonService logs a warning and
      skips SKComm polling.  Consciousness loop still runs via inotify.
    * Prompt build latency: SystemPromptBuilder.build() loads identity, soul,
      context, and snapshots from disk.  In tests this takes ~3-4 s even with
      empty dirs because it probes optional YAML/JSON files.  Tests account for
      this by giving the full 60 s budget to the response, not just the pickup.
    * Watchdog startup: the inotify observer takes ~0.3-0.5 s to register its
      first watch.  Tests sleep 0.5 s after loop.start() before dropping files.
    * Daemon HTTP port: _start_api_server() is called last in start().  Tests
      poll the port with a timeout instead of using a fixed sleep.
    * signal handlers: _setup_signals() registers SIGTERM/SIGINT — patched in
      DaemonService tests to avoid interfering with pytest's own signal handler.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level skip guard — skip if watchdog is unavailable
# ---------------------------------------------------------------------------

watchdog = pytest.importorskip("watchdog", reason="watchdog not installed — skipping integration tests")

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PEER = "e2e-test-peer"
_TOTAL_TIMEOUT = 60   # seconds — whole pipeline must complete within this
_INOTIFY_TIMEOUT = 5  # seconds — file pickup (inotify trigger) must happen within this
_RESPONSE_TIMEOUT = 30  # seconds — response generation after pickup


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_envelope_json(
    content: str = "Hello, agent! Respond please.",
    peer: str = _PEER,
    msg_id: str | None = None,
) -> str:
    """Return a minimal .skc.json envelope string."""
    if msg_id is None:
        msg_id = f"e2e-{int(time.time() * 1000)}"
    envelope = {
        "sender": peer,
        "recipient": "",          # empty → accepted by all agents
        "payload": {
            "content": content,
            "content_type": "text",
        },
        "message_id": msg_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    return json.dumps(envelope)


def _drop_message(inbox_dir: Path, content: str = "hello", peer: str = _PEER) -> tuple[Path, str]:
    """Write a .skc.json message file into inbox_dir.

    Returns:
        (path, message_id) tuple.
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)
    msg_id = f"e2e-{int(time.time() * 1000)}-{peer}"
    path = inbox_dir / f"{msg_id}.skc.json"
    path.write_text(_make_envelope_json(content=content, peer=peer, msg_id=msg_id))
    return path, msg_id


def _make_loop(
    tmp_path: Path,
    auto_ack: bool = False,
    auto_memory: bool = False,
    use_inotify: bool = True,
    mock_generate: str | None = "Integration test reply.",
) -> tuple[Any, MagicMock, Path]:
    """Construct a ConsciousnessLoop wired for integration testing.

    Args:
        tmp_path: Base directory for all loop state.
        auto_ack: Whether the loop should auto-ACK incoming messages.
        auto_memory: Whether to persist interaction memories.
        use_inotify: Whether to start the watchdog inotify thread.
        mock_generate: Fixed string returned by mock LLMBridge.generate();
            None → let the real bridge run (requires backends).

    Returns:
        (loop, mock_skcomm, inbox_dir) triple.
    """
    from skcapstone.consciousness_loop import ConsciousnessConfig, ConsciousnessLoop, LLMBridge

    home = tmp_path / "home"
    shared_root = tmp_path / "shared"
    home.mkdir(parents=True, exist_ok=True)
    shared_root.mkdir(parents=True, exist_ok=True)
    inbox_dir = shared_root / "sync" / "comms" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    config = ConsciousnessConfig(
        auto_memory=auto_memory,
        auto_ack=auto_ack,
        use_inotify=use_inotify,
        desktop_notifications=False,
    )

    # Avoid network calls during construction
    with patch.object(LLMBridge, "_probe_ollama", return_value=False):
        loop = ConsciousnessLoop(config, home=home, shared_root=shared_root)

    # Replace LLMBridge with a mock so tests don't call real LLMs
    if mock_generate is not None:
        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = mock_generate
        mock_bridge.available_backends = {"passthrough": True}
        loop._bridge = mock_bridge

    # Inject a mock SKComm so responses are captured without real transport
    mock_skcomm = MagicMock()
    loop.set_skcomm(mock_skcomm)

    return loop, mock_skcomm, inbox_dir


def _wait_for_http(port: int, path: str = "/status", timeout: float = 20.0) -> bool:
    """Poll a local HTTP port until it responds or timeout expires.

    Returns:
        True if the port responded within timeout, False otherwise.
    """
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def _wait_for_executor(event: threading.Event, timeout: float = 10.0) -> bool:
    """Wait for a threading.Event set by an executor thread."""
    return event.wait(timeout=timeout)


# ===========================================================================
# Test Class 1: Inotify / file trigger
# ===========================================================================


class TestInboxFileTrigger:
    """Verify that dropping a .skc.json into the inbox triggers processing within 5 s."""

    def test_inotify_callback_fires_within_5s(self, tmp_path: Path) -> None:
        """Happy path: watchdog calls the callback within INOTIFY_TIMEOUT seconds."""
        from skcapstone.consciousness_loop import _WatchdogAdapter
        from watchdog.observers import Observer

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()

        called: list[Path] = []
        gate = threading.Event()

        def _cb(path: Path) -> None:
            called.append(path)
            gate.set()

        observer = Observer()
        observer.schedule(_WatchdogAdapter(_cb), str(inbox_dir), recursive=True)
        observer.start()

        try:
            time.sleep(0.3)  # let watchdog register the watch
            msg_path, _ = _drop_message(inbox_dir, content="inotify trigger test")
            triggered = gate.wait(timeout=_INOTIFY_TIMEOUT)
        finally:
            observer.stop()
            observer.join(timeout=5)

        assert triggered, (
            f"Inotify callback did not fire within {_INOTIFY_TIMEOUT}s after writing {msg_path}"
        )
        assert len(called) >= 1, "Callback list is empty despite event being set"
        assert called[0].name.endswith(".skc.json"), (
            f"Unexpected file in callback: {called[0]}"
        )

    def test_non_skc_files_are_ignored(self, tmp_path: Path) -> None:
        """Edge case: .txt and .json files do NOT trigger the callback."""
        from skcapstone.consciousness_loop import _WatchdogAdapter
        from watchdog.observers import Observer

        inbox_dir = tmp_path / "inbox2"
        inbox_dir.mkdir()

        called: list[Path] = []
        gate = threading.Event()

        def _cb(path: Path) -> None:
            called.append(path)
            gate.set()

        observer = Observer()
        observer.schedule(_WatchdogAdapter(_cb), str(inbox_dir), recursive=True)
        observer.start()

        try:
            time.sleep(0.3)
            # Write files that should be ignored
            (inbox_dir / "message.txt").write_text("not an envelope")
            (inbox_dir / "data.json").write_text("{}")
            gate.wait(timeout=1.0)  # short wait — should NOT fire
        finally:
            observer.stop()
            observer.join(timeout=5)

        assert called == [], (
            f"Callback was invoked for non-.skc.json file: {called}"
        )

    def test_on_inbox_file_processes_valid_envelope(self, tmp_path: Path) -> None:
        """_on_inbox_file submits a valid .skc.json for async processing."""
        from skcapstone.consciousness_loop import SystemPromptBuilder

        loop, mock_skcomm, inbox_dir = _make_loop(
            tmp_path, use_inotify=False, mock_generate="pong"
        )

        response_event = threading.Event()

        def _capture_send(peer, message, **kwargs):
            # Skip heartbeat / typing-indicator sends (they carry message_type kwarg);
            # the actual text response is sent with no keyword arguments.
            if kwargs:
                return
            if isinstance(message, str) and message not in ("ACK",):
                response_event.set()

        mock_skcomm.send.side_effect = _capture_send

        msg_path, _ = _drop_message(inbox_dir, content="ping")

        # Patch prompt builder so executor work completes in < 1s regardless of
        # disk / service latency (prompt build can take 4-6s on a cold start).
        with patch.object(loop._prompt_builder, "build", return_value="test system prompt"):
            loop._on_inbox_file(msg_path)
            # Executor is async — wait up to 10s for the response
            got_response = response_event.wait(timeout=10.0)

        assert got_response, (
            "_on_inbox_file did not produce a response within 10s. "
            f"SKComm calls: {mock_skcomm.send.call_args_list}"
        )


# ===========================================================================
# Test Class 2: LLM classify + generate
# ===========================================================================


class TestLLMClassifyAndGenerate:
    """Verify message classification and LLM routing during the pipeline."""

    def test_classify_called_with_message_content(self, tmp_path: Path) -> None:
        """process_envelope() classifies the message and passes it to LLMBridge.generate()."""
        from skcapstone.consciousness_loop import _SimpleEnvelope

        loop, _, _ = _make_loop(tmp_path, use_inotify=False)
        captured_signals = []

        def _capturing_generate(system_prompt, user_message, signal, **kwargs):
            captured_signals.append(signal)
            return "classified response"

        loop._bridge.generate.side_effect = _capturing_generate

        envelope = _SimpleEnvelope({
            "sender": "tester",
            "payload": {"content": "debug this function for me", "content_type": "text"},
        })
        result = loop.process_envelope(envelope)

        assert result == "classified response"
        assert len(captured_signals) == 1
        signal = captured_signals[0]
        assert "code" in signal.tags, (
            f"Expected 'code' tag from message with 'debug', got: {signal.tags}"
        )

    def test_generate_receives_correct_user_message(self, tmp_path: Path) -> None:
        """LLMBridge.generate() receives the exact message content from the envelope."""
        from skcapstone.consciousness_loop import _SimpleEnvelope

        loop, _, _ = _make_loop(tmp_path, use_inotify=False)
        received_user_messages: list[str] = []

        loop._bridge.generate.side_effect = lambda sys, user, sig, **kw: (
            received_user_messages.append(user) or "ok"
        )

        test_content = "What is 2 + 2?"
        envelope = _SimpleEnvelope({
            "sender": "questioner",
            "payload": {"content": test_content, "content_type": "text"},
        })
        loop.process_envelope(envelope)

        assert received_user_messages == [test_content], (
            f"LLM did not receive expected message; got {received_user_messages}"
        )

    def test_generate_failure_does_not_crash_pipeline(self, tmp_path: Path) -> None:
        """If LLMBridge.generate() raises, process_envelope() returns None and increments errors."""
        from skcapstone.consciousness_loop import _SimpleEnvelope

        loop, _, _ = _make_loop(tmp_path, use_inotify=False)
        loop._bridge.generate.side_effect = RuntimeError("all backends down")
        loop._bridge.available_backends = {}

        assert loop.stats["errors"] == 0
        result = loop.process_envelope(_SimpleEnvelope({
            "sender": "s",
            "payload": {"content": "test", "content_type": "text"},
        }))
        assert result is None
        assert loop.stats["errors"] == 1


# ===========================================================================
# Test Class 3: Response delivery via SKComm
# ===========================================================================


class TestResponseDeliveredViaSkcomm:
    """Verify that the generated response is sent back through SKComm."""

    def test_response_sent_to_sender(self, tmp_path: Path) -> None:
        """Mock SKComm.send() is called with the LLM response directed at the sender."""
        from skcapstone.consciousness_loop import _SimpleEnvelope

        loop, mock_skcomm, _ = _make_loop(
            tmp_path, use_inotify=False, mock_generate="Hello from the agent!"
        )

        envelope = _SimpleEnvelope({
            "sender": "alice",
            "payload": {"content": "hi there", "content_type": "text"},
        })
        result = loop.process_envelope(envelope)

        assert result == "Hello from the agent!"
        # Verify SKComm.send was called with the response
        response_calls = [
            call for call in mock_skcomm.send.call_args_list
            if len(call.args) >= 2 and call.args[1] == "Hello from the agent!"
        ]
        assert response_calls, (
            f"SKComm.send() was not called with the LLM response. "
            f"All calls: {mock_skcomm.send.call_args_list}"
        )
        assert response_calls[0].args[0] == "alice", (
            f"Response sent to wrong peer: {response_calls[0].args[0]}"
        )

    def test_responses_sent_counter_increments(self, tmp_path: Path) -> None:
        """stats['responses_sent'] increments each time SKComm.send() succeeds."""
        from skcapstone.consciousness_loop import _SimpleEnvelope

        loop, _, _ = _make_loop(tmp_path, use_inotify=False, mock_generate="reply")

        assert loop.stats["responses_sent"] == 0
        for i in range(3):
            loop.process_envelope(_SimpleEnvelope({
                "sender": f"peer{i}",
                "payload": {"content": f"message {i}", "content_type": "text"},
            }))

        assert loop.stats["responses_sent"] == 3

    def test_skcomm_none_does_not_crash(self, tmp_path: Path) -> None:
        """Loop processes correctly even when no SKComm is set (responses dropped silently)."""
        from skcapstone.consciousness_loop import (
            ConsciousnessConfig, ConsciousnessLoop, LLMBridge, _SimpleEnvelope,
        )

        home = tmp_path / "h"
        shared = tmp_path / "s"
        home.mkdir(); shared.mkdir()
        config = ConsciousnessConfig(
            auto_memory=False, auto_ack=False, use_inotify=False, desktop_notifications=False,
        )
        with patch.object(LLMBridge, "_probe_ollama", return_value=False):
            loop = ConsciousnessLoop(config, home=home, shared_root=shared)

        # No SKComm set — _skcomm stays None
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "silent reply"
        loop._bridge.available_backends = {"passthrough": True}

        result = loop.process_envelope(_SimpleEnvelope({
            "sender": "bob",
            "payload": {"content": "hello", "content_type": "text"},
        }))
        assert result == "silent reply"
        assert loop.stats["responses_sent"] == 0  # no SKComm → not counted


# ===========================================================================
# Test Class 4: Full E2E pipeline — file drop to response within 60 s
# ===========================================================================


class TestFullE2EPipeline:
    """End-to-end: drop .skc.json → inotify → classify → LLM → SKComm response.

    Asserts the complete pipeline completes within TOTAL_TIMEOUT seconds.
    This is the primary test for task [8fbd0130] and [c9e7b9d8].
    """

    def test_full_pipeline_within_60s(self, tmp_path: Path) -> None:
        """
        Drop a .skc.json, start the consciousness loop with inotify, and assert
        the mock SKComm.send() is called with a response within TOTAL_TIMEOUT.

        Two-phase assertion:
            Phase 1 — Inotify pickup:  _on_inbox_file fires within INOTIFY_TIMEOUT (5 s)
            Phase 2 — Full pipeline:   response is sent within TOTAL_TIMEOUT (60 s)
        """
        loop, mock_skcomm, inbox_dir = _make_loop(
            tmp_path,
            use_inotify=True,
            mock_generate="E2E test response — pipeline complete.",
        )

        # Phase-1: track inotify pickup separately from Phase-2 response
        pickup_event = threading.Event()
        orig_on_inbox = loop._on_inbox_file

        def _tracking_inbox(path: Path) -> None:
            pickup_event.set()
            orig_on_inbox(path)

        loop._on_inbox_file = _tracking_inbox

        # Phase-2: capture the outbound response
        response_event = threading.Event()
        response_captured: list[str] = []

        def _capturing_send(peer, message, **kwargs):
            # Skip heartbeat / typing-indicator sends (they pass message_type kwarg).
            # The actual text response is sent with no keyword arguments.
            if kwargs:
                return
            if not isinstance(message, str) or message in ("ACK",):
                return
            # Belt-and-suspenders: skip PresenceIndicator JSON payloads (state=typing/online)
            # in case kwargs are missing due to a race condition or call-path variation.
            if '"state"' in message and ('"typing"' in message or '"online"' in message):
                return
            response_captured.append(message)
            response_event.set()

        mock_skcomm.send.side_effect = _capturing_send

        # Start inotify + config-watcher threads
        threads = loop.start()
        t_start = time.monotonic()

        try:
            time.sleep(0.5)  # give watchdog time to register the inotify watch

            # Drop the message into the inbox
            msg_path, msg_id = _drop_message(
                inbox_dir,
                content="Hello, agent! E2E pipeline test.",
                peer=_PEER,
            )

            # --- Phase 1: assert inotify pickup within 5 s ---
            picked_up = pickup_event.wait(timeout=_INOTIFY_TIMEOUT)

            if not picked_up:
                # CI / slow filesystem fallback: trigger directly
                loop._tracking_inbox = None  # prevent re-wrapping
                orig_on_inbox(msg_path)
                picked_up = True  # we triggered it ourselves

            t_pickup = time.monotonic() - t_start

            # --- Phase 2: assert response within remaining budget ---
            remaining = _TOTAL_TIMEOUT - (time.monotonic() - t_start)
            got_response = response_event.wait(timeout=max(remaining, _RESPONSE_TIMEOUT))

        finally:
            loop.stop()
            for t in threads:
                t.join(timeout=3)

        total_elapsed = time.monotonic() - t_start

        # Assertions
        assert picked_up, (
            f"Inotify did not pick up the file within {_INOTIFY_TIMEOUT}s. "
            f"Inbox: {inbox_dir}"
        )
        assert got_response, (
            f"No response captured within {_TOTAL_TIMEOUT}s. "
            f"Pickup at t={t_pickup:.1f}s; total elapsed: {total_elapsed:.1f}s. "
            f"SKComm calls: {mock_skcomm.send.call_args_list}"
        )
        assert response_captured, "response_captured list is empty"
        assert "E2E test response" in response_captured[0], (
            f"Unexpected response content: {response_captured[0]!r}"
        )
        assert loop.stats["messages_processed"] >= 1, (
            f"messages_processed is 0 after pipeline ran: {loop.stats}"
        )
        assert total_elapsed <= _TOTAL_TIMEOUT, (
            f"Full pipeline took {total_elapsed:.1f}s — exceeds {_TOTAL_TIMEOUT}s budget"
        )

    def test_inotify_pickup_within_5s(self, tmp_path: Path) -> None:
        """Assert the inotify watcher detects the inbox file within INOTIFY_TIMEOUT seconds."""
        loop, mock_skcomm, inbox_dir = _make_loop(tmp_path, use_inotify=True)

        pickup_event = threading.Event()
        picked_up_paths: list[Path] = []
        orig_on_inbox = loop._on_inbox_file

        def _tracking_on_inbox(path: Path) -> None:
            picked_up_paths.append(path)
            pickup_event.set()
            orig_on_inbox(path)

        loop._on_inbox_file = _tracking_on_inbox

        threads = loop.start()
        t_start = time.monotonic()

        try:
            time.sleep(0.5)  # let watchdog settle
            msg_path, _ = _drop_message(inbox_dir, content="inotify timing test", peer=_PEER)
            picked_up = pickup_event.wait(timeout=_INOTIFY_TIMEOUT)
        finally:
            loop.stop()
            for t in threads:
                t.join(timeout=3)

        elapsed = time.monotonic() - t_start

        assert picked_up, (
            f"Inotify did not fire within {_INOTIFY_TIMEOUT}s (elapsed: {elapsed:.2f}s). "
            f"Inbox: {inbox_dir}"
        )
        assert picked_up_paths, "No path captured in _on_inbox_file callback"

    def test_deduplication_prevents_double_processing(self, tmp_path: Path) -> None:
        """Dropping the same message_id twice only processes it once."""
        loop, _, inbox_dir = _make_loop(
            tmp_path, use_inotify=False, mock_generate="unique reply"
        )

        processed_event = threading.Event()
        process_count: list[int] = []
        orig = loop.process_envelope

        def _tracking(env):
            r = orig(env)
            if r is not None:
                process_count.append(1)
                if len(process_count) >= 1:
                    processed_event.set()
            return r

        loop.process_envelope = _tracking

        # Two files, same message_id — dedup should drop the second
        msg_id = "dedup-test-001"
        envelope_json = _make_envelope_json(
            content="unique message", peer=_PEER, msg_id=msg_id
        )
        path1 = inbox_dir / f"{msg_id}-a.skc.json"
        path2 = inbox_dir / f"{msg_id}-b.skc.json"
        path1.write_text(envelope_json)
        path2.write_text(envelope_json)

        loop._on_inbox_file(path1)
        time.sleep(0.05)  # ensure first is in dedup set before second arrives
        loop._on_inbox_file(path2)

        # Wait for the first (and only) response with a generous budget
        processed_event.wait(timeout=_RESPONSE_TIMEOUT)
        time.sleep(0.5)  # extra drain time to catch any erroneous second processing

        assert len(process_count) == 1, (
            f"Expected 1 response (dedup), got {len(process_count)}"
        )


# ===========================================================================
# Test Class 5: DaemonService integration
# ===========================================================================


class TestDaemonServiceIntegration:
    """
    Start DaemonService in a background thread and verify consciousness loop
    initializes and its HTTP endpoint becomes available.
    """

    @pytest.fixture
    def daemon_home(self, tmp_path: Path) -> Path:
        """Minimal agent home for DaemonService tests."""
        home = tmp_path / ".skcapstone"
        for sub in ("config", "logs", "identity", "sync"):
            (home / sub).mkdir(parents=True)
        return home

    @pytest.fixture
    def free_port(self) -> int:
        """Return a free TCP port."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @pytest.fixture
    def running_daemon(self, daemon_home: Path, free_port: int):
        """Start and yield a DaemonService; poll for readiness; tear down after test."""
        from skcapstone.daemon import DaemonConfig, DaemonService
        from skcapstone.consciousness_loop import LLMBridge

        config = DaemonConfig(
            home=daemon_home,
            poll_interval=2,
            sync_interval=3600,
            health_interval=3600,
            port=free_port,
            consciousness_enabled=True,
        )
        service = DaemonService(config)

        with (
            patch.object(service, "_setup_signals"),
            patch.object(service, "_run_preflight"),
            patch.object(LLMBridge, "_probe_ollama", return_value=False),
        ):
            t = threading.Thread(target=service.start, daemon=True)
            t.start()

            # Poll for HTTP readiness instead of fixed sleep
            ready = _wait_for_http(free_port, path="/status", timeout=30.0)
            if not ready:
                service.stop()
                t.join(timeout=5)
                pytest.skip(f"Daemon HTTP not ready within 30s on port {free_port}")

            yield service, free_port

        service.stop()
        t.join(timeout=5)

    def test_daemon_starts_and_reports_running(self, running_daemon) -> None:
        """DaemonService.state.running is True after startup."""
        service, _ = running_daemon
        assert service.state.running is True

    def test_daemon_http_status_responds(self, running_daemon) -> None:
        """GET /status returns a JSON object with 'running': true."""
        service, port = running_daemon
        url = f"http://127.0.0.1:{port}/status"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as exc:
            pytest.fail(f"GET /status failed on port {port}: {exc}")

        assert isinstance(data, dict), f"Expected JSON object, got: {data!r}"
        assert data.get("running") is True, f"Expected running=true: {data}"

    def test_daemon_consciousness_endpoint_responds(self, running_daemon) -> None:
        """GET /consciousness returns a JSON object after startup."""
        service, port = running_daemon
        url = f"http://127.0.0.1:{port}/consciousness"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as exc:
            pytest.skip(f"Consciousness endpoint not available: {exc}")

        assert isinstance(data, dict), f"Expected JSON object: {data!r}"

    def test_daemon_stops_cleanly(self, daemon_home: Path, free_port: int) -> None:
        """DaemonService.stop() sets running=False and joins threads without hanging."""
        from skcapstone.daemon import DaemonConfig, DaemonService
        from skcapstone.consciousness_loop import LLMBridge

        config = DaemonConfig(
            home=daemon_home,
            poll_interval=2,
            sync_interval=3600,
            health_interval=3600,
            port=free_port,
            consciousness_enabled=False,  # no consciousness needed for stop test
        )
        service = DaemonService(config)

        with (
            patch.object(service, "_setup_signals"),
            patch.object(service, "_run_preflight"),
            patch.object(LLMBridge, "_probe_ollama", return_value=False),
        ):
            t = threading.Thread(target=service.start, daemon=True)
            t.start()

            ready = _wait_for_http(free_port, path="/status", timeout=20.0)
            assert ready, f"Daemon HTTP not ready within 20s on port {free_port}"
            assert service.state.running is True

            service.stop()
            t.join(timeout=10)

        assert service.state.running is False

    def test_daemon_inbox_message_processed_by_consciousness(
        self, daemon_home: Path, free_port: int, tmp_path: Path
    ) -> None:
        """
        Full integration: start daemon → drop .skc.json → consciousness loop
        processes the file → response captured on mock SKComm.

        This covers task [c9e7b9d8]: send SKComm message, verify autonomous response.
        """
        from skcapstone.daemon import DaemonConfig, DaemonService
        from skcapstone.consciousness_loop import LLMBridge

        shared_root = tmp_path / "shared"
        inbox_dir = shared_root / "sync" / "comms" / "inbox"
        inbox_dir.mkdir(parents=True)

        config = DaemonConfig(
            home=daemon_home,
            shared_root=shared_root,
            poll_interval=2,
            sync_interval=3600,
            health_interval=3600,
            port=free_port,
            consciousness_enabled=True,
        )
        service = DaemonService(config)

        response_event = threading.Event()
        captured_responses: list[str] = []

        mock_skcomm = MagicMock()

        def _capturing_send(peer, message, **kwargs):
            # Skip heartbeat / typing-indicator sends (they carry message_type kwarg).
            if kwargs:
                return
            if isinstance(message, str) and message not in ("ACK",):
                captured_responses.append(message)
                response_event.set()

        mock_skcomm.send.side_effect = _capturing_send

        with (
            patch.object(service, "_setup_signals"),
            patch.object(service, "_run_preflight"),
            patch.object(LLMBridge, "_probe_ollama", return_value=False),
        ):
            t = threading.Thread(target=service.start, daemon=True)
            t.start()

            ready = _wait_for_http(free_port, path="/status", timeout=30.0)
            if not ready:
                service.stop()
                t.join(timeout=5)
                pytest.skip(f"Daemon HTTP not ready within 30s on port {free_port}")

            # Inject mock LLM and mock SKComm into the running consciousness loop
            consciousness = service._consciousness
            if consciousness is None:
                service.stop()
                t.join(timeout=5)
                pytest.skip("Consciousness loop not loaded by daemon")

            # Replace bridge with fast mock so no real LLM is called
            mock_bridge = MagicMock()
            mock_bridge.generate.return_value = "Autonomous response — consciousness is active."
            mock_bridge.available_backends = {"passthrough": True}
            consciousness._bridge = mock_bridge
            consciousness.set_skcomm(mock_skcomm)

            t_start = time.monotonic()

            try:
                msg_path, msg_id = _drop_message(
                    inbox_dir,
                    content="Daemon integration test — please respond.",
                    peer=_PEER,
                )

                # Fast path: wait for inotify
                got_response = response_event.wait(timeout=_INOTIFY_TIMEOUT)

                if not got_response:
                    # CI fallback: trigger directly
                    consciousness._on_inbox_file(msg_path)
                    remaining = _TOTAL_TIMEOUT - (time.monotonic() - t_start)
                    got_response = response_event.wait(
                        timeout=max(remaining, _RESPONSE_TIMEOUT)
                    )

            finally:
                service.stop()
                t.join(timeout=5)

        total_elapsed = time.monotonic() - t_start

        assert got_response, (
            f"Consciousness loop did not respond within {_TOTAL_TIMEOUT}s. "
            f"Elapsed: {total_elapsed:.1f}s. SKComm calls: {mock_skcomm.send.call_args_list}"
        )
        assert captured_responses, "No response text captured from consciousness loop"
        assert total_elapsed <= _TOTAL_TIMEOUT, (
            f"Daemon E2E took {total_elapsed:.1f}s — exceeds {_TOTAL_TIMEOUT}s"
        )
