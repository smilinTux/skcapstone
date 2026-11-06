### Fixed

- `sk-cron-run` now declares the acting agent for cron jobs, which the systemd
  half of the estate already had. The agent is declared once per node in
  `~/.config/environment.d/10-skagent.conf`; the systemd user manager reads
  `environment.d` and hands those values to every user unit, but cron reads
  neither `environment.d` nor `.bashrc`. A crontab job therefore arrived with no
  selector at all, and `skmemory.agents.get_active_agent()` returns `None` in
  that state because with several agents installed it refuses to guess. A caller
  that reads `None` as "use the template" lands on `lumina-template`. Measured on
  noroc2027 2026-11-06: 21 wrapped crontab jobs, every one resolving to `None`.

  The values are derived from that one file rather than pasted into the crontab
  or the schedule env file, so a node that changes agent changes one file. The
  file is parsed literally (`KEY=VALUE`, `#` comments, no shell expansion), the
  same way the systemd generator reads it, because a value two readers expand
  differently is a value that drifts. The block is all-or-nothing: if any
  selector is already set the file is left untouched, since `get_active_agent()`
  requires every selector that IS set to agree and mixing one caller-supplied
  value with three derived ones manufactures exactly the conflict this prevents.
