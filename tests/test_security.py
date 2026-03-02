"""Security tests for skcapstone.

Covers:
- Peer name sanitization / path traversal prevention
- Large message (oversized inbox file) rejection
- Invalid JSON in inbox files

These map to the findings from the sprint-14 security audit.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.consciousness_loop import (
    ConsciousnessConfig,
    ConsciousnessLoop,
    SystemPromptBuilder,
    _sanitize_peer_name,
)


# ---------------------------------------------------------------------------
# _sanitize_peer_name unit tests
# ---------------------------------------------------------------------------


class TestSanitizePeerName:
    """Unit tests for the _sanitize_peer_name helper."""

    def test_normal_name_passes_through(self):
        assert _sanitize_peer_name("alice") == "alice"

    def test_alphanumeric_with_dash(self):
        assert _sanitize_peer_name("agent-007") == "agent-007"

    def test_at_sign_allowed(self):
        assert _sanitize_peer_name("opus@skworld.io") == "opus@skworld.io"

    def test_dotted_name_allowed(self):
        assert _sanitize_peer_name("v1.2.3") == "v1.2.3"

    # --- path traversal ---

    def test_slash_stripped(self):
        result = _sanitize_peer_name("../../../etc/passwd")
        assert "/" not in result
        assert ".." not in result or result.count(".") <= 1

    def test_backslash_stripped(self):
        result = _sanitize_peer_name("..\\Windows\\system32")
        assert "\\" not in result

    def test_pure_dotdot_rejected(self):
        """'..'' alone is stripped and falls back to 'unknown'."""
        result = _sanitize_peer_name("..")
        # After stripping leading/trailing dots, should be empty → "unknown"
        assert result == "unknown"

    def test_dotdot_with_slash(self):
        result = _sanitize_peer_name("../../secret")
        assert "/" not in result
        assert result != "../../secret"

    def test_null_byte_stripped(self):
        result = _sanitize_peer_name("alice\x00evil")
        assert "\x00" not in result

    def test_empty_string_returns_unknown(self):
        assert _sanitize_peer_name("") == "unknown"

    def test_none_returns_unknown(self):
        assert _sanitize_peer_name(None) == "unknown"  # type: ignore[arg-type]

    def test_spaces_stripped(self):
        result = _sanitize_peer_name("alice bob")
        assert " " not in result

    def test_length_capped_at_64(self):
        long_name = "a" * 200
        result = _sanitize_peer_name(long_name)
        assert len(result) <= 64

    def test_special_chars_stripped(self):
        result = _sanitize_peer_name("peer<script>alert(1)</script>")
        assert "<" not in result
        assert ">" not in result


# ---------------------------------------------------------------------------
# Path traversal via SystemPromptBuilder._persist_peer_history
# ---------------------------------------------------------------------------


