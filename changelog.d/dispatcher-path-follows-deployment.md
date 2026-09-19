### Fixed

- **Three more sites named a per-host artifact's path by convention.** Each
  of these scripts exists at two paths on a live host, placed by two
  unrelated mechanisms: `~/.skenv/bin/<name>` as a side effect of
  `pip install -e .`, and `~/.local/bin/<name>` from the rollout's explicit
  copy step. The units execute the second one. Nothing keeps them equal.

  `skfleet-niobe-live.service` passed `--dispatcher %h/.skenv/bin/skfleet-rotate.py`,
  and `seat_cycle_entrypoint.py` derived the dispatcher twice as
  `Path(sys.executable).parent / "skfleet-rotate.py"`, which resolves to the
  same pip copy. All three now resolve through
  `deployment_manifest.deployed_artifact_path()`.

  The failure mode was worse than a missing file: the wrong path EXISTS, so
  the `is_file()` guard passed and a stale dispatcher would have run
  silently rather than failing closed.

- **A regression guard**, so a fourth site cannot land quietly: no unit in
  either systemd tree may name `~/.skenv/bin/<per-host artifact>`, and no
  source file naming a per-host artifact may derive a directory from
  `sys.executable`. The source check tokenizes before matching so it fires
  on code and not on the docstring that has to warn about the pattern, and
  carries a negative control proving it can still fail.
