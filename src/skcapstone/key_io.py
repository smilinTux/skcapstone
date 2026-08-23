"""Shared readers for CapAuth OpenPGP public key files.

CapAuth sometimes writes ``public.asc`` as raw binary OpenPGP packets
despite the ``.asc`` name. Reading such a file as UTF-8 text crashes
with ``UnicodeDecodeError``. Every reader of identity public keys
should go through :func:`read_armored_public_key` so armored and
binary files both work.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_armored_public_key(path: Path) -> str:
    """Read a public key file as ASCII armor, converting binary if needed.

    Args:
        path: Path to the public key file (usually ``public.asc``).

    Returns:
        str: The ASCII-armored public key, or ``""`` when the file cannot
        be read or parsed.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Failed to read public key from %s: %s", path, exc)
        return ""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return armor_binary_key(path)


def armor_binary_key(pub_key_path: Path) -> str:
    """Convert a binary OpenPGP public key file to ASCII armor.

    CapAuth sometimes writes public.asc as raw binary OpenPGP packets
    instead of ASCII-armored text. Parse it with PGPy (when installed)
    and re-emit the armored form.

    Args:
        pub_key_path: Path to the binary key file.

    Returns:
        str: The ASCII-armored public key, or "" if it cannot be parsed.
    """
    try:
        import pgpy

        key, _ = pgpy.PGPKey.from_file(str(pub_key_path))
        return str(key).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse binary public key from %s: %s", pub_key_path, exc)
        return ""
