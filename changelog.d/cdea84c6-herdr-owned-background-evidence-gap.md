- Card `cdea84c6`: record Herdr's current owned-background evidence gap from
  `herdr agent list`, `herdr pane process-info`, `herdr api snapshot`, and the
  0.9.0 docs: the CLI and API publish pane foreground processes but no stable
  pane-owned background-task identity, so skcapstone fleet evidence stays
  fail-closed on exact name+cwd card joins; PR #695 remains the required
  upstream blocker (card ffc1229d), and `skfleet-working.py` keeps failing
  closed until Herdr exposes exact owned-background work (card/worktree or
  equivalent) in agent list or process-info.
