"""Attributed machine Git identity in the exact clean worker environment."""

import re
import subprocess


def identity(owner: str) -> dict[str, str]:
    """Derive non-delivery commit metadata only from the assigned worker owner."""
    if not re.fullmatch(r"pi-[a-z0-9][a-z0-9-]{1,180}-[0-9a-f]{8}", owner):
        raise ValueError("validated assigned worker identity required")
    return {
        "GIT_AUTHOR_NAME": owner,
        "GIT_AUTHOR_EMAIL": owner + "@noreply.invalid",
        "GIT_COMMITTER_NAME": owner,
        "GIT_COMMITTER_EMAIL": owner + "@noreply.invalid",
    }


def preflight(command: list[str], workspace, owner: str) -> None:
    """Verify both effective identities using the launcher's env -i arguments."""
    expected = identity(owner)
    if command[:2] != ["/usr/bin/env", "-i"]:
        raise ValueError("clean worker environment required")
    end = 2
    while end < len(command) and "=" in command[end]:
        end += 1
    environment = dict(item.split("=", 1) for item in command[2:end])
    if any(environment.get(key) != value for key, value in expected.items()):
        raise ValueError("assigned worker Git identity differs")
    for role in ("AUTHOR", "COMMITTER"):
        try:
            result = subprocess.run(
                command[:end] + ["/usr/bin/git", "var", f"GIT_{role}_IDENT"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("worker Git identity preflight unavailable") from exc
        prefix = f"{owner} <{owner}@noreply.invalid> "
        if result.returncode or not result.stdout.startswith(prefix):
            raise ValueError("worker Git identity preflight failed")
