# Native Node test runner candidate

Card `c1a30112`, owner `codex-native-web-tests-c1a30112`, exact claim
`ff7ecd19724e43ed9e804df9883229c8`. Baseline
`e9ee44cd1ca43aebd0b947a0c6d7afb26d2ef143`. This is a tested source candidate
awaiting independent review and parent-controlled installation, not a claim
of production admission or canonical card completion.

## Change and acceptance evidence

The existing native test profile, plan, worker and receipt validator now support
a strict Node/Vitest variant through `production_test_node.py`. Fixed Vitest,
TypeScript and ESLint commands bind exact card criteria, candidate commit/tree,
runtime, package/configuration/lock and read-only dependency artifact bytes.
JUnit requires exact test-file membership, unique cases and positive per-file
minimum counts. Missing, malformed, skipped, failing or incomplete evidence
cannot pass. Existing Python recipes retain their behavior.

The source scaffold and dependencies are read-only; source mount symlinks are
rejected. Only TypeScript receives an initially empty build-info output mount.
Earlier tests cannot seed an incremental cache. The native bwrap, service
quotas and terminal-custody validation are reused. Failures while the executor
remains alive retain typed receipts; hard native termination retains observed
unit status and empty cgroup without inventing a receipt.

Private evidence root:
`~/.skcapstone/evidence/work/c1a30112/`.

- `regression-r2.log`: 171 tests passed in 28.32 seconds across profile,
  worker/receipt, lifecycle and Node suites. After restricting the scratch
  mount to TypeScript, `regression-r3.log`: all 81 affected worker and Node
  tests passed in 20.02 seconds. Profile and lifecycle source did not change.
- Ruff on all five runtime modules and both changed test files passed.
  `git diff --check` and changelog fragment validation passed.
- `positive-network/result.json`: actual isolated Vitest 3/3, TypeScript
  and ESLint exit zero. Tests prove source/dependency write denial, private
  scratch access, absent host credential/socket paths and denied access to
  an actual host-loopback TCP listener. Exact receipt validation passed.
- `failure-sealed/result.json`: actual failed Vitest assertion, raw failing
  JUnit, service exit 1, acceptance rejected.
- `missing-sealed/result.json`: actual pre-collection failure with missing
  JUnit, bounded log and typed missing-file receipt; acceptance rejected.
- `timeout-sealed/result.json`: interval-backed hung config killed by native
  eight-second runtime quota, status 15, no worker receipt, main PID zero and
  no remaining tasks; acceptance rejected from independent terminal custody.
- `drift-sealed/result.json`: qualified copied artifact deliberately changed
  after plan sealing. Service exit 1 and acceptance rejected for dependency
  drift before candidate execution; empty cgroup verified.

All final service trial results bind the five executed development module
hashes, checked equal before and after each trial. The trusted Python `-I`
bootstrap imports those exact development modules because production still
has the old installed runner. The underlying service, sandbox and validator
are the production implementations; these trials do not claim an installed
rollout. Each trial used 100% CPU, 1 GiB RAM and 128 tasks, within this node's
qualified limits. No dependency download, image, Docker socket or candidate
network access was added.

## Preserved diagnostics and operating impact

Earlier attempts remain preserved. The initial trial exposed missing
read-only dependency mountpoints; the next exposed TypeScript 6 rejecting
`--tsBuildInfoFile` with `-b`. Both root causes were corrected and rerun.
An early unresolved-promise fixture exited without JUnit and proves missing
output rejection only; the later interval-backed fixture proves timeout.
An observation helper briefly waited for a retained service before stopping
it; its exact completed invocation was recovered without restarting work.

`syncthing-permissions.log` proves actual directory-mode normalization in
the original synchronized artifact location. The fixed artifact root is now
node-local `~/.local/share/skcapstone/test-dependencies/`, mode `0700`, with
read-only artifact content. The original public synthetic artifact and failed
evidence remain preserved. No sync configuration was changed.

See `docs/fleet/qualified-test-profiles.md` for qualification and rollback.
The parent must independently review this exact candidate, preserve exact
preimages of the four existing runtime modules and qualify installation and
rollback before enabling any profile. Installation changes the runtime
fingerprint and invalidates unfinished old profiles; preserve those immutable
records and requalify only with actual fresh evidence. No install, push,
merge, live profile admission, claim release or database migration occurred.
Scanner card `a8300e02` still requires its own exact full-suite and M1/M2
mutation qualification. This runner candidate does not satisfy those criteria.
