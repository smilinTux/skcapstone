"""Hermetic provenance checks for the fleet rotation launcher."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCE = REPOSITORY_ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
EXPECTED_SHA256 = "36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e"
EXPECTED_BYTE_COUNT = 74_594
OBSERVED_INSTALLED_MODE = 0o775


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest for exact bytes."""
    return hashlib.sha256(data).hexdigest()


def derive_launcher_provenance(source: Path, installed: Path) -> dict[str, object]:
    """Validate and return deterministic source-to-installed provenance."""
    source_bytes = source.read_bytes()
    installed_bytes = installed.read_bytes()
    source_sha256 = sha256_bytes(source_bytes)
    installed_sha256 = sha256_bytes(installed_bytes)
    installed_mode = stat.S_IMODE(installed.stat().st_mode)

    if source_sha256 != EXPECTED_SHA256:
        raise ValueError(f"source SHA-256 mismatch: {source_sha256}")
    if len(source_bytes) != EXPECTED_BYTE_COUNT:
        raise ValueError(f"source byte count mismatch: {len(source_bytes)}")
    if installed_bytes != source_bytes:
        raise ValueError("installed bytes differ from governed source")
    if installed_sha256 != EXPECTED_SHA256:
        raise ValueError(f"installed SHA-256 mismatch: {installed_sha256}")
    if installed_mode != OBSERVED_INSTALLED_MODE:
        raise ValueError(f"installed mode mismatch: {installed_mode:04o}")

    return {
        "bytes_match": True,
        "installed_bytes": len(installed_bytes),
        "installed_mode": f"{installed_mode:04o}",
        "installed_sha256": installed_sha256,
        "source_bytes": len(source_bytes),
        "source_sha256": source_sha256,
    }


def write_installed_fixture(
    directory: Path,
    *,
    content: bytes | None = None,
    mode: int = OBSERVED_INSTALLED_MODE,
) -> Path:
    """Create an installed-launcher fixture only inside a pytest temp path."""
    installed = directory / "skfleet-rotate.py"
    installed.write_bytes(LAUNCHER_SOURCE.read_bytes() if content is None else content)
    installed.chmod(mode)
    return installed


def test_repository_source_is_exact() -> None:
    """The governed source has the expected immutable bytes."""
    source_bytes = LAUNCHER_SOURCE.read_bytes()
    assert len(source_bytes) == EXPECTED_BYTE_COUNT
    assert sha256_bytes(source_bytes) == EXPECTED_SHA256


def test_derivation_is_exact_and_repeatable(tmp_path: Path) -> None:
    """Repeated derivation produces identical results and no evidence writes."""
    installed = write_installed_fixture(tmp_path)
    first = derive_launcher_provenance(LAUNCHER_SOURCE, installed)
    second = derive_launcher_provenance(LAUNCHER_SOURCE, installed)

    assert (
        first
        == second
        == {
            "bytes_match": True,
            "installed_bytes": EXPECTED_BYTE_COUNT,
            "installed_mode": "0775",
            "installed_sha256": EXPECTED_SHA256,
            "source_bytes": EXPECTED_BYTE_COUNT,
            "source_sha256": EXPECTED_SHA256,
        }
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["skfleet-rotate.py"]


def test_derivation_rejects_byte_drift(tmp_path: Path) -> None:
    """A one-byte installed change fails closed."""
    installed = write_installed_fixture(
        tmp_path,
        content=LAUNCHER_SOURCE.read_bytes() + b"\n",
    )

    with pytest.raises(ValueError, match="installed bytes differ"):
        derive_launcher_provenance(LAUNCHER_SOURCE, installed)


def test_derivation_rejects_mode_drift(tmp_path: Path) -> None:
    """An installed mode other than the observed exact mode fails closed."""
    installed = write_installed_fixture(tmp_path, mode=0o755)

    with pytest.raises(ValueError, match="installed mode mismatch: 0755"):
        derive_launcher_provenance(LAUNCHER_SOURCE, installed)
