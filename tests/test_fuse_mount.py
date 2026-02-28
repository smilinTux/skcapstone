"""Tests for the skcapstone FUSE mount module.

Covers helper functions, the SovereignFS virtual filesystem class, and the
FUSEDaemon lifecycle manager.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.fuse_mount import (
    FUSEDaemon,
    SovereignFS,
    _build_fingerprint_txt,
    _build_identity_card,
    _dir_stat,
    _file_stat,
    _list_coordination_tasks,
    _list_documents,
    _list_inbox,
    _list_memory_ids,
    _load_memory_file,
    _memory_to_markdown,
    _parse_path,
    _read_coordination_task,
    _read_document,
    _read_inbox_file,
    _send_via_skcomm,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Provide a minimal agent home directory for FUSE tests."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


@pytest.fixture
def memory_dir(agent_home: Path) -> Path:
    """Provide a memory directory with short/mid/long-term subdirs."""
    mem = agent_home / "memory"
    mem.mkdir()
    for layer in ("short-term", "mid-term", "long-term"):
        (mem / layer).mkdir()
    return mem


@pytest.fixture
def sample_memory() -> Dict[str, Any]:
    """Return a sample memory dict with all typical fields populated."""
    return {
        "memory_id": "abc123",
        "created_at": "2026-02-28T12:00:00+00:00",
        "layer": "short-term",
        "importance": 0.85,
        "tags": ["test", "fuse"],
        "soul_context": "lumina",
        "source": "mcp",
        "content": "This is a test memory for FUSE.",
        "metadata": {"origin": "test_suite", "version": "1"},
    }


@pytest.fixture
def sovereign_fs(agent_home: Path, memory_dir: Path) -> SovereignFS:
    """Provide a SovereignFS instance backed by a tmp agent home."""
    return SovereignFS(agent_home=agent_home)


# ---------------------------------------------------------------------------
# TestParsePath
# ---------------------------------------------------------------------------


class TestParsePath:
    """Tests for _parse_path helper that splits virtual FS paths."""

    def test_root_path(self) -> None:
        """Root path returns an empty tuple."""
        assert _parse_path("/") == ()

    def test_single_component(self) -> None:
        """Single-level path returns a one-element tuple."""
        assert _parse_path("/memories") == ("memories",)

    def test_multi_component(self) -> None:
        """Multi-level path returns all components."""
        assert _parse_path("/memories/short/abc123.md") == (
            "memories",
            "short",
            "abc123.md",
        )

    def test_trailing_slash_stripped(self) -> None:
        """Trailing slashes are removed and do not produce empty elements."""
        assert _parse_path("/inbox/") == ("inbox",)

    def test_double_slashes_ignored(self) -> None:
        """Consecutive slashes do not produce empty strings."""
        assert _parse_path("//memories//short//") == ("memories", "short")

    def test_empty_string(self) -> None:
        """Empty string returns an empty tuple like root."""
        assert _parse_path("") == ()


# ---------------------------------------------------------------------------
# TestStatHelpers
# ---------------------------------------------------------------------------


class TestStatHelpers:
    """Tests for _dir_stat and _file_stat stat-dict builders."""

    def test_dir_stat_mode_is_directory(self) -> None:
        """_dir_stat sets the S_IFDIR flag in st_mode."""
        result = _dir_stat()
        assert result["st_mode"] & stat.S_IFDIR

    def test_dir_stat_default_nlink(self) -> None:
        """Default nlink for a directory is 2."""
        result = _dir_stat()
        assert result["st_nlink"] == 2

    def test_dir_stat_custom_nlink(self) -> None:
        """Custom nlink value is honoured."""
        result = _dir_stat(nlink=5)
        assert result["st_nlink"] == 5

    def test_dir_stat_permissions(self) -> None:
        """Directories have 0o555 permission bits (r-xr-xr-x)."""
        result = _dir_stat()
        perms = result["st_mode"] & 0o777
        assert perms == 0o555

    def test_dir_stat_uid_gid(self) -> None:
        """Directory uid/gid match the current process."""
        result = _dir_stat()
        assert result["st_uid"] == os.getuid()
        assert result["st_gid"] == os.getgid()

    def test_file_stat_mode_is_regular(self) -> None:
        """_file_stat sets the S_IFREG flag in st_mode."""
        result = _file_stat(size=42)
        assert result["st_mode"] & stat.S_IFREG

    def test_file_stat_readonly_permissions(self) -> None:
        """Read-only files have 0o444 permission bits."""
        result = _file_stat(size=10, writable=False)
        perms = result["st_mode"] & 0o777
        assert perms == 0o444

    def test_file_stat_writable_permissions(self) -> None:
        """Writable files have 0o644 permission bits."""
        result = _file_stat(size=10, writable=True)
        perms = result["st_mode"] & 0o777
        assert perms == 0o644

    def test_file_stat_size(self) -> None:
        """File size is correctly stored in st_size."""
        result = _file_stat(size=1024)
        assert result["st_size"] == 1024

    def test_file_stat_nlink_always_one(self) -> None:
        """Regular files always have nlink == 1."""
        result = _file_stat(size=0)
        assert result["st_nlink"] == 1

    def test_file_stat_timestamps_present(self) -> None:
        """All three timestamps are numeric and recent."""
        result = _file_stat(size=0)
        now = time.time()
        for key in ("st_atime", "st_mtime", "st_ctime"):
            assert isinstance(result[key], float)
            assert abs(result[key] - now) < 5


# ---------------------------------------------------------------------------
# TestMemoryMarkdown
# ---------------------------------------------------------------------------


class TestMemoryMarkdown:
    """Tests for _memory_to_markdown that renders memory dicts as Markdown."""

    def test_basic_fields(self, sample_memory: Dict[str, Any]) -> None:
        """All standard fields appear in the rendered output."""
        md = _memory_to_markdown(sample_memory).decode("utf-8")
        assert "# Memory: abc123" in md
        assert "**Created:** 2026-02-28T12:00:00+00:00" in md
        assert "**Layer:** short-term" in md
        assert "**Importance:** 0.85" in md
        assert "**Tags:** test, fuse" in md
        assert "**Soul:** lumina" in md
        assert "**Source:** mcp" in md
        assert "## Content" in md
        assert "This is a test memory for FUSE." in md

    def test_metadata_section(self, sample_memory: Dict[str, Any]) -> None:
        """Metadata dict is rendered as a bullet list under ## Metadata."""
        md = _memory_to_markdown(sample_memory).decode("utf-8")
        assert "## Metadata" in md
        assert "- **origin:** test_suite" in md
        assert "- **version:** 1" in md

    def test_missing_optional_fields(self) -> None:
        """Minimal memory with only memory_id and content still renders."""
        minimal: Dict[str, Any] = {
            "memory_id": "min1",
            "content": "Minimal memory.",
        }
        md = _memory_to_markdown(minimal).decode("utf-8")
        assert "# Memory: min1" in md
        assert "Minimal memory." in md
        # Optional fields should not appear
        assert "**Layer:**" not in md
        assert "**Tags:**" not in md

    def test_empty_tags_excluded(self) -> None:
        """An empty tags list does not produce a Tags line."""
        mem: Dict[str, Any] = {"memory_id": "t1", "content": "x", "tags": []}
        md = _memory_to_markdown(mem).decode("utf-8")
        assert "**Tags:**" not in md

    def test_importance_format(self) -> None:
        """Importance is formatted to two decimal places."""
        mem: Dict[str, Any] = {"memory_id": "t2", "content": "x", "importance": 0.5}
        md = _memory_to_markdown(mem).decode("utf-8")
        assert "**Importance:** 0.50" in md

    def test_returns_bytes(self, sample_memory: Dict[str, Any]) -> None:
        """Return type is bytes (UTF-8 encoded)."""
        result = _memory_to_markdown(sample_memory)
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# TestFileHelpers
# ---------------------------------------------------------------------------


