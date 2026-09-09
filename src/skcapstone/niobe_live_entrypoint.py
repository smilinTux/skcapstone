"""Fail-closed Niobe wrapper for the existing fleet dispatcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from .niobe_activation import LIVE_UNIT, parse_activation
from .seat_mail import poll_mail, startup_hello


def _verify_card_fence(home: Path, card_id: str, expected_revision: str) -> None:
    core = home / "cards" / card_id / "core.json"
    if not core.is_file():
        raise ValueError("Niobe approval card is missing")
    actual = hashlib.sha256(core.read_bytes()).hexdigest()
    if actual != expected_revision:
        raise ValueError("Niobe approval card revision is stale")


def run_live(
    *,
    activation_path: Path,
    dispatcher: Path,
    local_host: str | None = None,
    runner=subprocess.run,
) -> int:
    value = json.loads(activation_path.read_text(encoding="utf-8"))
    activation = parse_activation(value)
    host = (local_host or socket.gethostname()).strip().lower()
    if host != activation.host:
        raise ValueError("Niobe live unit refused on inactive host")
    if not dispatcher.is_file():
        raise ValueError("Niobe dispatcher is missing")
    home = activation_path.parent.parent
    _verify_card_fence(home, activation.card_id, activation.card_revision)
    startup_hello(home, "niobe", host=host)
    poll_mail("niobe")
    environment = os.environ.copy()
    environment["SKFLEET_NIOBE_ACTIVATION"] = str(activation_path.resolve())
    completed = runner(
        [sys.executable, str(dispatcher), "--go"],
        check=False,
        env=environment,
    )
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--dispatcher", type=Path, required=True)
    parser.add_argument("--unit", default=LIVE_UNIT)
    args = parser.parse_args(argv)
    if args.unit != LIVE_UNIT:
        raise ValueError("Niobe live unit name does not match the decision")
    return run_live(activation_path=args.activation, dispatcher=args.dispatcher)


if __name__ == "__main__":
    raise SystemExit(main())
