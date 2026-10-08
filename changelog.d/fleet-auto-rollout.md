### Added

- `skfleet-auto-rollout.timer`: the authority host now deploys merged main to
  the fleet every 2 minutes on its own, using the existing gated
  `skcapstone fleet rollout --apply` per host, canary first. It takes a lock so
  two runs never race, refuses a deploy checkout that diverged from main, stops
  at the first host whose gate fails, and skmails `ROLLED` or `HALTED` to jarvis
  and lumina-nor. Before this, rollout ran from a polling loop in an operator
  chat session, and merged fixes sat undeployed whenever that loop ended.
