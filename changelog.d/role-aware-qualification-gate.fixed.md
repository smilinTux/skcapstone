### Fixed

- The rollout drift gate no longer demands the qualification sandbox (qualify-env prefix,
  AppArmor bwrap profile in enforce mode, Node 22) of a host that carries `production.json`
  but is not a worker destination. A host now counts as one exactly as
  `production_policy.require_destination` decides: a `node_quotas` entry and, when
  `worker_destinations` is set, a place in it. Light nodes (chiap10) and workstations
  (chiwk12-wsl, where AppArmor is not available) can therefore pass the gate and be enrolled
  in `SKFLEET_AUTO_ROLLOUT_HOSTS`. `rollout_artifacts sandbox-profile` and `qualify_env build`
  skip on such a host. An unreadable or unrecognisable policy fails closed (still checked).
