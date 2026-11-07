- Docs only: migrate to the sk-standards AGENT_INSTRUCTIONS_STANDARD. `AGENTS.md` is now
  the single agent instructions file, reshaped into the eight standard sections (about 120
  lines), and the root `CLAUDE.md` is removed because Claude Code skips its native
  `AGENTS.md` loading when a `CLAUDE.md` sits next to it. The long worker and reviewer coord
  walkthrough moved to `docs/COORD_AGENT_GUIDE.md`, which `scripts/check-coord-write-guidance.py`
  now scans in place of the deleted `CLAUDE.md`. Stale facts dropped: per-machine worktree
  paths, the "max 500 lines per file" rule (not enforced), and a hardcoded co-author trailer.
