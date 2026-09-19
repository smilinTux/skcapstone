### Added

- **`skcapstone fleet node drift --fleet`: do the estate's nodes agree with
  each OTHER?** `detect_drift` answers "is this node internally consistent"
  and structurally cannot answer this one: a node whose checkout, package and
  deployed artifacts all agree is perfectly self-consistent while being the
  only host in the estate on last week's commit. On 2026-09-19 all five chi
  hosts sat on a commit none of them had any way to notice was not the one
  being rolled out.

  Answered from the rollout history every node already publishes to its own
  node-scoped path under the shared fleet tree, so there is no ssh, no new
  publishing step and no second reporting channel: findings come back as
  ordinary `Drift` records with `artifact="fleet:git_sha"`, and `--json`,
  `--strict` and the text output all handle them unchanged. The JSON payload
  now carries `host` per finding, because a fleet finding is about a peer.

  Opt-in on purpose, and deliberately NOT folded into `detect_drift`: the
  staged rollout gates each node with `detect_drift`, and during a staged
  rollout the nodes are supposed to disagree, so folding it in would make
  every staged rollout fail at its second node.

  The rule is plurality, not "compare everyone to me", so two operators on
  two hosts reach the same conclusion about the same estate. Ties resolve
  deterministically. A node that has never recorded a deployment is not
  reported: never having deployed is a different fact from having deployed
  the wrong thing.
