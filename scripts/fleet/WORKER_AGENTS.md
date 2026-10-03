# SK stack card worktrees: shared agent rules

This file is the shared guidance layer for every AI coding agent working a
coordination card in a worktree under `/srv/sklegal-fast/work/`. Pi, Codex,
and other tools load it automatically from this parent directory. A
repository's own AGENTS.md at the worktree root layers on top and is more
specific: when they disagree, the repository file and your exact card
contract win.

## Identity and bootstrap

- Run `"${CODEX_HOME:-$HOME/.codex}/bin/load-sk-agent-context.sh"` and
  `skcapstone coord status` before any card work.
- Run every `skcapstone coord` command with your explicit
  `--agent <identity>` matching your claim. Never fall back silently to a
  default operator identity.
- Your agent profile directory under `~/.skcapstone/agents/<identity>/` must
  exist before you launch, or the launcher will rebind you to a default.

## Board discipline

- Work only your exact claimed card. Do not broaden scope, claim siblings,
  or complete another agent's card. One writer per card.
- All task writes go through `skcapstone coord`. Never create, append,
  rewrite, or delete CardStore JSONL directly.
- Read the board through targeted commands (`coord show <id>`,
  `coord gates <id>`, `coord briefing`), not broad dumps that burn context.
- Stale or failing work: preserve the worktree, record what happened, and
  release or hand back the claim through the CLI. Never destroy uncertain
  work.

## Worktree discipline

- Never push, merge to main, deploy, restart services, or mutate live
  gateways, registries, nodes, or systemd units from a card worktree.
  Installation and operations are separate cards with their own gates.
- Keep credentials and secrets out of tracked files, logs, prompts, cards,
  evidence, and model calls. Secret loading stays in the existing boundary.
- Public synthetic content only for external providers unless your card and
  policy explicitly authorize protected-data egress.
- Never use em dash or en dash characters in code, comments, documentation,
  or generated artifacts.

## Cards quick reference

One source of truth for depth: `skcapstone coord briefing` and
`~/.skcapstone/docs/AGENT_COORDINATION.md`. The essentials:

- Query narrowly: `skcapstone coord show <id>` (add `--json` for links),
  `skcapstone coord gates <id>` for admission, `coord board` only for
  overviews. Never dump the whole board into your context.
- Create: `skcapstone coord create --title "[PREFIX][S|M|L] Title" --criteria
  "..."`. Card IDs are assigned by SKCapstone and never invented. Link the
  repository and assigned TDD before working.
- Lifecycle: `coord claim <id> --agent <you>` -> `coord move <id> doing` ->
  work -> `coord verdict <id> PASS_FOR_REVIEW --agent <you> --candidate
  <shared evidence path> --commit <sha> --tree <sha> --ref refs/heads/<branch>`
  -> `coord move <id> review`. Record the verdict last, then read it back
  through the CLI before believing it: these tools can print success while
  storing nothing.
- Release stale or failing claims only through `coord release-claim` with the
  exact owner and claim revision. Preserve uncertain worktrees.

## Talking to other agents

- The board carries work; skmail carries coordination about work (blockers,
  handoffs, file locks). `skmail read <me>`, `skmail ack <me>`, `skmail send
  <from> <to> <priority> <re> "..."`. Check before claiming, after finishing
  or releasing, and when blocked.
- `skcapstone chat` is a P2P retry transport: it reports success while
  queueing locally. Verify the recipient actually received before relying on
  it. Use your session's configured MCP servers (skcapstone, skcomms,
  skmemory) for mediated calls instead of raw transports.
- In shared repos: work only in your own worktree branched from `origin/main`,
  never `git stash` (one shared stash ref per repository), and leave other
  sessions' uncommitted files alone.

## Completion evidence

A card is complete only with durable evidence recording: files changed,
tests with exact commands and counts, acceptance criteria evidence, known
limitations, rollback or migration notes when data changes, and the linked
card update (`coord link ... evidence`, verdict, then move to review).
Helper completion never completes a parent card.
