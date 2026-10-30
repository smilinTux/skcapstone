### Changed

- `skcapstone register` and the install wizard wire session hooks through `skmemory hooks install` for Claude Code, Codex, OpenCode and Pi; the Codex/Pi context loader calls `skmemory hook session-start --harness generic`.
