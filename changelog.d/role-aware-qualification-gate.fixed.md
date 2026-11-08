### Fixed

- The rollout drift gate (`node drift`) now applies the same role rule the readiness gate
  already used for the qualification sandbox (qualify-env prefix, AppArmor bwrap profile in
  enforce mode, Node 22): only a node whose admitted role runs qualified work
  (`builder-standby` or `control`) is graded on it. Before, any host carrying `production.json`
  was, so a light node with no admitted role (chiap10) failed the gate and could not join
  `SKFLEET_AUTO_ROLLOUT_HOSTS`.
- A qualifying role is not enough on its own: the host must also be an authorized worker
  destination in `production.json`, decided as `production_policy.require_destination` does.
  chiwk12-wsl holds a `builder-standby` spec but is not a destination and its WSL2 kernel has
  no AppArmor, so it can never run sealed qualification and is no longer graded on it. The
  readiness gate applies the same destination check.
- `rollout_artifacts sandbox-profile` and `qualify_env build` skip on such a host. An
  unreadable role or policy fails closed (still checked).
