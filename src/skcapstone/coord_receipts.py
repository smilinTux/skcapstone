"""Receipt-backed SKCoord adapters shared by CLI and MCP."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def verified_coord_link(
    home: Path, card_id: str, key: str, value: str, writer: str = ""
) -> dict[str, Any]:
    """Write one annotation through SKCoord and return its verified receipt."""
    key = key.strip()
    value = value.strip()
    if not key or not value:
        raise ValueError("link key and value must not be blank")

    from skcoord.graph_truth import write_verified_annotation

    return write_verified_annotation(
        Path(home),
        card_id,
        "link",
        writer,
        link_key=key,
        link_value=value,
    )
