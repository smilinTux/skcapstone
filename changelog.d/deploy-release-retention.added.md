- `skcapstone deploy` now prunes old releases. Every `deploy apply` built a new
  `~/.skenv-releases/<time>-<manifest>` and none was ever deleted: noroc2027 had 11
  after two weeks. After a successful apply it keeps the newest
  `SK_DEPLOY_KEEP_RELEASES` (default 3, `0` turns the automatic prune off), the
  release `~/.skenv` points at, the current and previous releases in the deploy
  state (the rollback target, `legacy-*` included), any release a running process
  still uses (exe, cwd, command line or a mapped `.so`, so an MCP server started
  before the deploy keeps its venv) and anything younger than an hour. A prune
  problem is a note, never a failed deploy. New `skcapstone deploy prune [--keep N]
  [--dry-run]` runs it by hand.
