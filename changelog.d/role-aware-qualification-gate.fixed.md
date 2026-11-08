### Fixed

- The qualification-sandbox checks (qualify-env prefix, AppArmor bwrap profile in enforce mode,
  Node 22) in `node drift`, `rollout_artifacts sandbox-profile`, `qualify_env build` and the
  readiness gate now skip a host only when `production_policy.can_receive_qualified_work`
  says no dispatch path can hand it qualified work: it is absent from `node_quotas`,
  `worker_destinations`, `node_admission`, `remote_review.destinations` and `authority_host`,
  matched exactly or by a loose spelling (a case-variant key is never read as absence), under
  the dispatch host resolution (`socket.gethostname()`) as well as `SKFLEET_NODE`.
- Role can only require the sandbox (`builder-standby`, `control`), never exempt from it. An
  unparseable or unrecognisable `production.json` fails closed. This lets a host that production
  policy never places work on (chiap10) pass the gate and be enrolled in
  `SKFLEET_AUTO_ROLLOUT_HOSTS`.
