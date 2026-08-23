"""Non-interactive provisioning of lightweight fleet role agents.

The interactive ``skcapstone init`` / ``skcapstone onboard`` wizard targets
full sovereign agents: PGP identity via CapAuth, SKMemory layers, trust chain,
soul blueprint, security audit, and sync. Fleet operators also need *lightweight*
role agents (workers, reviewers) that exist so the coordination board, the
Telegram bridge, and per-agent tooling have a real home to read - without
prompts and without the sovereign pillar stack.

This module scaffolds exactly that, modeled on the hand-built reference
profile at ``~/.skcapstone/agents/veritas/``:

- ``identity/identity.json`` - name, role, mandate, ``capauth_managed: false``
- ``profile.yaml`` - bridge-curation block (same shape as
  ``skcapstone agent profile --init`` writes)
- ``MANDATE.md`` - optional role mandate template

Everything here is pure filesystem work with no prompts, no network, and no
optional-dependency imports, so it is safe to call from scripts and CI.

See ``docs/LIGHTWEIGHT_AGENTS.md`` for the lightweight-vs-sovereign capability
delta and the upgrade path to a full sovereign profile.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from . import SHARED_ROOT

IDENTITY_FILENAME = "identity.json"
PROFILE_FILENAME = "profile.yaml"
MANDATE_FILENAME = "MANDATE.md"

# Role slugs with a dedicated mandate template. Any other role string gets the
# generic template.
ROLE_REVIEWER = "reviewer"
ROLE_WORKER = "worker"


class ProvisionResult(BaseModel):
    """Outcome of a lightweight agent provisioning run."""

    agent: str = Field(description="Agent slug (directory name).")
    home: str = Field(description="Absolute path of the agent home.")
    role: str = Field(description="Role recorded in identity.json.")
    files: list[str] = Field(default_factory=list, description="Absolute paths written, in order.")


def slugify_name(name: str) -> str:
    """Normalize a display name into an agent directory slug.

    Matches the convention used elsewhere in skcapstone (lowercase, spaces to
    hyphens) and additionally strips characters that are unsafe in paths.

    Args:
        name: Human-provided agent name (e.g. ``"Veritas"``, ``"review bot"``).

    Returns:
        Lowercase slug such as ``veritas`` or ``review-bot``.

    Raises:
        ValueError: If nothing usable remains after normalization.
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace(" ", "-")).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"Agent name {name!r} produces an empty slug")
    return slug


def default_agent_home(name: str, shared_root: Path | None = None) -> Path:
    """Resolve the conventional home for a lightweight agent.

    Args:
        name: Agent name (will be slugified).
        shared_root: Override for the shared root; defaults to
            ``skcapstone.SHARED_ROOT`` (``~/.skcapstone``).

    Returns:
        ``<shared_root>/agents/<slug>``.
    """
    root = Path(shared_root if shared_root is not None else SHARED_ROOT).expanduser()
    return root / "agents" / slugify_name(name)


def mandate_template(name: str, role: str) -> str:
    """Render a MANDATE.md template for a role.

    Args:
        name: Agent display name.
        role: Role slug (``reviewer`` and ``worker`` have dedicated templates;
            anything else falls back to a generic template).

    Returns:
        Markdown mandate text.
    """
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        f"# {name} - Lightweight {role} agent\n\n"
        f"Created {created} via `skcapstone init --non-interactive`.\n"
    )

    if role == ROLE_REVIEWER:
        body = """
## Role

Independent review and verification agent for the SK fleet. Reviews cards it
did not implement: runs quality gates, verifies completion evidence against
acceptance criteria, and moves cards through review. Never implements the
cards it reviews.

## Separation of duties (hard rule)

- NEVER review a card this agent implemented or co-implemented.
- NEVER modify product source in a reviewed repository. Allowed writes:
  board events (claim/move/complete/describe-with-findings) and review
  evidence artifacts when the assigning card asks for them.
- Defects are written on the card as findings and the card moves back to
  doing. Never fix silently.

## Review checklist (run in order, record each result)

1. Claim the review card before starting.
2. Read the card's acceptance criteria.
3. Confirm the implementer's completion evidence names real files, real
   test output, and real commands.
4. Re-run the smallest relevant gate set yourself. Never accept pasted
   output without a local re-run for security-relevant cards.
5. Check the work against the repo's AGENTS.md rules.
6. Record the verdict on the card: evidence checked, commands run, exact
   results, findings with severity.
7. Move the card: done only when every acceptance criterion has verified
   evidence; otherwise back to doing with blocking findings.
"""
    elif role == ROLE_WORKER:
        body = """
## Role

Fleet worker agent. Picks up implementation cards from the coordination
board, does the work in an isolated worktree, runs the repo's gates, and
reports evidence on the card.

## Working rules

- Work only cards claimed by this agent; never touch another agent's claim.
- Never work in the main checkout. Use an isolated worktree per card.
- Follow the repo's AGENTS.md conventions; run the relevant tests before
  reporting completion.
- Report exact commands, outputs, files changed, and caveats on the card.
"""
    else:
        body = f"""
## Role

Lightweight `{role}` agent in the SK fleet. Replace this section with the
specific duties, allowed writes, and escalation path for this role.

## Working rules

- Work only cards claimed by this agent.
- Follow each repo's AGENTS.md conventions.
- Report exact evidence (commands, outputs, files) on every card.
"""
    return header + body


