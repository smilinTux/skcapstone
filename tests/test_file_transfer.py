"""Tests for encrypted file transfer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.file_transfer import (
    FileTransfer,
    TransferManifest,
    ChunkInfo,
    TransferStatus,
    CHUNK_SIZE,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home for file transfer tests."""
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "identity.json").write_text(json.dumps({
        "name": "test-agent",
        "fingerprint": "ABCD1234567890ABCDEF1234567890ABCDEF1234",
    }), encoding="utf-8")

    security_dir = tmp_path / "security"
    security_dir.mkdir()

    return tmp_path


@pytest.fixture
def ft(home: Path) -> FileTransfer:
    """Create an initialized FileTransfer instance."""
    t = FileTransfer(home, agent_name="opus", chunk_size=1024)
    t.initialize()
    return t


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    """Create a sample file for transfer."""
    f = tmp_path / "test-document.txt"
    f.write_text("Hello, sovereign world! " * 100, encoding="utf-8")
    return f


@pytest.fixture
def large_file(tmp_path: Path) -> Path:
    """Create a file larger than one chunk."""
    f = tmp_path / "large-file.bin"
    f.write_bytes(b"X" * 3000)  # 3 chunks at 1024 bytes
    return f


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for file transfer setup."""

    def test_initialize_creates_dirs(self, home: Path) -> None:
        """Initialize creates directory structure."""
        ft = FileTransfer(home)
        ft.initialize()
        assert (home / "file-transfer" / "outbox").is_dir()
        assert (home / "file-transfer" / "inbox").is_dir()
        assert (home / "file-transfer" / "completed").is_dir()

    def test_initialize_idempotent(self, ft: FileTransfer, home: Path) -> None:
        """Multiple initializations don't break anything."""
        ft.initialize()
        ft.initialize()
        assert (home / "file-transfer" / "outbox").is_dir()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class TestSend:
    """Tests for file sending."""

    def test_send_creates_manifest(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Send creates a manifest file."""
        manifest = ft.send(sample_file, recipient="lumina")
        manifest_path = home / "file-transfer" / "outbox" / manifest.transfer_id / "manifest.json"
        assert manifest_path.exists()

    def test_send_creates_chunks(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Send creates encrypted chunk files."""
        manifest = ft.send(sample_file, recipient="lumina")
        transfer_dir = home / "file-transfer" / "outbox" / manifest.transfer_id
        chunks = list(transfer_dir.glob("chunk-*.enc"))
        assert len(chunks) == manifest.total_chunks
        assert len(chunks) > 0

    def test_send_manifest_metadata(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Manifest contains correct file metadata."""
        manifest = ft.send(sample_file, recipient="lumina")
        assert manifest.filename == "test-document.txt"
        assert manifest.file_size == sample_file.stat().st_size
        assert manifest.sender == "opus"
        assert manifest.recipient == "lumina"
        assert manifest.file_sha256
        assert manifest.total_chunks > 0

    def test_send_marks_chunks_sent(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """All chunks are marked as sent."""
        manifest = ft.send(sample_file, recipient="grok")
        assert all(c.sent for c in manifest.chunks)

    def test_send_multiple_chunks(
        self, ft: FileTransfer, large_file: Path,
    ) -> None:
        """Large files produce multiple chunks."""
        manifest = ft.send(large_file, recipient="lumina")
        assert manifest.total_chunks == 3

    def test_send_nonexistent_file_raises(self, ft: FileTransfer) -> None:
        """Sending a nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ft.send(Path("/nonexistent/file.txt"), recipient="lumina")

    def test_send_empty_file_raises(self, ft: FileTransfer, tmp_path: Path) -> None:
        """Sending an empty file raises ValueError."""
        empty = tmp_path / "empty.txt"
        empty.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            ft.send(empty, recipient="lumina")

    def test_send_without_encryption(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Files can be sent without encryption."""
        manifest = ft.send(sample_file, recipient="lumina", encrypt=False)
        assert all(not c.encrypted for c in manifest.chunks)


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


class TestReceive:
    """Tests for file receiving."""

    def test_receive_reassembles_file(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Receive reassembles the original file."""
        manifest = ft.send(sample_file, recipient="lumina")
        output = ft.receive(manifest.transfer_id)
        assert output.exists()
        assert output.read_bytes() == sample_file.read_bytes()

    def test_receive_large_file(
        self, ft: FileTransfer, large_file: Path,
    ) -> None:
        """Receive handles multi-chunk files."""
        manifest = ft.send(large_file, recipient="lumina")
        output = ft.receive(manifest.transfer_id)
        assert output.read_bytes() == large_file.read_bytes()

    def test_receive_records_completion(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Receive writes a completion receipt."""
        manifest = ft.send(sample_file, recipient="lumina")
        ft.receive(manifest.transfer_id)
        receipt = home / "file-transfer" / "completed" / f"{manifest.transfer_id}.json"
        assert receipt.exists()

    def test_receive_custom_output_dir(
        self, ft: FileTransfer, sample_file: Path, tmp_path: Path,
    ) -> None:
        """Receive writes to custom output directory."""
        manifest = ft.send(sample_file, recipient="lumina")
        output_dir = tmp_path / "downloads"
        output = ft.receive(manifest.transfer_id, output_dir=output_dir)
        assert output.parent == output_dir
        assert output.read_bytes() == sample_file.read_bytes()

    def test_receive_nonexistent_transfer_raises(self, ft: FileTransfer) -> None:
        """Receiving a nonexistent transfer raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            ft.receive("nonexistent-id")

    def test_receive_detects_tampering(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Receive detects tampered chunks."""
        manifest = ft.send(sample_file, recipient="lumina", encrypt=False)
        # Tamper with a chunk
        transfer_dir = home / "file-transfer" / "outbox" / manifest.transfer_id
        chunk_file = list(transfer_dir.glob("chunk-*.enc"))[0]
        chunk_file.write_bytes(b"TAMPERED DATA")

        with pytest.raises(ValueError, match="integrity"):
            ft.receive(manifest.transfer_id)

    def test_receive_without_encryption(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Unencrypted transfers reassemble correctly."""
        manifest = ft.send(sample_file, recipient="lumina", encrypt=False)
        output = ft.receive(manifest.transfer_id)
        assert output.read_bytes() == sample_file.read_bytes()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestResume:
    """Tests for transfer resume functionality."""

    def test_resume_send_complete(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Complete transfer has no unsent chunks."""
        manifest = ft.send(sample_file, recipient="lumina")
        unsent = ft.resume_send(manifest.transfer_id)
        assert unsent == []

    def test_resume_receive_all_present(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Transfer with all chunks has no missing."""
        manifest = ft.send(sample_file, recipient="lumina")
        missing = ft.resume_receive(manifest.transfer_id)
        assert missing == []

    def test_resume_receive_missing_chunks(
        self, ft: FileTransfer, large_file: Path, home: Path,
    ) -> None:
        """Detect missing chunks for resume."""
        manifest = ft.send(large_file, recipient="lumina")
        # Delete a chunk to simulate interrupted transfer
        transfer_dir = home / "file-transfer" / "outbox" / manifest.transfer_id
        (transfer_dir / "chunk-0001.enc").unlink()

        missing = ft.resume_receive(manifest.transfer_id)
        assert 1 in missing

    def test_resume_nonexistent_transfer(self, ft: FileTransfer) -> None:
        """Resume on nonexistent transfer returns empty."""
        assert ft.resume_send("ghost") == []
        assert ft.resume_receive("ghost") == []


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListTransfers:
    """Tests for transfer listing."""

    def test_list_empty(self, ft: FileTransfer) -> None:
        """Empty transfer system returns no items."""
        assert ft.list_transfers() == []

    def test_list_after_send(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Sent transfers appear in listing."""
        ft.send(sample_file, recipient="lumina")
        transfers = ft.list_transfers()
        assert len(transfers) == 1
        assert transfers[0].direction == "send"
        assert transfers[0].filename == "test-document.txt"

    def test_list_filter_direction(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Can filter by send/receive direction."""
        ft.send(sample_file, recipient="lumina")
        assert len(ft.list_transfers(direction="send")) == 1
        assert len(ft.list_transfers(direction="receive")) == 0

    def test_list_progress(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Transfer status shows progress."""
        ft.send(sample_file, recipient="lumina")
        status = ft.list_transfers()[0]
        assert status.progress == 1.0
        assert status.chunks_done == status.total_chunks


# ---------------------------------------------------------------------------
# Manifest and status
# ---------------------------------------------------------------------------


class TestManifestAndStatus:
    """Tests for manifest retrieval and status."""

    def test_get_manifest(
        self, ft: FileTransfer, sample_file: Path,
    ) -> None:
        """Get manifest by transfer ID."""
        manifest = ft.send(sample_file, recipient="lumina")
        retrieved = ft.get_manifest(manifest.transfer_id)
        assert retrieved is not None
        assert retrieved.filename == manifest.filename

    def test_get_manifest_nonexistent(self, ft: FileTransfer) -> None:
        """Getting nonexistent manifest returns None."""
        assert ft.get_manifest("ghost") is None

    def test_status_summary(self, ft: FileTransfer, sample_file: Path) -> None:
        """Status returns structured summary."""
        ft.send(sample_file, recipient="lumina")
        status = ft.status()
        assert status["outbox_transfers"] == 1
        assert status["inbox_transfers"] == 0


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for transfer cleanup."""

    def test_cleanup_removes_files(
        self, ft: FileTransfer, sample_file: Path, home: Path,
    ) -> None:
        """Cleanup removes transfer directory."""
        manifest = ft.send(sample_file, recipient="lumina")
        assert ft.cleanup(manifest.transfer_id) is True
        transfer_dir = home / "file-transfer" / "outbox" / manifest.transfer_id
        assert not transfer_dir.exists()

    def test_cleanup_nonexistent(self, ft: FileTransfer) -> None:
        """Cleaning up nonexistent transfer returns False."""
        assert ft.cleanup("ghost") is False


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for transfer models."""

    def test_manifest_progress_empty(self) -> None:
        """Empty manifest has zero progress."""
        m = TransferManifest(
            filename="test", file_size=100,
            file_sha256="abc", total_chunks=0,
        )
        assert m.progress == 0.0

    def test_manifest_is_complete(self) -> None:
        """Complete manifest detected correctly."""
        m = TransferManifest(
            filename="test", file_size=100,
            file_sha256="abc", total_chunks=1,
            chunks=[ChunkInfo(index=0, size=100, sha256="def", sent=True, received=True)],
        )
        assert m.is_complete is True

    def test_manifest_not_complete(self) -> None:
        """Incomplete manifest detected correctly."""
        m = TransferManifest(
            filename="test", file_size=100,
            file_sha256="abc", total_chunks=1,
            chunks=[ChunkInfo(index=0, size=100, sha256="def", sent=True, received=False)],
        )
        assert m.is_complete is False

    def test_chunk_info_defaults(self) -> None:
        """ChunkInfo has sensible defaults."""
        c = ChunkInfo(index=0, size=1024, sha256="abc")
        assert c.encrypted is True
        assert c.sent is False
        assert c.received is False
