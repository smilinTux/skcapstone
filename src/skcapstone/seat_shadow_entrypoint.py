"""Bounded, read-only shadow beat for the Niobe successor seat."""

from __future__ import annotations

import argparse
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

from .card_store import CardStore
from .seat_mail import poll_mail, startup_hello


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _control(path: Path, host: str) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or host != value.get("active_host"):
        raise ValueError("inactive or invalid control plane")
    seats = value.get("seats")
    if not isinstance(seats, dict) or host not in seats.get("niobe", []):
        raise ValueError("niobe is not provisioned on active host")
    return str(value["revision"])


def run_shadow(*, home: Path, control_plane: Path, local_host: str | None = None) -> dict:
    host = (local_host or socket.gethostname()).strip().lower()
    revision = _control(control_plane, host)
    startup_hello(home, "niobe", host=host)
    mailbox = poll_mail("niobe")
    store = CardStore(home)
    ids = store.list_card_ids()
    open_cards = 0
    assigned = 0
    for card_id in ids:
        try:
            card = store.fold(card_id)
        except Exception:  # noqa: BLE001 - shadow observation skips unreadable cards
            continue
        if card is None or card.archived:
            continue
        open_cards += 1
        labels = {str(label).lower() for label in getattr(card, "labels", ())}
        if "seat-niobe" in labels:
            assigned += 1
    record = {
        "schema": "sk.lifecycle-seat.shadow-beat/v1",
        "seat": "niobe",
        "activation_state": "shadow_only",
        "host": host,
        "control_revision": revision,
        "observed_at": _now(),
        "open_cards": open_cards,
        "seat_cards": assigned,
        "mutation": False,
        **mailbox.as_dict(),
    }
    path = home / "coordination" / "seat-cycles" / "niobe.shadow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--control-plane", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run_shadow(home=args.home, control_plane=args.control_plane), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
