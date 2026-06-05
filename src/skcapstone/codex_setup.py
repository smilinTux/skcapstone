"""Codex bootstrap setup for SK agent context.

Codex reads ``AGENTS.md`` files into the model prompt, but environment
variables alone are not visible enough to make an agent profile active in a
new session. This module keeps the global Codex bootstrap idempotent and
repairable from install scripts and ``skcapstone doctor --fix``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

START_MARKER = "<!-- SKCAPSTONE_CODEX_AGENT_CONTEXT_START -->"
END_MARKER = "<!-- SKCAPSTONE_CODEX_AGENT_CONTEXT_END -->"
LOADER_NAME = "load-sk-agent-context.sh"


def codex_home() -> Path:
    """Return the Codex config home, honoring CODEX_HOME when set."""
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def resolve_default_agent() -> str:
    """Resolve the current default SK agent for generated guidance."""
    for env_name in ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value

    try:
        from . import active_agent_name

        active = active_agent_name()
        if active:
            return active
    except Exception:
        pass

    return "sovereign"


def render_loader_script() -> str:
    """Render the Codex context loader script."""
    return """#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.skenv/bin:$PATH"
export SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"

AGENT="${1:-${SKAGENT:-${SKCAPSTONE_AGENT:-${SKMEMORY_AGENT:-}}}}"
if [[ -z "$AGENT" && -d "$SKCAPSTONE_HOME/agents" ]]; then
  AGENT="$(find "$SKCAPSTONE_HOME/agents" -mindepth 1 -maxdepth 1 -type d ! -name '*-template' -exec basename {} \\; 2>/dev/null | sort | head -n1 || true)"
fi
if [[ -z "$AGENT" ]]; then
  echo "No active SK agent could be resolved." >&2
  exit 1
fi

export SKAGENT="$AGENT"
export SKCAPSTONE_AGENT="$AGENT"
export SKMEMORY_AGENT="${SKMEMORY_AGENT:-$AGENT}"

AGENT_HOME="$SKCAPSTONE_HOME/agents/$AGENT"
MEMORIES="${SKCAPSTONE_CONTEXT_MEMORIES:-10}"

echo "# SK Agent Bootstrap"
echo
echo "agent=$AGENT"
echo "agent_home=$AGENT_HOME"
echo "skmemory_home=$AGENT_HOME/memory"
echo

echo "## skcapstone context"
if command -v skcapstone >/dev/null 2>&1; then
  skcapstone context show --home "$AGENT_HOME" --format json --memories "$MEMORIES" || skcapstone status --home "$AGENT_HOME" || true
else
  echo "skcapstone command not found on PATH."
fi

echo
echo "## skmemory ritual"
if command -v skmemory >/dev/null 2>&1; then
  skmemory ritual --full || skmemory ritual || true
else
  echo "skmemory command not found on PATH."
fi
"""


def render_agents_block(
    *,
    loader_path: Path,
    agent_name: str | None = None,
    skcapstone_home: Path | None = None,
) -> str:
    """Render the managed global Codex AGENTS.md block."""
    agent = agent_name or resolve_default_agent()
    sk_home = skcapstone_home or Path(os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone")).expanduser()
    return f"""{START_MARKER}
# SKCapstone Agent Context

This Codex installation is wired to the local SK* sovereign agent stack. Use
the active SK agent profile for SKCapstone, SKMemory, SKWhisper, CapAuth,
SKSeed, SKPerf, and related local stack work.

Active agent resolution order: `$SKAGENT`, `$SKCAPSTONE_AGENT`,
`$SKMEMORY_AGENT`, then installed agents under `{sk_home}/agents/`. Current
install default: `{agent}`.

At the start of SK* work, identity/status questions, or tasks involving the
local sovereign stack, run:

```bash
{loader_path}
```

Treat that output as the current agent context. When asked who you are, what
profile is active, or what your OOF/status is, answer from the current
SKMemory ritual / SKCapstone context instead of generic Codex defaults.
{END_MARKER}
"""


def loader_path(home: Path | None = None) -> Path:
    """Return the expected Codex loader path."""
    base = home or codex_home()
    return base / "bin" / LOADER_NAME


def agents_path(home: Path | None = None) -> Path:
    """Return the expected global Codex AGENTS.md path."""
    base = home or codex_home()
    return base / "AGENTS.md"


def has_functional_agents_bootstrap(text: str) -> bool:
    """Return True when AGENTS.md already has enough SK bootstrap guidance."""
    required = (
        LOADER_NAME,
        "SKAGENT",
        "SKCAPSTONE_AGENT",
        "SKMEMORY_AGENT",
        "OOF",
    )
    return all(token in text for token in required)


def has_functional_loader_bootstrap(text: str) -> bool:
    """Return True when the loader can emit SKCapstone/SKMemory context."""
    required = (
        "SKAGENT",
        "SKCAPSTONE_AGENT",
        "SKMEMORY_AGENT",
        "skcapstone",
        "skmemory",
    )
    return all(token in text for token in required)


def ensure_codex_setup(
    *,
    home: Path | None = None,
    agent_name: str | None = None,
    skcapstone_home: Path | None = None,
) -> list[str]:
    """Create or repair the global Codex SK agent bootstrap.

    Returns:
        Human-readable actions performed. An empty list means no changes were
        needed.
    """
    base = home or codex_home()
    base.mkdir(parents=True, exist_ok=True)

    actions: list[str] = []

    script_path = loader_path(base)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_content = render_loader_script()
    existing_script = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    if not has_functional_loader_bootstrap(existing_script):
        script_path.write_text(script_content, encoding="utf-8")
        actions.append(f"wrote {script_path}")

    mode = script_path.stat().st_mode
    executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if mode & stat.S_IXUSR == 0:
        script_path.chmod(mode | executable_bits)
        actions.append(f"made {script_path} executable")

    ag_path = agents_path(base)
    existing = ag_path.read_text(encoding="utf-8") if ag_path.exists() else ""
    block = render_agents_block(
        loader_path=script_path,
        agent_name=agent_name,
        skcapstone_home=skcapstone_home,
    )

    if START_MARKER in existing and END_MARKER in existing:
        before, remainder = existing.split(START_MARKER, 1)
        _, after = remainder.split(END_MARKER, 1)
        updated = before.rstrip() + "\n\n" + block.rstrip() + "\n" + after
    elif has_functional_agents_bootstrap(existing):
        updated = existing
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block
    else:
        updated = block

    if updated != existing:
        ag_path.write_text(updated, encoding="utf-8")
        actions.append(f"updated {ag_path}")

    return actions


def check_codex_setup(home: Path | None = None) -> tuple[bool, str]:
    """Check whether Codex has the SK agent bootstrap installed."""
    base = home or codex_home()
    missing: list[str] = []

    script_path = loader_path(base)
    if not script_path.exists():
        missing.append(str(script_path))
    elif not has_functional_loader_bootstrap(script_path.read_text(encoding="utf-8")):
        missing.append(f"{script_path} SK context loader")
    elif not os.access(script_path, os.X_OK):
        missing.append(f"{script_path} executable bit")

    ag_path = agents_path(base)
    if not ag_path.exists():
        missing.append(str(ag_path))
    else:
        text = ag_path.read_text(encoding="utf-8")
        if START_MARKER not in text and not has_functional_agents_bootstrap(text):
            missing.append(f"{ag_path} SK bootstrap instructions")

    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, f"{ag_path} + {script_path}"
