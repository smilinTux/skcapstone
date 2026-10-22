"""Restricted native SSH access to exact card reads and candidate artifacts."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .source_transport import serve

CLI = str(Path.home() / ".skenv/bin/skcapstone")
ARTIFACT_COMMAND = "~/.skenv/bin/python -m skcapstone.fleet.source_transport"


def command_binding(command: str) -> tuple[str, str]:
    """Preserve the original exact native card-read allowlist without a shell."""
    args = shlex.split(command)
    if (
        len(args) != 8
        or args[0] != "env"
        or args[3:6] != [CLI, "coord", "show"]
        or args[7] != "--json"
    ):
        raise ValueError("command refused")
    owner = args[1].removeprefix("SKAGENT=")
    if args[1] != "SKAGENT=" + owner or args[2] != "SKCAPSTONE_AGENT=" + owner:
        raise ValueError("identity refused")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", owner) or not re.fullmatch(
        r"[0-9a-f]{8}", args[6]
    ):
        raise ValueError("binding refused")
    return owner, args[6]


def document_allowed(row: dict, owner: str, card: str) -> bool:
    """Keep the stricter existing ZIO owner and active-source claim boundary."""
    return (
        isinstance(row, dict)
        and row.get("id") == card
        and row.get("owner") == owner
        and row.get("status") in {"ready", "doing"}
        and not row.get("archived")
        and "source-only" in (row.get("labels") or [])
        and re.fullmatch(r"[0-9a-f]{32}", str((row.get("meta") or {}).get("_claim_revision", "")))
        is not None
    )


def main(*, source_claim: bool = False) -> int:
    """Admit one literal bounded artifact protocol or the existing exact read."""
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if original == ARTIFACT_COMMAND:
        serve()
        return 0
    try:
        owner, card = command_binding(original)
        home = str(Path.home())
        result = subprocess.run(
            [CLI, "coord", "show", card, "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            env={
                "HOME": home,
                "PATH": home + "/.skenv/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "SKAGENT": owner,
                "SKCAPSTONE_AGENT": owner,
            },
        )
        if result.returncode or len(result.stdout) > 1_048_576:
            raise ValueError("read refused")
        row = json.loads(result.stdout)
        if source_claim and not document_allowed(row, owner, card):
            raise ValueError("document refused")
        print(json.dumps(row, sort_keys=True))
        return 0
    except (ValueError, TypeError, OSError, subprocess.SubprocessError):
        print("Restricted SSH: native card or source artifact command required.", file=sys.stderr)
        return 126 if source_claim else 77


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--source-claim"]):
        raise SystemExit(77)
    raise SystemExit(main(source_claim=sys.argv[1:] == ["--source-claim"]))
