Fleet: the wedge reaper now measures progress from the agent's own session
transcript instead of workspace file mtime, and its deadline drops 4h -> 2h.

Workspace mtime could not fire. It counted DIRECTORY mtimes, and an ordinary
`git status` bumps `.git` without writing a file, so on 2026-09-19 six chi
workers with zero edit/write tool calls, zero commits and zero dirty files
across 2.5h to 6h all reported `progress_age_s=11..26` and classified
`wedge-progressing`. No value of `DEFAULT_WEDGE_TIMEOUT_S` could have
reached, because the oracle never went stale. Tool caches (`.pytest_cache`,
`.ruff_cache`, `uv-cache`, `__pycache__`, `node_modules`) did the same for
any worker re-running its tests.

`_workspace_progress_at` now timestamps regular files only and skips those
cache directories; `.git` files are still counted, as before. A new
`_session_progress_at` reads the newest `~/.pi/agent/sessions/*/*.jsonl` for
the worker and is preferred when present, with the source recorded in the
`WORKER_PROGRESS` log line.

Only the transcript may actuate. A workspace-mtime reading is reported and
never acted on. Its gaps overlap the healthy population (productive sessions
were measured going up to 29,181s between file writes), and more decisively
an entire card class is required to write nothing: 2,601 of 6,901 cards
(37.7%), and 20 of the 41 in DOING, are labelled `source-only` with
acceptance criteria reading "Read-only audit. No edits, commit, push".
Arming a workspace-mtime deadline would have reaped about half of all active
work for complying with its own card. Total absence (no workspace and no
transcript) keeps the existing absent-reaper deadline.

`DEFAULT_WEDGE_TIMEOUT_S` 14400 -> 7200, re-derived over 2,754 fleet
sessions: the longest silence inside a session whose worker kept working was
2,558s, no session exceeded one hour, and the 139ec63d incident sat silent
22,680s. 7200 sits in that empty band, above the 3,600s largest bash tool
timeout any worker has ever issued, and kills zero working workers on replay.