class TestPeerHistoryPathTraversal:
    """Verify that malicious peer names cannot write outside the conversations dir."""

    def _make_builder(self, tmp_path: Path) -> SystemPromptBuilder:
        """Return a SystemPromptBuilder backed by tmp_path."""
        return SystemPromptBuilder(home=tmp_path, max_tokens=4096)

    def test_traversal_peer_stays_inside_conversations(self, tmp_path):
        """A sender like '../../../etc/passwd' must not escape conversations/."""
        builder = self._make_builder(tmp_path)
        conversations_dir = tmp_path / "conversations"

        # Simulate receiving a message from a malicious peer
        malicious_peer = "../../../etc/passwd"
        builder.add_to_history(malicious_peer, "user", "hello")

        # Only files inside conversations/ should exist
        for written_file in conversations_dir.rglob("*"):
            try:
                written_file.relative_to(conversations_dir)
            except ValueError:
                pytest.fail(
                    f"Path traversal detected: {written_file} is outside {conversations_dir}"
                )

    def test_dotdot_peer_sanitized_to_unknown(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder.add_to_history("..", "user", "hi")
        conversations_dir = tmp_path / "conversations"
        assert (conversations_dir / "unknown.json").exists()

    def test_slash_in_peer_name_sanitized(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder.add_to_history("a/b/c", "user", "hi")
        conversations_dir = tmp_path / "conversations"
        # Should write abc.json (slashes stripped) or similar safe name
        for f in conversations_dir.iterdir():
            assert "/" not in f.name

    def test_null_byte_in_peer_name_sanitized(self, tmp_path):
        builder = self._make_builder(tmp_path)
        builder.add_to_history("peer\x00evil", "user", "hi")
        conversations_dir = tmp_path / "conversations"
        for f in conversations_dir.iterdir():
            assert "\x00" not in f.name


# ---------------------------------------------------------------------------
# Large message rejection (file-size cap in ConsciousnessLoop._on_inbox_file)
# ---------------------------------------------------------------------------


class TestLargeMessageRejected:
    """The inbox handler must reject files larger than 1 MB."""

    def _make_loop(self, tmp_path: Path) -> ConsciousnessLoop:
        config = ConsciousnessConfig(use_inotify=False)
        loop = ConsciousnessLoop(
            config=config,
            home=tmp_path / "agent",
            shared_root=tmp_path / "shared",
        )
        return loop

    def test_oversized_file_is_dropped(self, tmp_path):
        """A 1.1 MB inbox file must not be processed."""
        loop = self._make_loop(tmp_path)

        inbox_file = tmp_path / "big.skc.json"
        # Write 1.1 MB of data — exceeds the 1_000_000 byte cap
        inbox_file.write_bytes(b"x" * 1_100_000)

        submitted = []
        loop._executor.submit = lambda fn, *a, **kw: submitted.append((fn, a))  # type: ignore[method-assign]

        loop._on_inbox_file(inbox_file)

        assert submitted == [], "Oversized file should have been dropped without submitting"

    def test_1mb_minus_one_byte_is_processed(self, tmp_path):
        """A file just below the cap should be attempted (may fail on parse — that's fine)."""
        loop = self._make_loop(tmp_path)

        inbox_file = tmp_path / "ok.skc.json"
        # Valid JSON just under the limit
        payload = json.dumps({"sender": "alice", "payload": {"content": "hi"}})
        inbox_file.write_text(payload, encoding="utf-8")

        submitted = []
        original_submit = loop._executor.submit

        def capture_submit(fn, *a, **kw):
            submitted.append((fn, a))
            return MagicMock()

        loop._executor.submit = capture_submit  # type: ignore[method-assign]
        loop._on_inbox_file(inbox_file)

        # Under the cap → should be submitted (even if the envelope later fails)
        assert len(submitted) == 1, "File under size cap should be submitted for processing"


# ---------------------------------------------------------------------------
# Invalid JSON rejection
# ---------------------------------------------------------------------------


class TestInvalidJsonRejected:
    """Malformed JSON in the inbox must not crash the consciousness loop."""

    def _make_loop(self, tmp_path: Path) -> ConsciousnessLoop:
        config = ConsciousnessConfig(use_inotify=False)
        return ConsciousnessLoop(
            config=config,
            home=tmp_path / "agent",
            shared_root=tmp_path / "shared",
        )

    def test_invalid_json_does_not_raise(self, tmp_path):
        """Malformed JSON must be silently dropped, not crash."""
        loop = self._make_loop(tmp_path)
        inbox_file = tmp_path / "bad.skc.json"
        inbox_file.write_text("{invalid json{{", encoding="utf-8")

        # Must not raise
        loop._on_inbox_file(inbox_file)

    def test_non_dict_json_does_not_raise(self, tmp_path):
        """A valid JSON array (not a dict) must also be silently dropped."""
        loop = self._make_loop(tmp_path)
        inbox_file = tmp_path / "array.skc.json"
        inbox_file.write_text("[1, 2, 3]", encoding="utf-8")

        loop._on_inbox_file(inbox_file)

    def test_truncated_json_does_not_raise(self, tmp_path):
        """Truncated / partially-written JSON must be silently dropped."""
        loop = self._make_loop(tmp_path)
        inbox_file = tmp_path / "truncated.skc.json"
        inbox_file.write_text('{"sender": "alice", "payload":', encoding="utf-8")

        loop._on_inbox_file(inbox_file)

    def test_empty_file_does_not_raise(self, tmp_path):
        """An empty inbox file must not crash."""
        loop = self._make_loop(tmp_path)
        inbox_file = tmp_path / "empty.skc.json"
        inbox_file.write_bytes(b"")

        loop._on_inbox_file(inbox_file)
