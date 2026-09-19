### Added

- **`skcapstone fleet node drift --fleet`: do the estate's nodes agree with
  each OTHER?** `detect_drift` answers "is this node internally consistent"
  and structurally cannot answer this one: every expected value it compares
  against is read from the node's own checkout, so a host that never pulled
  agrees with itself perfectly, forever. On 2026-09-19 all five chi hosts did
  exactly that at once.

  The readiness gate now publishes `installed_git_sha` in its verdict (a
  dist-info glob, stdlib only, never affects the `ready` result), and the new
  `detect_fleet_incoherence` reads those verdicts. That file is already
  written every 15 minutes to the shared fleet tree and already read by
  `staged_rollout._readiness_verdict`, so there is no ssh, no new publishing
  step and no second reporting channel. Findings are ordinary `Drift` records
  with `artifact="fleet:installed_git_sha"`, so text output, `--json` and
  `--strict` all work unchanged. The JSON payload now carries `host` per
  finding, because a fleet finding is about a peer.

  Deliberately NOT built on `rollout_history`, the obvious-looking substrate:
  it is written only by `staged_rollout.record_deployment`, and this fleet's
  deployments do not all go through it. Measured on 2026-09-19, every node's
  recorded manifest said `0c8dcd6b` while every node's checkout was on
  `112b2ef4`. A check built on it answers "coherent" from records that agree
  only because they are equally out of date.

  Opt-in, and not folded into `detect_drift`: the staged rollout gates each
  node with `detect_drift`, and during a staged rollout nodes are supposed to
  disagree. Plurality rather than "compare everyone to me", so two operators
  reading the same tree reach the same conclusion; ties break
  deterministically. A node that cannot name its commit is reported, never
  counted as agreeing.
