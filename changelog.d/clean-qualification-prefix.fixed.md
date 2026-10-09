Sealed fleet test execution now runs from a dedicated clean prefix
(`~/.local/share/skcapstone/qualify-env`) built by
`python -m skcapstone.fleet.qualify_env build` from a committed hashed lock
(sklegal's locked third-party set plus skcapstone's own dependencies), instead
of each host's production `~/.skenv`. The prefix rejects editable `.pth` files
and ambient site-packages, its build record is bound into the runtime and
toolchain fingerprints so remote builders can reach fingerprint parity, the
staged rollout builds and verifies it after the package install, and profiles
qualified on the exact previous `~/.skenv` toolchain get one native
requalification instead of being stranded.