class TestFileHelpers:
    """Tests for file listing and reading helpers that work with on-disk data."""

    def test_list_memory_ids_empty_layer(self, memory_dir: Path) -> None:
        """Empty layer directory returns an empty list."""
        ids = _list_memory_ids(memory_dir, "short-term")
        assert ids == []

    def test_list_memory_ids_sorted(self, memory_dir: Path) -> None:
        """Memory IDs are returned sorted alphabetically."""
        layer = memory_dir / "short-term"
        for name in ("zzz", "aaa", "mmm"):
            (layer / f"{name}.json").write_text("{}")
        ids = _list_memory_ids(memory_dir, "short-term")
        assert ids == ["aaa", "mmm", "zzz"]

    def test_list_memory_ids_nonexistent_layer(self, memory_dir: Path) -> None:
        """Non-existent layer directory returns an empty list."""
        ids = _list_memory_ids(memory_dir, "nonexistent")
        assert ids == []

    def test_load_memory_file_success(
        self, memory_dir: Path, sample_memory: Dict[str, Any]
    ) -> None:
        """A valid memory JSON loads and renders as Markdown bytes."""
        layer_dir = memory_dir / "short-term"
        (layer_dir / "abc123.json").write_text(
            json.dumps(sample_memory), encoding="utf-8"
        )
        result = _load_memory_file(memory_dir, "short-term", "abc123")
        assert result is not None
        assert b"# Memory: abc123" in result

    def test_load_memory_file_missing(self, memory_dir: Path) -> None:
        """Missing memory file returns None."""
        result = _load_memory_file(memory_dir, "short-term", "nonexistent")
        assert result is None

    def test_load_memory_file_invalid_json(self, memory_dir: Path) -> None:
        """Corrupt JSON returns None instead of raising."""
        layer_dir = memory_dir / "short-term"
        (layer_dir / "bad.json").write_text("{not valid json", encoding="utf-8")
        result = _load_memory_file(memory_dir, "short-term", "bad")
        assert result is None

    def test_list_inbox_empty(self, agent_home: Path) -> None:
        """No inbox directory returns an empty list."""
        assert _list_inbox(agent_home) == []

    def test_list_inbox_with_files(self, agent_home: Path) -> None:
        """Inbox files are listed and sorted."""
        inbox = agent_home / "comms" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "msg_002.json").write_text("{}")
        (inbox / "msg_001.json").write_text("{}")
        result = _list_inbox(agent_home)
        assert result == ["msg_001.json", "msg_002.json"]

    def test_read_inbox_file_success(self, agent_home: Path) -> None:
        """Reading a valid inbox file returns its bytes."""
        inbox = agent_home / "comms" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "test.msg").write_bytes(b"Hello from sender")
        result = _read_inbox_file(agent_home, "test.msg")
        assert result == b"Hello from sender"

    def test_read_inbox_file_missing(self, agent_home: Path) -> None:
        """Reading a missing inbox file returns None."""
        assert _read_inbox_file(agent_home, "ghost.msg") is None

    def test_list_documents_empty(self, agent_home: Path) -> None:
        """No documents directory returns an empty list."""
        assert _list_documents(agent_home) == []

    def test_list_documents_with_files(self, agent_home: Path) -> None:
        """Document files are listed and sorted."""
        docs = agent_home / "documents"
        docs.mkdir()
        (docs / "contract_b.pdf").write_bytes(b"pdf")
        (docs / "contract_a.pdf").write_bytes(b"pdf")
        result = _list_documents(agent_home)
        assert result == ["contract_a.pdf", "contract_b.pdf"]

    def test_read_document_success(self, agent_home: Path) -> None:
        """A valid document is returned as bytes."""
        docs = agent_home / "documents"
        docs.mkdir()
        (docs / "doc.txt").write_bytes(b"Signed content")
        assert _read_document(agent_home, "doc.txt") == b"Signed content"

    def test_read_document_missing(self, agent_home: Path) -> None:
        """Missing document returns None."""
        assert _read_document(agent_home, "nope.txt") is None

    def test_list_coordination_tasks_empty(self, agent_home: Path) -> None:
        """No coordination directory returns an empty list."""
        assert _list_coordination_tasks(agent_home) == []

    def test_list_coordination_tasks_with_files(self, agent_home: Path) -> None:
        """Coordination task files are listed and sorted."""
        tasks = agent_home / "coordination" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "task_02.json").write_text("{}")
        (tasks / "task_01.json").write_text("{}")
        result = _list_coordination_tasks(agent_home)
        assert result == ["task_01.json", "task_02.json"]

    def test_read_coordination_task_success(self, agent_home: Path) -> None:
        """A valid task JSON file is returned as bytes."""
        tasks = agent_home / "coordination" / "tasks"
        tasks.mkdir(parents=True)
        payload = json.dumps({"title": "Test task"})
        (tasks / "task_01.json").write_text(payload, encoding="utf-8")
        result = _read_coordination_task(agent_home, "task_01.json")
        assert result is not None
        assert b"Test task" in result

    def test_read_coordination_task_missing(self, agent_home: Path) -> None:
        """Missing task file returns None."""
        assert _read_coordination_task(agent_home, "ghost.json") is None

    def test_build_identity_card_fallback(self, agent_home: Path) -> None:
        """Without CapAuth or manifest, falls back to unknown card."""
        with patch("skcapstone.fuse_mount.Path.expanduser", return_value=Path("/nonexistent")):
            card_bytes = _build_identity_card(agent_home)
        card = json.loads(card_bytes)
        assert card["name"] == "unknown"
        assert card["source"] == "fallback"

    def test_build_identity_card_from_manifest(self, agent_home: Path) -> None:
        """Identity card loads from manifest.json when CapAuth is absent."""
        manifest = {
            "name": "opus",
            "identity": {"fingerprint": "DEADBEEF"},
            "created_at": "2026-01-01",
        }
        (agent_home / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        # Ensure CapAuth profile path does not exist
        fake_capauth = agent_home / "no_capauth"
        with patch(
            "skcapstone.fuse_mount.Path.expanduser",
            return_value=fake_capauth,
        ):
            card_bytes = _build_identity_card(agent_home)
        card = json.loads(card_bytes)
        assert card["name"] == "opus"
        assert card["fingerprint"] == "DEADBEEF"
        assert card["source"] == "manifest"

    def test_build_fingerprint_txt_fallback(self, agent_home: Path) -> None:
        """Without CapAuth or manifest, returns placeholder text."""
        with patch("skcapstone.fuse_mount.Path.expanduser", return_value=Path("/nonexistent")):
            fp = _build_fingerprint_txt(agent_home)
        assert fp == b"(no fingerprint)\n"

    def test_build_fingerprint_txt_from_manifest(self, agent_home: Path) -> None:
        """Fingerprint is extracted from manifest.json."""
        manifest = {"identity": {"fingerprint": "AABBCCDD"}}
        (agent_home / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        fake_capauth = agent_home / "no_capauth"
        with patch(
            "skcapstone.fuse_mount.Path.expanduser",
            return_value=fake_capauth,
        ):
            fp = _build_fingerprint_txt(agent_home)
        assert fp == b"AABBCCDD\n"

    def test_send_via_skcomm_fallback_to_outbox(self, agent_home: Path) -> None:
        """When CLI is unavailable, message is queued as JSON envelope in outbox."""
        with patch("skcapstone.fuse_mount.subprocess.run", side_effect=FileNotFoundError):
            result = _send_via_skcomm(agent_home, "jarvis", "Hello Jarvis")
        assert result is True
        outbox = agent_home / "comms" / "outbox"
        files = list(outbox.glob("jarvis_*.json"))
        assert len(files) == 1
        envelope = json.loads(files[0].read_text(encoding="utf-8"))
        assert envelope["recipient"] == "jarvis"
        assert envelope["message"] == "Hello Jarvis"
        assert envelope["delivered"] is False


# ---------------------------------------------------------------------------
# TestSovereignFS
# ---------------------------------------------------------------------------


class TestSovereignFS:
    """Tests for the SovereignFS FUSE operations class."""

    # -- getattr -----------------------------------------------------------

    def test_getattr_root_is_directory(self, sovereign_fs: SovereignFS) -> None:
        """Root path returns directory stat attributes."""
        result = sovereign_fs.getattr("/")
        assert result["st_mode"] & stat.S_IFDIR

    def test_getattr_top_level_dir(self, sovereign_fs: SovereignFS) -> None:
        """Top-level virtual dirs like /memories are directories."""
        result = sovereign_fs.getattr("/memories")
        assert result["st_mode"] & stat.S_IFDIR

    def test_getattr_memories_nlink(self, sovereign_fs: SovereignFS) -> None:
        """The /memories dir has nlink == 2 + number of memory subdirs (3)."""
        result = sovereign_fs.getattr("/memories")
        assert result["st_nlink"] == 5  # 2 + short, mid, long

    def test_getattr_memory_subdir(self, sovereign_fs: SovereignFS) -> None:
        """/memories/short is a valid directory."""
        result = sovereign_fs.getattr("/memories/short")
        assert result["st_mode"] & stat.S_IFDIR

    def test_getattr_identity_file(self, sovereign_fs: SovereignFS) -> None:
        """/identity/fingerprint.txt is a regular file."""
        result = sovereign_fs.getattr("/identity/fingerprint.txt")
        assert result["st_mode"] & stat.S_IFREG

    def test_getattr_enoent(self, sovereign_fs: SovereignFS) -> None:
        """Non-existent path raises OSError with ENOENT."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.getattr("/does_not_exist")
        assert exc_info.value.errno == errno.ENOENT

    def test_getattr_outbox_file_is_writable(
        self, sovereign_fs: SovereignFS
    ) -> None:
        """Outbox files have writable permission bits."""
        # Seed a buffer so the file exists in the virtual FS
        sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] = b"hello"
        result = sovereign_fs.getattr("/outbox/jarvis.msg")
        perms = result["st_mode"] & 0o777
        assert perms == 0o644

    # -- readdir -----------------------------------------------------------

    def test_readdir_root(self, sovereign_fs: SovereignFS) -> None:
        """Root listing includes dot entries and all top-level dirs."""
        entries = sovereign_fs.readdir("/", fh=0)
        assert "." in entries
        assert ".." in entries
        for d in ("memories", "documents", "identity", "inbox", "outbox", "coordination"):
            assert d in entries

    def test_readdir_memories(self, sovereign_fs: SovereignFS) -> None:
        """/memories lists the three layer subdirs."""
        entries = sovereign_fs.readdir("/memories", fh=0)
        assert "short" in entries
        assert "mid" in entries
        assert "long" in entries

    def test_readdir_memories_short_with_files(
        self,
        sovereign_fs: SovereignFS,
        memory_dir: Path,
        sample_memory: Dict[str, Any],
    ) -> None:
        """/memories/short lists .md files for each memory."""
        layer_dir = memory_dir / "short-term"
        (layer_dir / "mem001.json").write_text(
            json.dumps(sample_memory), encoding="utf-8"
        )
        entries = sovereign_fs.readdir("/memories/short", fh=0)
        assert "mem001.md" in entries

    def test_readdir_identity(self, sovereign_fs: SovereignFS) -> None:
        """/identity lists card.json and fingerprint.txt."""
        entries = sovereign_fs.readdir("/identity", fh=0)
        assert "card.json" in entries
        assert "fingerprint.txt" in entries

    def test_readdir_inbox(self, sovereign_fs: SovereignFS, agent_home: Path) -> None:
        """/inbox lists files in the comms/inbox directory."""
        inbox = agent_home / "comms" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "msg_from_ava.json").write_text("{}")
        entries = sovereign_fs.readdir("/inbox", fh=0)
        assert "msg_from_ava.json" in entries

    def test_readdir_outbox_shows_buffered(self, sovereign_fs: SovereignFS) -> None:
        """/outbox lists files that are in the outbox write buffer."""
        sovereign_fs._outbox_buffers["/outbox/lumina.msg"] = b"pending"
        entries = sovereign_fs.readdir("/outbox", fh=0)
        assert "lumina.msg" in entries

    def test_readdir_documents(self, sovereign_fs: SovereignFS, agent_home: Path) -> None:
        """/documents lists files in the documents directory."""
        docs = agent_home / "documents"
        docs.mkdir()
        (docs / "signed.pdf").write_bytes(b"data")
        entries = sovereign_fs.readdir("/documents", fh=0)
        assert "signed.pdf" in entries

    def test_readdir_coordination(self, sovereign_fs: SovereignFS, agent_home: Path) -> None:
        """/coordination lists task JSON files."""
        tasks = agent_home / "coordination" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "task_abc.json").write_text("{}")
        entries = sovereign_fs.readdir("/coordination", fh=0)
        assert "task_abc.json" in entries

    def test_readdir_invalid_path(self, sovereign_fs: SovereignFS) -> None:
        """readdir on a non-directory raises ENOENT."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.readdir("/bogus_dir", fh=0)
        assert exc_info.value.errno == errno.ENOENT

    # -- read --------------------------------------------------------------

    def test_read_memory_content(
        self,
        sovereign_fs: SovereignFS,
        memory_dir: Path,
        sample_memory: Dict[str, Any],
    ) -> None:
        """Reading a memory file returns its Markdown content."""
        layer_dir = memory_dir / "short-term"
        (layer_dir / "abc123.json").write_text(
            json.dumps(sample_memory), encoding="utf-8"
        )
        content = sovereign_fs.read("/memories/short/abc123.md", size=4096, offset=0, fh=0)
        assert b"# Memory: abc123" in content

    def test_read_with_offset_and_size(
        self,
        sovereign_fs: SovereignFS,
        memory_dir: Path,
        sample_memory: Dict[str, Any],
    ) -> None:
        """Read respects offset and size arguments."""
        layer_dir = memory_dir / "short-term"
        (layer_dir / "abc123.json").write_text(
            json.dumps(sample_memory), encoding="utf-8"
        )
        full = sovereign_fs.read("/memories/short/abc123.md", size=99999, offset=0, fh=0)
        partial = sovereign_fs.read("/memories/short/abc123.md", size=5, offset=2, fh=0)
        assert partial == full[2:7]

    def test_read_enoent(self, sovereign_fs: SovereignFS) -> None:
        """Reading a nonexistent file raises ENOENT."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.read("/memories/short/ghost.md", size=4096, offset=0, fh=0)
        assert exc_info.value.errno == errno.ENOENT

    def test_read_identity_fingerprint(self, sovereign_fs: SovereignFS) -> None:
        """/identity/fingerprint.txt is readable."""
        content = sovereign_fs.read("/identity/fingerprint.txt", size=4096, offset=0, fh=0)
        assert isinstance(content, bytes)
        assert len(content) > 0

    # -- open --------------------------------------------------------------

    def test_open_readonly_file(
        self,
        sovereign_fs: SovereignFS,
    ) -> None:
        """Opening a readable file with O_RDONLY succeeds."""
        fh = sovereign_fs.open("/identity/fingerprint.txt", flags=os.O_RDONLY)
        assert fh == 0

    def test_open_write_to_nonoutbox_raises(self, sovereign_fs: SovereignFS) -> None:
        """Writing to a non-outbox path raises EACCES."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.open("/identity/card.json", flags=os.O_WRONLY)
        assert exc_info.value.errno == errno.EACCES

    def test_open_write_outbox_initialises_buffer(
        self, sovereign_fs: SovereignFS
    ) -> None:
        """Opening an outbox file for write initialises the buffer."""
        sovereign_fs.open("/outbox/ava.msg", flags=os.O_WRONLY)
        assert "/outbox/ava.msg" in sovereign_fs._outbox_buffers
        assert sovereign_fs._outbox_buffers["/outbox/ava.msg"] == b""

    def test_open_nonexistent_readonly_raises(self, sovereign_fs: SovereignFS) -> None:
        """Opening a nonexistent file for read raises ENOENT."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.open("/memories/short/ghost.md", flags=os.O_RDONLY)
        assert exc_info.value.errno == errno.ENOENT

    # -- write and flush ---------------------------------------------------

    def test_write_to_outbox(self, sovereign_fs: SovereignFS) -> None:
        """Writing data to an outbox path buffers the bytes."""
        sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] = b""
        n = sovereign_fs.write("/outbox/jarvis.msg", b"Hello!", offset=0, fh=0)
        assert n == 6
        assert sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] == b"Hello!"

    def test_write_appends_at_offset(self, sovereign_fs: SovereignFS) -> None:
        """Subsequent writes at a nonzero offset append to the buffer."""
        sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] = b"Hello"
        sovereign_fs.write("/outbox/jarvis.msg", b" World", offset=5, fh=0)
        assert sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] == b"Hello World"

    def test_write_to_non_outbox_raises(self, sovereign_fs: SovereignFS) -> None:
        """Writing outside /outbox/ raises EACCES."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.write("/memories/short/x.md", b"data", offset=0, fh=0)
        assert exc_info.value.errno == errno.EACCES

    def test_flush_sends_via_skcomm(
        self, sovereign_fs: SovereignFS
    ) -> None:
        """Flushing an outbox file invokes _send_via_skcomm with correct args."""
        sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] = b"Test message"
        with patch("skcapstone.fuse_mount._send_via_skcomm", return_value=True) as mock_send:
            sovereign_fs.flush("/outbox/jarvis.msg", fh=0)
        mock_send.assert_called_once_with(
            sovereign_fs._home, "jarvis", "Test message"
        )
        # Buffer should be cleared after flush
        assert "/outbox/jarvis.msg" not in sovereign_fs._outbox_buffers

    def test_flush_strips_msg_suffix(self, sovereign_fs: SovereignFS) -> None:
        """Flush extracts the recipient by stripping the .msg suffix."""
        sovereign_fs._outbox_buffers["/outbox/lumina.msg"] = b"Hi"
        with patch("skcapstone.fuse_mount._send_via_skcomm", return_value=True) as mock_send:
            sovereign_fs.flush("/outbox/lumina.msg", fh=0)
        assert mock_send.call_args[0][1] == "lumina"

    def test_flush_empty_buffer_no_send(self, sovereign_fs: SovereignFS) -> None:
        """Flushing an empty buffer does not call _send_via_skcomm."""
        sovereign_fs._outbox_buffers["/outbox/ava.msg"] = b""
        with patch("skcapstone.fuse_mount._send_via_skcomm") as mock_send:
            sovereign_fs.flush("/outbox/ava.msg", fh=0)
        mock_send.assert_not_called()

    def test_flush_non_outbox_is_noop(self, sovereign_fs: SovereignFS) -> None:
        """Flushing a non-outbox path is a no-op returning 0."""
        result = sovereign_fs.flush("/memories/short/x.md", fh=0)
        assert result == 0

    # -- create ------------------------------------------------------------

    def test_create_outbox_file(self, sovereign_fs: SovereignFS) -> None:
        """Creating a file under /outbox/ initialises the buffer."""
        fh = sovereign_fs.create("/outbox/opus.msg", mode=0o644)
        assert fh == 0
        assert sovereign_fs._outbox_buffers["/outbox/opus.msg"] == b""

    def test_create_non_outbox_raises(self, sovereign_fs: SovereignFS) -> None:
        """Creating a file outside /outbox/ raises EACCES."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.create("/inbox/hacker.msg", mode=0o644)
        assert exc_info.value.errno == errno.EACCES

    # -- truncate ----------------------------------------------------------

    def test_truncate_outbox(self, sovereign_fs: SovereignFS) -> None:
        """Truncating an outbox buffer shortens it."""
        sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] = b"Hello World"
        sovereign_fs.truncate("/outbox/jarvis.msg", length=5)
        assert sovereign_fs._outbox_buffers["/outbox/jarvis.msg"] == b"Hello"

    def test_truncate_non_outbox_raises(self, sovereign_fs: SovereignFS) -> None:
        """Truncating a non-outbox path raises EACCES."""
        with pytest.raises(OSError) as exc_info:
            sovereign_fs.truncate("/identity/card.json", length=0)
        assert exc_info.value.errno == errno.EACCES

    # -- release -----------------------------------------------------------

    def test_release_flushes_outbox(self, sovereign_fs: SovereignFS) -> None:
        """Release calls flush, which sends the outbox buffer."""
        sovereign_fs._outbox_buffers["/outbox/ava.msg"] = b"Goodbye"
        with patch("skcapstone.fuse_mount._send_via_skcomm", return_value=True) as mock_send:
            sovereign_fs.release("/outbox/ava.msg", fh=0)
        mock_send.assert_called_once()

    # -- pass-through stubs ------------------------------------------------

    def test_chmod_noop(self, sovereign_fs: SovereignFS) -> None:
        """chmod returns 0 (no-op)."""
        assert sovereign_fs.chmod("/inbox", mode=0o777) == 0

    def test_chown_noop(self, sovereign_fs: SovereignFS) -> None:
        """chown returns 0 (no-op)."""
        assert sovereign_fs.chown("/inbox", uid=0, gid=0) == 0

    def test_utimens_noop(self, sovereign_fs: SovereignFS) -> None:
        """utimens returns 0 (no-op)."""
        assert sovereign_fs.utimens("/inbox") == 0


# ---------------------------------------------------------------------------
# TestFUSEDaemon
# ---------------------------------------------------------------------------


class TestFUSEDaemon:
    """Tests for FUSEDaemon lifecycle manager (no real FUSE mounts)."""

    def test_status_when_no_state(self, tmp_path: Path) -> None:
        """Status returns a dict with mounted=False when no state exists."""
        daemon = FUSEDaemon(
            mount_point=tmp_path / "mount",
            agent_home=tmp_path / "home",
        )
        with patch.object(daemon, "_is_mounted", return_value=False):
            status = daemon.status()
        assert status["mounted"] is False
        assert status["pid"] is None
        assert status["updated_at"] is None

    def test_write_and_read_state(self, tmp_path: Path) -> None:
        """State persists to disk and is readable."""
        home = tmp_path / "home"
        home.mkdir()
        daemon = FUSEDaemon(mount_point=tmp_path / "mount", agent_home=home)
        daemon._write_state(mounted=True, pid=12345)
        state = daemon._read_state()
        assert state is not None
        assert state["mounted"] is True
        assert state["pid"] == 12345
        assert "updated_at" in state

    def test_read_state_missing(self, tmp_path: Path) -> None:
        """Reading state when no file exists returns None."""
        daemon = FUSEDaemon(
            mount_point=tmp_path / "mount",
            agent_home=tmp_path / "home",
        )
        assert daemon._read_state() is None

    def test_read_state_corrupt_json(self, tmp_path: Path) -> None:
        """Corrupt state file returns None."""
        home = tmp_path / "home"
        home.mkdir()
        daemon = FUSEDaemon(mount_point=tmp_path / "mount", agent_home=home)
        fuse_dir = home / "fuse"
        fuse_dir.mkdir(parents=True)
        (fuse_dir / "fuse_state.json").write_text("{broken", encoding="utf-8")
        assert daemon._read_state() is None

    def test_status_includes_mount_point(self, tmp_path: Path) -> None:
        """Status dict includes the configured mount point."""
        mnt = tmp_path / "mymount"
        daemon = FUSEDaemon(mount_point=mnt, agent_home=tmp_path / "home")
        with patch.object(daemon, "_is_mounted", return_value=False):
            status = daemon.status()
        assert status["mount_point"] == str(mnt)

    def test_status_includes_agent_home(self, tmp_path: Path) -> None:
        """Status dict includes the configured agent home."""
        home = tmp_path / "home"
        daemon = FUSEDaemon(mount_point=tmp_path / "mnt", agent_home=home)
        with patch.object(daemon, "_is_mounted", return_value=False):
            status = daemon.status()
        assert status["agent_home"] == str(home)

    def test_state_file_path(self, tmp_path: Path) -> None:
        """The state file is at <agent_home>/fuse/fuse_state.json."""
        home = tmp_path / "home"
        daemon = FUSEDaemon(mount_point=tmp_path / "mnt", agent_home=home)
        assert daemon._state_file() == home / "fuse" / "fuse_state.json"

    def test_pid_file_path(self, tmp_path: Path) -> None:
        """The PID file is at <agent_home>/fuse/fuse.pid."""
        home = tmp_path / "home"
        daemon = FUSEDaemon(mount_point=tmp_path / "mnt", agent_home=home)
        assert daemon._pid_file() == home / "fuse" / "fuse.pid"

    def test_status_reads_pid_from_state(self, tmp_path: Path) -> None:
        """Status returns pid from persisted state."""
        home = tmp_path / "home"
        home.mkdir()
        daemon = FUSEDaemon(mount_point=tmp_path / "mnt", agent_home=home)
        daemon._write_state(mounted=True, pid=9999)
        with patch.object(daemon, "_is_mounted", return_value=True):
            status = daemon.status()
        assert status["pid"] == 9999
        assert status["mounted"] is True