def build_identity_document(
    name: str,
    role: str,
    mandate: str,
    created_by: str,
) -> dict:
    """Build the identity.json document for a lightweight agent.

    Args:
        name: Agent display name.
        role: Role slug.
        mandate: Mandate text (single-paragraph summary).
        created_by: Provenance string recorded on the identity.

    Returns:
        JSON-serializable dict matching the hand-built reference layout.
    """
    return {
        "name": name,
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": "lightweight",
        "capauth_managed": False,
        "mandate": mandate,
        "created_by": created_by,
    }


def build_profile_document(name: str) -> dict:
    """Build the profile.yaml document (bridge-curation block).

    Matches the shape written by ``skcapstone agent profile --init`` so the
    Telegram bridge reads one consistent file format.

    Args:
        name: Agent slug.

    Returns:
        YAML-serializable dict.
    """
    return {
        "agent": name,
        "bridge": {
            "tools": "default",
            "voice_reply": "voice",
        },
        "_note": "Bridge-curation block read by telegram_bridge.py. "
        "tools: 'default' | 'all' | [explicit list]. "
        "Full manifest: `skcapstone agent profile --agent %s`." % name,
    }


def provision_lightweight_agent(
    name: str,
    role: str = ROLE_WORKER,
    home: Path | None = None,
    mandate: str | None = None,
    write_mandate: bool = True,
    created_by: str = "skcapstone init --non-interactive",
    force: bool = False,
) -> ProvisionResult:
    """Scaffold a lightweight fleet agent home without any prompts.

    Creates the agent home, ``identity/identity.json``, ``profile.yaml``, and
    optionally ``MANDATE.md``. Does not create PGP keys, memory layers, trust
    state, soul blueprints, or sync folders - see docs/LIGHTWEIGHT_AGENTS.md.

    Args:
        name: Agent name. Also slugified for the directory name.
        role: Role slug (``worker``, ``reviewer``, or a custom string).
        home: Explicit agent home. Defaults to
            ``<SHARED_ROOT>/agents/<slug>``.
        mandate: Custom mandate summary for identity.json and MANDATE.md.
            When omitted, a role template is used for MANDATE.md and a short
            default summary for identity.json.
        write_mandate: Write MANDATE.md (default True).
        created_by: Provenance string recorded in identity.json.
        force: Overwrite an existing lightweight profile. Without it,
            provisioning an existing agent raises FileExistsError.

    Returns:
        ProvisionResult listing every file written.

    Raises:
        ValueError: If the name produces an empty slug.
        FileExistsError: If the agent already exists and ``force`` is False.
    """
    slug = slugify_name(name)
    agent_home = Path(home).expanduser() if home is not None else default_agent_home(name)
    identity_path = agent_home / "identity" / IDENTITY_FILENAME

    if identity_path.exists() and not force:
        raise FileExistsError(
            f"Agent '{slug}' already has an identity at {identity_path}. "
            "Pass force=True (or --force) to overwrite."
        )

    mandate_text = mandate or f"Lightweight {role} agent in the SK fleet."

    agent_home.mkdir(parents=True, exist_ok=True)
    (agent_home / "identity").mkdir(parents=True, exist_ok=True)

    files: list[str] = []

    identity_doc = build_identity_document(name, role, mandate_text, created_by)
    identity_path.write_text(json.dumps(identity_doc, indent=2) + "\n", encoding="utf-8")
    files.append(str(identity_path))

    profile_path = agent_home / PROFILE_FILENAME
    profile_path.write_text(
        yaml.dump(build_profile_document(slug), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    files.append(str(profile_path))

    if write_mandate:
        mandate_path = agent_home / MANDATE_FILENAME
        if mandate:
            body = (
                f"# {name} - Lightweight {role} agent\n\n"
                f"Created {datetime.now(timezone.utc).strftime('%Y-%m-%d')} via "
                "`skcapstone init --non-interactive`.\n\n"
                f"## Mandate\n\n{mandate}\n"
            )
        else:
            body = mandate_template(name, role)
        mandate_path.write_text(body, encoding="utf-8")
        files.append(str(mandate_path))

    return ProvisionResult(
        agent=slug,
        home=str(agent_home),
        role=role,
        files=files,
    )
