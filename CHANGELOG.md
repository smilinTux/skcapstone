# Changelog

All notable changes to **skcapstone** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Added

- **Card ccbe1a37: add the read-only `coord portfolio-plan` shadow CLI.**
  The command accepts one frozen, typed JSON input on stdin or from a file and
  emits the exact deterministic SKCoord proposal. Expected abstention succeeds
  by default, while `--strict` exits nonzero for automation. It has no board,
  model, queue, claim, or mutation path.

- **Card 90b5b277 (epic c880017b): `sknoded` grows a read-only operator-plane
  HTTP surface, P1 of the `skoperator.remote/v1` migration
  (`docs/OPERATOR_PLANE_REMOTE_STANDARD.md`, PR #179).** `sknoded` now serves
  `/operator/v1/{healthz,readyz,apps,apps/{app}/explain,apps/{app}/observe,
  observe (SSE watch),estate,apps/{app}/act (reserved)}` on port 9392,
  tailnet-bind only, never `0.0.0.0`/`::`. Gated OFF by default behind
  `SKOPERATOR_HTTP` (unset, a node behaves byte-identically to before this
  surface existed, and `sknoded.main_loop` never imports the new module's
  heavy dependencies); a one-shot `--once` report pass never starts it
  regardless of the gate. Auth is capauth end to end: a detached PGP request
  signature (`X-SK-Fingerprint`/`X-SK-Timestamp`/`X-SK-Nonce`/`X-SK-Signature`
  over `method\npath\nsha256(body)\ntimestamp\nnonce`, pinned to the claimed
  fingerprint's OWN key, never "any trusted key") establishes identity, then
  `capauth.authz.decide()` (the shipped PDP, not a new scheme) checks
  `operator.observe` / `operator.act` / `operator.estate.read` -- observe
  never implies act. The observe tree is GET-only and freeze-independent by
  construction: `apps`/`explain`/`observe`/`estate` never read
  `store.is_frozen`, reusing `operator_seat.eyes`'s already freeze-proof
  cli/seat lanes rather than `operator_seat.loop`'s freeze-gated pass, so
  `skcapstone atlas eyes` keeps working exactly as it does today while
  frozen. Failure taxonomy stays three distinct, never-healthy families
  (`Unreachable`/`Unknown`/`Unauthorized`, `REASON_FAMILY` asserted
  exhaustive by tests): a genuine cli/seat lane disagreement renders that
  condition `Unknown (LaneConflict)` rather than picking a winner. `act`
  exists only as a reserved, always-`501` path (checks freeze server-side on
  every call, gated on the separate `operator.act` scope) -- no actuation
  ships in this card. Registered port 9392 in `docs/PORTS.md` and
  `FLEET_RESERVED_PORTS`. `fleet/signing.py` gained `roster_by_fingerprint()`
  and `own_fingerprint()` (additive) so a claimed identity can be pinned to
  its own key rather than "any roster key verifies".

### Fixed

- **Card 90b5b277: skdashboard, fleet, and skbrain made observable (ATLAS
  Eyes was showing 1 BLIND app + 2 structurally invisible ones).**
  - `skdashboard` was registered as an Operatorapp with a working, tested
    adapter (`operator_seat/skdashboard_adapter.py`) that was simply never
    added to `loop.ADAPTERS`, so an unfrozen ATLAS never actually observed it
    (seat: `NO ADAPTER`) -- and its declared cli contract was dead
    (`skcapstone dashboard operator` exited 2, "unexpected extra argument":
    `dashboard` was a plain command with no subcommands). Both are fixed: the
    adapter is wired into `loop.ADAPTERS`, and `skcapstone dashboard` is now a
    group (`invoke_without_command=True`, same pattern as `capabilities`/
    `usage`) exposing `dashboard operator explain|observe|act`, mirroring
    `cmdb operator`. `skdashboard` now reads OK in both lanes.
  - `fleet` (`fleet_adapter.fleet_observe`) has been a `loop.ADAPTERS` builtin
    since the first cut of the operator seat but was deliberately excluded
    from `registration.APP_REGISTRY` ("the reference the apps plug into, not
    an Operatorapp"), which meant the discovery path -- anything that lists or
    ratifies apps via the fleet store rather than the in-process loop -- could
    not see it at all. Decision: register it. `cli` is deliberately `None`
    (not a made-up command): fleet is not a separate daemon with its own CLI,
    and inventing one that does not resolve would just trade "no
    registration" for "cli-error" -- a confident-but-wrong reading. An
    out-of-process transport for the fleet reference is tracked by the
    remote-operator-plane epic (`c880017b`) and its HTTP surface
    (`fleet/sknoded.py`), out of scope here.
  - `skbrain` (a shell module, `enabled: false` in `shell/modules.json`, no
    Operatorapp) is not given a fabricated adapter -- it is not installed in
    this environment, so any probe would either invent a confident value or
    manufacture a new BLIND row, both of which this pass exists to prevent.
    Instead, "disabled on purpose" is now made legible as its own state,
    distinct from "invisible / no observation path exists": `eyes.py` used to
    fold both into one "BLIND EVEN IF UNFROZEN" bucket, distinguished only by
    an inline `(enabled)`/`(disabled)` word. A disabled module no longer
    appears there at all; it gets its own `disabled_modules` assessment key
    and a separate, non-alarming "DISABLED (off by choice, not a blind spot)"
    report section (`eyes.disabled_module_notes`). An *enabled*-but-
    unregistered module (a real gap) still surfaces under "BLIND EVEN IF
    UNFROZEN" as before.
  - Net result: `skcapstone atlas eyes` now reports zero BLIND rows (down
    from 1), zero CONFLICT rows, and zero lane-conflict (`!=`) lines, with
    `fleet` newly enumerable via `skoperator apps list`/`apps ratify`.
- `skcapstone coord claim --force` now retains its compatibility spelling but
  cannot bypass incomplete, unknown, review, or human dependency gates. The
  CLI returns a concise nonzero refusal listing every blocking ID (card
  54cd56f2).

- **Card 504d0046: the 10 lying ATLAS Eyes lane conflicts, root-caused and
  fixed; `atlas eyes` now reports zero CONFLICT rows.** ATLAS Eyes' first real
  run (PR #178) found the cli lane and the in-process seat lane disagreeing on
  10 conditions across 5 apps. Root causes, all fixed at the source (never by
  hiding a disagreement or inventing a confident default):
  - `skcomms_adapter._default_probe` / `skmemory_adapter._default_probe`
    shelled out to `<app> daemon status`, a subcommand that no longer exists
    on either CLI (exit 2), which read as confidently WRONG health; and the
    skcomms probe hardcoded `queue_depth: 0`, so `QueueDrained` could never see
    a real backlog. Both now delegate to the app's own
    `<app>.operator_probe.observe()` / `queue_depth()` (the exact module the
    `<app> operator observe` cli lane runs): one real signal, two callers.
  - `skos_adapter._default_probe`'s `SchedulerAlive` shelled out to
    `skos scheduler status` (also nonexistent, exit 2); `GtdSinkDraining` was
    hardcoded `None` (never implemented) though the real quarantine-backlog
    signal was available the whole time. Both now delegate to
    `skos.operator_probe.observe()`.
  - `skchat.operator_probe` (skchat repo) and `skharness.operator_cli` (the
    skcode-hostd operator facet) each collapsed a genuinely-unknown signal
    (`AuthEnforced` with neither the daemon nor the env reporting anything;
    `HostdReady`/`SessionsHealthy`/`RegistryConsistent`/`AuthEnforced` when
    hostd was totally unreachable) into a confident `True`, while Atlas's
    in-process seat adapters already read Unknown for the same nodes. Fixed
    upstream in those repos (skchat PR #181, skharness PR #59) to render
    Unknown instead of inventing health.
- **Lane conflicts are now a hard failure, not just a printed line.** Added
  `eyes.LaneConflict` and `eyes.assert_no_conflicts(assessment)`, and wired
  `skcapstone atlas eyes --strict` to raise it (non-zero exit, after still
  printing the report) when any app has a lane conflict. Per PR #179's design
  ("exactly one authoritative producer per condition; two authoritative
  readings = hard LaneConflict rather than a silent preference"), this makes
  the P0 gate ("eyes CONFLICT=0") script-checkable instead of relying on a
  human reading the report carefully enough to notice a `!=` line.

### Added

- Added `skcapstone atlas eyes` (`operator_seat/eyes.py` + `cli/atlas_cmd.py`):
  a read-only, freeze-proof estate assessor. In one pass it observes every
  registered Operatorapp through TWO lanes - the declared `<spec.cli> observe`
  contract out-of-process (hard per-app timeout) and the in-process
  `loop.ADAPTERS` code an unfrozen ATLAS would run - and renders per-app
  verdicts (OK / FIRING / CONFLICT / UNKNOWN / BLIND). Unknown is a first-class
  result distinct from unreachable (`no-cli` / `cli-error` / `timeout` /
  `unparseable` / `no-adapter`), declared-but-unreported conditions surface as
  Unknown (absent), and lane disagreements are flagged per condition. The pass
  correlates open ITIL incidents/problems to apps, surfaces the pending-change
  backlog, unmigrated legacy flat ITIL files, unregistered shell modules, and
  ends with a plain "blind even if unfrozen" list. It never calls `act`, never
  touches the freeze, and writes nothing.

### Fixed

- Fixed card `0e98a570` (critical, blocked lifting the ATLAS freeze): a
  genuinely later real-world recurrence of a condition that had already run
  its lifecycle to a terminal ledger state (VERIFIED / ROLLED_BACK /
  ESCALATED) previously died with `invalid action transition: <terminal> ->
  authorized` and never actuated again, because
  `action_ledger.stable_intent_id()` derives identity from nine governance
  fields with no time or attempt component, so two separate real occurrences
  of the same condition on the same target hashed to the same `intent_id`.
  `ActionIntent` now carries an `occurrence` field (omitted from the identity
  hash, and therefore byte-identical for occurrence 0, when it is unset), and
  `ActionLedger.resolve_occurrence()` derives it purely from durable on-disk
  lineage state: it reuses occurrence 0 while that lineage is still open
  (dedup holds - a condition observed repeatedly within one still-open
  episode never gets a second intent), and advances to the next occurrence
  only once every prior one has reached a terminal state. `loop.py` memoizes
  the resolved occurrence for the lifetime of one operator pass, so two
  identical proposals within the SAME pass still collapse onto one intent
  even if the first finishes (e.g. VERIFIED) before the second is examined -
  only a genuinely later pass is allowed to see the prior terminal state and
  mint a new occurrence. Accepted failure mode: a condition that flaps
  faster than the existing cooldown/circuit-breaker window will now create
  one ledger lineage per flap (previously it silently stopped after the
  first terminal state); it still cannot re-actuate faster than that
  cooldown, because `safety.ExecutionState`'s fingerprint-keyed eligibility
  check is untouched by this change and still gates every physical
  actuation attempt independently of the ledger.
- Fixed the companion correlation loss on the `--honor` path (same card,
  defect 2): `act_dispatch.build_apply_fn` auto-creates its own ITIL change
  whenever proposer-supplied `change_id` is absent, but that id was created
  after the `ActionIntent` was already frozen and so was never recorded
  anywhere the ledger could show it. The frozen intent's `itil_change_id`
  field stays proposer-scoped by design (rewriting it post-freeze would
  silently change what identity it hashes to), but `loop.py` now records the
  auto-created change id on the durable, append-only `VERIFIED` (success) or
  `FAILED`/`ESCALATED` (failure) event's `detail`, and `build_apply_fn`
  stamps it onto any exception it raises so the correlation survives a
  failed attempt too. Combined with the recurrence fix above, the circuit
  breaker's `retry_budget` (previously unreachable on the production
  `--honor` path for any standard action with no `rollback_plan`, because
  one failure already terminated the ledger lineage before a second retry
  could occur) is now genuinely reachable: 3 consecutive real failures are
  each a distinct occurrence/intent, and only the 4th attempt is refused,
  by `safety.ExecutionState`'s circuit breaker, not the ledger.
- Rebased the ATLAS P3.2 fault-injection drill's `scenario 11` and
  `scenario 7` onto this fix (both previously asserted the pre-fix gap
  behaviour) and added two scenarios: same-episode duplicate observations
  still dedupe onto one intent even when the first's lineage completes
  terminal mid-pass, and `retry_budget` is genuinely reachable on the
  `--honor` path with a lifecycle ledger attached.

### Documentation

- Added the remote operator-plane transport standard
  (`docs/OPERATOR_PLANE_REMOTE_STANDARD.md`) and its no-flag-day migration
  plan (`docs/OPERATOR_PLANE_MIGRATION.md`): HTTP/1.1+JSON over Tailscale
  served by a per-node sknoded operator agent (port 9392) with SSE watch and
  cursor/relist semantics, capauth request signing with separate
  `operator.observe` / `operator.act` scopes, a three-way
  unreachable/unknown/unauthorized failure taxonomy that never renders as
  healthy, a single-authoritative-lane rule resolving the Eyes lane
  disagreements (card 504d0046), skos as a read-only estate consumer, and a
  disk-SEV2-gated sequencing for moving skgateway to .100. Design only; no
  running service changed.
- Recorded the GitHub-first, two-node CMDB package deployment and verification
  procedure used by card `3799733b`, including the dashboard restart required
  after updating the in-process `skcoord`/`skdashboard` dependencies.

### Added

- Added the ATLAS P3.2 fault-injection drill harness (card `b993eaaa`, epic
  `fb3cc09d`): `skcapstone.operator_seat.fault_injection_drill` assembles the
  already-merged safety mechanisms (`act_dispatch.py`'s injectable actuation
  seam, `safety.ExecutionState` cooldown/circuit-breaker, `loop.py`'s
  `performed=False` and post-action verification gates, typed rollback +
  escalation, and the signed `action_ledger`) into one real end-to-end run
  against an isolated, guarded fleet root, run via
  `scripts/atlas_p32_fault_injection_drill.py`. The root guard delegates to
  `skcapstone.fleet.drill.resolve_drill_root` (never SKFLEET_ROOT-trusting)
  so the drill hard-refuses to run against production. 17 scenarios cover the
  documented mechanisms plus gaps the existing unit tests don't reach:
  duplicate observations, stale evidence, key revocation, a mid-run freeze
  race, scheduler overlap, and (the most significant finding) that
  `action_ledger.stable_intent_id()` has no time component, so once a
  standing condition's ledger lineage reaches a terminal state
  (VERIFIED/ROLLED_BACK/ESCALATED) every later, separate real-world
  recurrence of that exact condition fails closed with an "invalid action
  transition" ledger error instead of a genuine re-attempt.
- Added priority-based context-window budgeting to
  `SystemPromptBuilder._build_prompt`: when the assembled system prompt would
  exceed the token budget, soul, identity, warmth, behavioral rules, and the
  most-recent peer history are preserved verbatim while the trimmable middle
  (gathered memories/journal context and recent snapshots) is shortened or
  dropped lowest-priority-first, replacing the previous blind tail-truncation
  that could cut off protected sections first.
- Added `validate_seed_for_store()` to `skcapstone cli skseed`, wiring a
  raising guard (schema validation plus store-level size/type limits on
  summary length, key-claim count, and tag type) into the `ingest_document`
  store flow before a seed reaches `MemoryStore.snapshot`, so malformed or
  oversized seeds are rejected with a clear error instead of being silently
  stored.
- Added a bounded Syncthing guard for scheduled fleet reconciliation. It can
  verify service state and CMDB coverage, attempt an approved service restart,
  and retain checksummed evidence without exposing API credentials.

- Added an opt-in, loopback-only Windows browser proxy through each
  workstation's canonical WSL Tailscale identity. The installer provides
  shared enable and disable desktop controls, preserves per-profile browser
  settings for rollback, and denies resolved destinations outside the
  Tailscale overlay ranges.
- Added read-only `skfleet node endpoint-audit` reconciliation between
  canonical fleet node identities and live or captured Tailscale status. It
  flags duplicate, stale, mismatched, offline, and ambiguous registrations,
  names exact retirement candidates, and only marks routing safe when one
  active peer matches the declared endpoint or every active Windows and
  Linux/WSL runtime has a unique role-scoped endpoint with matching OS
  evidence.
- Added fail-closed per-node Tailscale policy checks for allowed peer operating
  systems and maximum active peer count, including WSL-only workstation
  enforcement.

- Added a single-fire CMDB reconciliation job pinned to `chiap04` and
  `jarvis`. The bundled scheduler tick remains inert until reviewed JSON
  configuration enables it with exact targets and `skvault://` references.
  Enabled runs use bounded collection, configurable retries and stale grace,
  safe positive reconciliation, retained checksummed summaries, and
  deduplicated CI-linked ITIL escalation.

- Added explicit `skcapstone cmdb plan`, `cmdb apply`, and `cmdb status`
  operations. Apply validates the evidence batch before writes and delays
  retirement lifecycle updates until validation succeeds; the existing
  `cmdb reconcile` command remains compatible with deployed timers.

### Fixed

- Fixed `cmdb reconcile --local`/`--host` silently dropping `--record-run`
  and `--apply` evidence: `write_run_artifact()` was only ever wired into
  the `--network` branch, so the 3-hourly `skcapstone-cmdb-reconcile.service`
  timer (`--local --apply`) was mutating the live CMDB with zero auditable
  artifact. The local/host branch now builds the same checksummed run
  envelope (scan id, timing, scope, reconcile report) as the network branch
  and persists it via the existing `orch.write_run_artifact()`, with
  `completeness.complete` only true for an actual `--apply` so a
  `--record-run`-only dry run cannot be mistaken for a fresh apply by
  `cmdb status`'s freshness SLO.
- Updated the release gate and runtime dependency to require the Syncthing
  discovery contract published by skcoord 0.1.32.

- Reject blank legacy `MemoryEntry.memory_id` values at load, save, index,
  verification, and both promotion boundaries. This prevents the SKCapstone
  verifier/promoter from recreating unsafe `.json` files after SKMemory has
  quarantined them. Canonical unified SKMemory records (`id`, not
  `memory_id`) are now routed away from the legacy loader at debug level, so a
  promotion sweep neither mutates them nor emits one warning per valid record.
- Restored the enforced Black/Ruff gate after the new coordination-amendment
  and qualification VCS-audit modules landed with formatting drift.
- Updated the legacy CMDB seed tests to the schema-driven discovery contract
  shipped by current `skcoord`, preventing dependency upgrades from breaking
  the otherwise clean unit-test gate.
- Warm the Ollama model selected by the validated consciousness configuration
  instead of a hard-coded default, and query canonical PyPI distribution names
  (`skcomms`, `skchat-sovereign`, and `cloud9-protocol`) during version checks.
- Added a fail-closed systemd credential-file path for protected CapAuth
  signing keys. ATLAS can now sign noninteractively without exposing its
  passphrase in a unit environment or command line; symlinked, foreign-owned,
  overlarge, and group/world-accessible credential files are rejected.
- Restored the `main` push trigger for `publish.yml`, making its existing
  GitHub-owned patch-tag job reachable and bringing the release workflow back
  into agreement with this SOP. The main run publishes the version it tags;
  manually pushed release tags build and publish directly.
- Cleared the repository-wide Ruff gate by normalizing legacy test names and
  imports, removing unused test imports, and replacing an ambiguous local name;
  these are test-only cleanups with no runtime behavior change.
- Restored the packaged `skcapstone-cmdb-reconcile-network.service` contract
  used by ATLAS for governed credentialed CMDB applies. The unit fails closed
  unless the owner-reviewed exact-target/SKVault launcher exists; the legacy
  local-only reconcile service is no longer misdocumented as the ATLAS target.

### Added

- Added `skcapstone init --non-interactive --name X --role ROLE`, a scriptable
  provisioning path for lightweight fleet role agents (workers, reviewers). It
  scaffolds `<home>/agents/<slug>/` with `identity/identity.json`,
  `profile.yaml`, and an optional `MANDATE.md` role template - no prompts, no
  PGP/capauth, memory, trust, soul, security, or sync pillars. The
  lightweight-vs-sovereign capability delta and the upgrade path are documented
  in `docs/LIGHTWEIGHT_AGENTS.md`.
- Extended Atlas's skcode operator adapter with authenticated SKHarness activity
  replay/live-stream discovery and expiring idempotent steering commands plus receipt
  lookup. Monitor and control scopes remain separate, and queued commands are never
  presented as applied work. Replay filters now include job/card/contract/lease IDs so
  Atlas can preserve the controller-owned cross-agent lineage back to an immutable card,
  signed contract, source commit, attempt, and evidence rather than treating a display
  name as identity or authority.
- Added the ChatGPT/Codex SK client deployment runbook for Linux and Windows
  with WSL2, including MCP registration, current Codex skill paths, global
  Jarvis/soul bootstrap, SKWhisper, acceptance, safe restart, and rollback.
- Added `skcapstone dashboard --host ADDRESS` and propagated the selected bind
  address to SKDashboard. The default remains `127.0.0.1`; the SOP documents
  deliberate tailnet or all-interface exposure.
- Added `skcapstone qualify` commands for exact source checkpoints, independent
  review dispositions, evidence-only completion inventories, durable
  content-addressed review artifacts, and split exact-Git dependency audits that
  preserve hash enforcement for registry packages.
- Added `coord reconcile-agents`, a read-only-by-default audit with explicit,
  serialized repair receipts for card lifecycle versus mutable agent claims.

### Changed

- Raised the `skcoord` runtime floor to 0.1.18, delegated acceptance-criteria
  reads to the authoritative `CardStore.fold`, and added a registry-only CI
  gate that enforces the `skcoord`-first release order.
- `coord move` and the matching MCP tool now use `skcoord` lifecycle transitions so
  Review stops active execution while preserving accountable ownership, Done clears
  live claims and records history, and reopen transitions remove stale completion
  state before returning.

## [0.15.17] - 2026-08-20

### Added

- Added Pi coding-agent harness integration: the shared agent picker now wraps `pi`,
  registration installs Pi's managed `AGENTS.md`/context loader, and Pi receives the
  default `skcapstone` and `skmemory` MCP servers from `~/.skenv/bin`.
- Added `skcapstone cmdb retire` for append-only, idempotent CI retirement by explicit
  ID or discovery orphan set. Retirement preserves attributes, relationships, and
  history instead of deleting records.

### Fixed

- Codex bootstrap now exports the shared SK environment and enables the profile's
  unrestricted (`SK_CODEX_YOLO=1`) mode by default.
- Agent resolution no longer silently chooses a named or alphabetically-first identity
  when several profiles are installed. Explicit environment selection wins; otherwise
  only a sole installed agent is an acceptable fallback.

### Verification

- `pytest tests/test_doctor.py tests/test_multi_agent.py tests/test_register_paths.py tests/test_register_plugins.py -q`
- `pytest tests/test_cli_cmdb.py -q`
- `ruff check` on every changed Python source and test file; `bash -n` on the picker.

### Added
- **`skcapstone cmdb`: a CLI over the CMDB.** The CMDB held 48 CIs and had only
  a dashboard surface, so the only way to see or populate assets was the web
  UI. It also left the skbrain pack shipping
  `cronjob-skbrain-cmdb-reconcile.json`, whose command is
  `sk-cron-run skcapstone cmdb reconcile` -- a verb that did not exist, so the
  job has never run.

  Six verbs: `list`, `show`, `scan`, `reconcile`, `drift`, `impact`, each read
  verb taking `--json` so this composes into the operator and Atlas paths
  instead of only printing for a human. `scan` and `reconcile` are read-only
  unless `--apply` is passed. Observation is opt-in per host (`--local`,
  `--host NAME[=ssh-target]`, repeatable) and `scan` says out loud when no
  runner was given, because a scan that quietly read only specs looks identical
  to a scan that found a clean fleet.

  Requires the `skcoord.discovery` collectors; an older skcoord gets a message
  naming the package to upgrade rather than a bare `ImportError`.
### Security
- **The .100 smoke probe certified cloud-served models as sovereign, because it
  matched a model NAME** (card `16af7915`). `dot100-inference-smoke.sh` carried
  `SOVEREIGN_MODELS="ornith qwen llama mxbai beellama"` and matched it as a
  SUBSTRING against the `model` field of the gateway's response body. Measured
  against the live gateway ledger (`skgateway/data/metrics.db`, `energy_log`,
  opened read-only) on 2026-08-17: **76 rows** carry one of those tokens while
  running on `backend=nvidia`, `basis=imputed_cloud`, including
  `meta/llama-3.3-70b-instruct`, `nvidia/llama-3.3-nemotron-super-49b-v1` and
  `qwen3.8-27b-huihui-abliterated-q4_k_m`. Reproduced end to end against a stub
  gateway: the old probe printed
  `PASS gateway-sovereignty  sk-default served by meta/llama-3.3-70b-instruct`
  for a call whose serving backend was `nvidia`. The probe existed to catch
  silent cloud failover and it was the thing announcing the failover as healthy.
- **The probe now reads WHO SERVED, from skgateway's own attribution headers**
  (`x-sk-backend`, `x-sk-energy-basis`, `x-sk-energy-node`), which the gateway
  already emits for the serving attempt. Sovereignty is a claim about hardware
  and jurisdiction, so the discriminator is the backend plus the energy basis,
  never the model name: `ornith-1.0-9b` served by `nvidia` is a violation and
  the same weights served by `reg:ornith` are not. The weights are not the
  variable.
- **One definition, called rather than mirrored.** The rule lives in skharness
  (`skharness/autocode/sovereignty.py`) and this script shells out to
  `python3 -m skharness.autocode.sovereignty`. A bash copy of the rule would
  become a second definition the moment either side was edited, with nothing to
  report the drift; calling across the seam costs one subprocess and makes
  drift impossible. Requires the matching skharness change to be installed.
- **Fails closed, in three distinct states.** `sovereign` passes; `violated`
  fails naming the backend; `unobserved` (a gateway that emits no attribution)
  ALSO fails, because unknown is not sovereign and reading "nothing" as a pass
  is what made the old allowlist look healthy. A classifier that will not run at
  all is reported as "cannot classify" rather than as a violation: the probe
  branches on the state word and cross-checks the exit code, since
  `python3 -m some.missing.module` also exits 1 and an exit-code-only reader
  would point an operator at the routing when the real problem is the install.


### Fixed
- **`admit --preset` silently applied nothing on the GPU node.** `PRESETS` was keyed
  `node-100`, but `paths.self_node_name()` derives from the hostname and produces
  `node-ollama`, so the lookup missed and the box got no labels, no role and no
  taint while the command exited 0. This is the SAME defect that was fixed for the
  control node (`node-158` to `node-noroc2027`) and it survived that fix because
  only one of the two address-style keys was rekeyed. Rekeyed to `node-ollama`,
  with `node-100` kept as an alias so a runbook that says
  `admit node-100 --preset` still does what it reads like it does.
  Pinned by tests rather than by care: every live node name must resolve a preset,
  every legacy spelling must resolve to the same object, and every canonical key
  must be a name a node can actually have. That last invariant was stated wrongly
  on the first attempt (it flagged any key ending in digits, which wrongly
  condemns `node-41`, a real hostname-derived name); the defect was never "looks
  like an address" but "matches no live node", so the test asserts membership.
  Negative-controlled: 3 of 8 fail against the old table.

- **Concurrent joule settlements no longer lose updates** (card `b2bd1cad`).
  `JouleEngine.record_work` read the balance (`JouleWallet.__init__` loading the
  snapshot), added to it and wrote it back, with nothing serialising that
  sequence. Two writers therefore captured the same balance and the second write
  erased the first. This is measured, not theoretical: the live `lumina` wallet
  lost a 25 J credit and a 50 J credit exactly this way, both of them
  `auto_tokenize_task` entries (`[<card_id>] Task completed: <title>`).
  `record_work` now holds a cross-process `flock` across the WHOLE
  read-modify-write, wallet acquisition included, and reloads the snapshot inside
  the lock so a cached balance written by somebody else since construction cannot
  be carried into the mutation.

  The lock file name and its resolution are a cross-repo contract, not a local
  detail. skharness settles against the same wallet files from its own process,
  and two processes holding two DIFFERENT locks over one wallet protect nothing,
  so `SETTLE_LOCK_NAME`, `SETTLE_LOCK_TIMEOUT` and `_settle_lock_path()` mirror
  `skharness.autocode.joules` exactly. skharness is deliberately not a dependency
  here (the arrow runs the other way), so the constants are mirrored rather than
  imported and `tests/test_skjoule_settle_lock.py` asserts both that the two
  resolved paths are byte-identical and that the two locks actually exclude each
  other in both directions. A drift in either repo now fails a test instead of
  silently un-protecting the wallet.

  Evidence: the race tests are deterministic, not opportunistic. Each writer is
  held at a rendezvous placed immediately after it has read the balance and
  before it writes, so neither can write until both have read. Against
  unmodified `origin/main` the thread race lost an update in 5 of 5 runs and the
  two-process race lost one in 5 of 5 runs (balance 125 where 150 was owed); on
  this branch, 0 of 5 and 0 of 5. A positive control covers the other half of the
  argument, since a lock that turned every settlement after the first into a
  silent no-op would pass a race test too: sequential settlements from separate
  engines both land, five settlements on one engine all land, and a write from
  outside the process is picked up rather than clobbered.

  The lost 25 J and 50 J are deliberately NOT restored here. That is a separate
  reconciliation decision; this change stops the bleeding.

- **`coord reconcile` now reports the cards it refused to un-complete.** Pairs
  with the skcoord guard that skips cards which are `done` in the store but not
  yet in legacy, instead of dragging them backward to match a lagging legacy
  projection. Those cards keep failing parity, so the CLI has to say why: an
  operator otherwise sees a gate that will not go green with no explanation and
  reaches for a bigger hammer. Printed loud rather than dimmed, unlike the
  informational priority/swimlane bucket, because this is a real divergence a
  human must resolve on the legacy side rather than noise to filter out. Adds
  `--allow-uncomplete` (off by default, documented as MOVES THEM OUT OF DONE).

- **The packaged systemd unit tree now includes `skmeter.service`.** The drift
  guard `test_packaged_tree_matches_canonical` had been red on `main` for five
  consecutive commits because `scripts/sync-systemd-units.py` was not re-run when
  the canonical unit was added. Regenerated, no hand-editing. The guard exists
  precisely to catch this, and it did: what it could not do is stop the resulting
  red from making every other pull request's gate unreadable.
- **The execute-mux idempotency guard compared truthiness where it meant identity,
  so the code leg was never wired against a mocked dispatcher.**
  `_maybe_wire_execute_mux` read `if getattr(d, "_is_execute_mux", False)`, and any
  object with a permissive `__getattr__` satisfies that. `unittest.mock.Mock`
  auto-creates the attribute as a truthy child mock, so the function returned early
  and left the existing dispatcher unwrapped. `build_execute_mux` stamps exactly
  `True`, so the guard now compares with `is True`. This was one of three failures
  keeping `main` red. A regression test asserts both directions: a truthy-but-not-True
  marker does NOT count as already-muxed, and a real `True` marker still does, so the
  fix cannot be mistaken for disabling idempotency.

- **`black --check` no longer fails on `main`.** `src/skcapstone/cli/coord.py` was
  unformatted, so every open pull request inherited a red lint. A permanently red
  gate stops being a signal: it cannot distinguish a change that broke something
  from one that inherited the breakage, and the honest reading of any red becomes
  "probably pre-existing". Formatting only, one file, no behaviour change.

### Added

- **The test suite isolates the joule wallet by default, and asserts that it
  did.** `JouleWallet`/`JouleEngine` fall back to `skjoule.SHARED_ROOT` when no
  `home` is given, and mint/spend are writes to real economic state, so a test
  that forgot `home=tmp_path` edited the operator's live ledger and the entries
  it left were indistinguishable from real ones. The sibling harness measured
  1,366 fixture mints totalling 102,450 joules reaching a live wallet before
  anybody noticed. An autouse `_isolate_joule_wallet` fixture now redirects the
  default wallet root for every test, and `assert_not_production_wallet_in_test()`
  raises `ProductionWalletInTestError` when a test reaches a production root by
  some path the fixture does not cover (an explicit `home=`, for instance). The
  fixture keeps honest tests safe; the assertion is what proves they were. The
  set of production roots is frozen at import, because a guard whose definition
  of production moves with the thing it is guarding is not a guard.

- **`skfleet seat-audit`: two-seat detection by provenance, not by collision**
  (card `4c32df6f`, gap G2). The only existing detector was the Syncthing conflict
  file, and the drill measured what it actually catches: two seats writing inside
  one sync interval produce 1 conflict file, but the same two seats with a sync
  between the writes produce 10 writes and **zero** conflict files. The interleaved
  case is the likely one (a 368K folder converges in seconds against a 15-minute
  timer), and the promotion runbook's own advice to wait one full timer cycle names
  precisely the interval that guarantees no collision is raised. So a quiet conflict
  directory was being read as evidence of a single writer when it is nothing of the
  kind. The audit groups every spec by the `writer` block it already carries.
  Verified against the live store: one operator seat, 39 objects. A discarded
  conflict copy cannot inflate the count, and objects with no writer block are
  reported separately rather than counted as clean.
  Its limit is documented in the module and repeated in `--help`, because it decides
  how much a clean result is worth: this is CURRENT-STATE only. `write_spec` emits no
  event, so a second seat that wrote and was later overwritten leaves no trace at all.
  Closing that needs an event on `write_spec` (card `27aa2d4d`), not a better reader.

- **`skfleet label`, because there was no safe way to change a label.** Labels are
  what the scheduler actually filters on: `scheduler.feasible` reads them and never
  reads `spec.role`, so a node's labels decide whether anything can be placed on it.
  The only tool for changing one was `skfleet apply`, which replaces the whole spec
  from the document handed to it. During the promotion drill (card `4c32df6f`) a
  label-only apply silently dropped `taints`, `cordoned` and `address`, un-cordoning
  the node, and exited 0, so the documented way to fix a label corrupted the spec it
  was fixing. `skfleet label NODE key=value ... [--remove key]` merges instead:
  every other spec field survives and the generation bumps by exactly one. Removing
  an absent key is a silent no-op, so a revert is safe to run twice and safe to run
  when you do not know how far a promotion got. Setting and unsetting the same key
  in one call is refused rather than resolved, since either resolution would make
  the outcome depend on argument order.
  The promotion runbook's Step 2.2b is now one line instead of a copy-the-whole-
  document dance. A test asserts the end-to-end property the card is really about:
  `set-role node-41 control` alone leaves the seat INFEASIBLE for a
  `control-plane`-selecting workload, and labelling is what makes it schedulable.

### Fixed
- **The operator seat rewrote 7 unchanged specs every 15 minutes, forever.**
  `skoperator.timer` refreshes all operatorapp objects on a 15-minute cycle, and
  `_write_preserving_ratifications` called `write_spec` unconditionally.
  `write_spec` has no no-op short-circuit of its own, so every refresh bumped the
  generation and rewrote the file. Measured on the live control node: those objects
  had reached generation **1674**, and watching one across a tick caught the write
  directly, generation 1674 to 1675 with a **byte-identical body** (sha
  `3abb1b3523529136` on both sides). That is roughly **672 no-op writes a day** into
  `~/.skcapstone`, a Syncthing folder shared to four machines, which is the same
  shape as the outbox floods.
  The helper now compares before writing. The guard is deliberately in this caller
  rather than in `write_spec`: fourteen call sites rely on that primitive bumping
  the generation, and `set_role`, `set_taint` and `set_labels` each have a test
  asserting a bump of exactly one, so making the primitive conditional would change
  all of them to fix one caller. `write_placement` already returns
  `(existing, False)` on unchanged content, so the store's own precedent for this is
  per-writer rather than global.
  Four tests, negative-controlled (two fail against the old code). One covers the
  subtle case: the helper injects prior ratifications into the spec it compares, so
  a ratified object has to settle too, rather than differing from the incoming spec
  on every pass and rewriting forever.
  Downstream impact was checked rather than assumed and is currently nil:
  `store.py` flags a status stale when `observedGeneration < generation` and
  `service_controller` acts on that, but only three live statuses carry
  `observedGeneration` and all three are `node.json`. Nothing observes operatorapp
  objects, so the bumps marked nothing stale. That changes the moment anything does.

- **The drill's containment guard could be relocated by the caller** (card `4c32df6f`,
  gap G0). `drill.sovereign_home()` expanded `~` through `os.path.expanduser`, which
  prefers the `HOME` environment variable, so the definition of "production" moved
  whenever `HOME` moved. Drilled: under a rewritten `HOME` the guard computed a
  different forbidden prefix and ACCEPTED the real production tree as a drill root.
  No write was performed, but the refusal that is the entire point of the guard did
  not fire, and 10 of the other 11 refusal probes had passed, so the suite looked
  healthy. The leading `~` is now expanded against the password database, which the
  protected process cannot set. Negative-controlled: the new tests fail against the
  old code and pass against the fix, with a positive control asserting a legitimate
  scratch root is still accepted, since a guard that refused everything would satisfy
  the other assertions while being useless.

- **A test asserted the CAB self-approval bypass that skcoord had just closed, and it
  held `main` red.** skcoord `941570f` ("a raw `status` event can no longer grant CAB
  approval") stopped `update_change(..., new_status="approved")` from being the thing
  that grants approval, because `agent` there is free text: without the guard, any
  caller (the MCP tool and CLI included) could approve its own change around
  `submit_cab_vote()` and its no-self-approval fold guard.
  `test_process_one_execute_blocked_once_approved` built its fixture with exactly that
  shortcut, so it began asserting `approved` against a change the guard correctly left
  at `proposed`. The guard is right and the test was wrong, so the fixture now reaches
  approval the way a real CAB approval happens: a `human` APPROVE vote via
  `submit_cab_vote()`, with a voter that differs from the drafter so the fold's
  no-self-approval filter keeps it. No test helper force-sets the status, since that
  would rebuild the bypass inside the suite and leave it unable to notice a regression.
  A negative control was added alongside (`test_raw_status_event_cannot_grant_cab_approval`)
  that asserts the raw-status route still folds to `proposed`, so the guard now has a
  test that fails if it is ever removed. This was the only raw-status approval shortcut
  in the tree; it kept every open PR inheriting a red on `main`, which is how a red gate
  stops being a signal.

- **A Syncthing conflict copy silently overrode the real fleet object.**
  `store.list_specs` and `store.list_placements` globbed `*.json`, which also matches
  `<stem>.sync-conflict-<timestamp>-<device>.json`, and both readers key on the `name`
  field inside each payload rather than on the filename. The conflict copy therefore
  replaced the real object in the result mapping, so the store served the version
  Syncthing had discarded. Reproduced against a scratch fleet: with the object on disk
  at `role=builder-standby cordoned=true`, `skfleet nodes` reported `role=control` and
  showed no CORDONED flag, meaning a node an operator had explicitly cordoned read as
  schedulable to the scheduler. Found by running the promotion runbook end to end
  (card `4c32df6f`), where this is exactly the artifact a two-seat write produces: the
  conflict file meant to be the alarm had quietly become the corruption. Conflict
  copies are now skipped on read. They are not hidden, since the `SyncConflict`
  condition still reports them, so they remain visible as a finding while no longer
  being obeyed as data.

- **Six tests depended on a security bug and broke when it was fixed.** capauth PR #38 made
  `issue_token` refuse to store an unsigned capability token instead of downgrading the
  signing failure to a warning, which means every "signed" token this fleet issued through
  that path was in fact unsigned. The tests created a placeholder identity with no secret
  behind it and asked capauth to sign with it, so they had been passing on the strength of
  the bug. They now use genuinely signed tokens, and one test asserts the refusal itself,
  so the property is guarded rather than merely no longer violated.

### Added
- **Atlas can see when the watchdog stops narrating** (card `f0786ba6`, cross-repo follow-up
  to WD-11). The skos operator adapter declared two conditions and now declares four.
  `WatchdogDigestFresh` reads the published `digests/latest/digest.json` and fires when it is
  older than 26h. This is the signal nobody raises on their own: a missing morning digest
  reads to a human as "nothing happened", not as "the thing that tells me what happened is
  broken". `GradingBacklog` fires only when the latest digest carries a `GradingGap` event
  whose `meta.budget_exhausted` is true, meaning `GRADE_RUN_BUDGET_S` ran out mid-list.
  That flag is the whole point of the condition. The same `GradingGap` kind is also emitted
  when the grader was unreachable or a reply did not parse, which is grader availability and
  not backlog, so the check stays narrow on purpose: widening it to any `GradingGap` would
  make every skgateway blip look like a backlog and retire the real signal.
  Two properties are held by tests rather than by intent. Observation is read-only: the
  watchdog root is resolved by mirroring `skos.watchdog.cursor.watchdog_home()`'s precedence
  instead of calling it, because that helper (and `publish.digests_dir` / `latest_dir`)
  mkdirs, and a probe that creates the store it looks at manufactures the state it reports.
  A test asserts nothing exists under the root after a full observe pass. And the two
  watchdog probes fail to `Unknown`, never to healthy, so a missing or unreadable digest can
  never report "fresh" and silence the exact case the condition exists to catch.
  `GradingBacklog` is the first problem-when-true condition owned by an app adapter, so
  `loop.PROBLEM_WHEN_TRUE` now unions each adapter's own declaration onto the fleet's set;
  without that the brief would have read the backlog upside down and fired when grading was
  healthy. skos' `skworld_manifest` mirrors this adapter's `CONDITIONS` and the drift guard
  lives in this repo, so the manifest needs the same two entries in the same order.
- **`.100` holds a node-class capauth identity, and the PDP ceiling is proven rather than
  asserted** (epic `3bbf39ea`, card `5ee6510f`). The key was generated on `.100` and its
  private half never left the box. The class assignment denies `token:issue`,
  `identity:sign` and `*` with the pinned reason strings, while the same subject is still
  allowed `skgateway.infer`, so the denial is demonstrably about the capability and not a
  broken subject.
  Two results worth keeping. Re-deciding all 133 enrolled subjects across 12 capabilities
  (1596 decisions) against a store with the class row removed changed exactly 9 outcomes,
  all of them the node itself and all of them already denials, so no live seat lost access.
  And a genuinely signed `Capability.ALL` token is denied with the class present and
  GRANTS with it removed, which is what makes the ceiling load-bearing rather than
  decorative. The previous protection was incidental: `token:issue` was denied only as an
  unknown capability and `change.deploy` only on enrollment mode, both of which would
  evaporate on a rule addition or a `verified` enrollment.
  `scripts/fleet/issue-node-identity.py` captures the attestation trap in code: an attested
  enrollment needs a signed MESSAGE, not a detached signature, because the verifier compares
  the embedded payload against the challenge bytes. A detached blob passes `gpg --verify`,
  passes bare pgpy, and is still rejected.

### Fixed
- **Six tests asked capauth for a token no key could sign, and used to get one.**
  They stood up a throwaway identity whose fingerprint has no secret half in any
  gpg keyring, then issued a capability token against it. Until capauth PR #38 that
  silently produced an UNSIGNED token, which `capauth.authz.decide` rejects, so
  every token those paths ever minted authorized nothing while looking issued.
  capauth now refuses, and the tests went red. They are fixed honestly rather than
  skipped: a session-scoped fixture generates a real passphrase-less ed25519 key in
  an isolated temp `GNUPGHOME` (never `~/.gnupg`, gpg-agent killed and the home
  removed on teardown) and binds the agent's identity to it, so issuance runs the
  genuine sign-then-store path. `test_full_token_lifecycle` now also asserts the
  token actually carries a signature and that `verify_token` affirms it, which no
  test checked before. Added
  `TestIdentityTokenLifecycle::test_issue_refuses_when_issuer_has_no_secret_key`
  as the regression guard: against an empty keyring, issuance must raise
  `TokenSigningError` and leave the token store empty. Nothing in this repo
  guarded that fail-closed behaviour until now. Tests only; no `src/` change.
- **The `.100` smoke gate could pass while sovereign traffic went to the cloud.**
  It probed `.100:8082` directly, which proves the node is up and proves nothing
  about what actually answers `sk-default`. When `.100` was down for four hours on
  2026-08-16, skgateway silently failed `sk-default` over to a cloud provider and a
  direct probe would have returned a healthy 200 throughout. Added a
  `gateway-sovereignty` probe that asks the gateway and asserts the SERVING model is
  sovereign hardware, so a silent substitution fails the gate instead of hiding
  behind it. Absent gateway skips rather than fails, since the script also runs on
  boxes that do not host one.

### Added
- **Node roles wave 4** (epic `3bbf39ea`). Report-only throughout; no node state changed.
  - `skfleet node stignore`: checks that a node holding a sovereign Syncthing folder
    still carries the private-key ignore rules. Those three lines (`*.key`, `*.pem`,
    `**/private.*`) are the only reason the control node holds 11 agent private keys
    while the GPU worker holds zero, in the same `sendreceive` folder. Keyed by FOLDER
    id, not by role, because every node in a folder needs byte-identical rules; a
    role-keyed ruleset would recreate the drift it exists to stop.
  - `skfleet drill`: a scratch-fleet harness for rehearsing the promotion runbook. It
    is structurally incapable of touching production: the target is resolved before it
    is judged (so `..` and symlinks cannot walk in), the forbidden prefix is the whole
    sovereign home rather than the fleet folder alone, an ownership marker means it can
    never adopt or delete a tree it did not create, and `SKFLEET_ROOT` is never read as
    the target so an exported variable cannot aim a drill at the live fleet.
  - `src/skcapstone/defaults/.stignore` refreshed from 40 to 70 patterns with nothing
    dropped, comments carried across with their rules. Those comments are the only
    record of which incident bought each rule.
  - Docs: the control-bus nesting decision, the `skfleet-control` share runbook, the
    control unit set `.41` must gain to become control, and the promotion runbook with
    a documented revert for every step.

### Fixed
- **`skfleet node doctor <name>` graded the LOCAL node's units against another node's
  profile.** Run from the control node, `node doctor node-41` collected this node's
  inventory and returned a confident, well-formed report about the wrong machine. It
  disagreed silently with `--all`, which correctly reported that node-41 had published
  no inventory. Only the local node can be inventoried live; any other node is now read
  from what it published, the same source `--all` uses, through one shared helper so the
  two paths cannot drift apart again.

### Added
- **`skfleet install`: profile-aware stack installer (the actuation half of the
  node-roles-install-profiles epic `3bbf39ea`).** Reads the applied profile from the
  synced fleet store, reports drift (`--check`), and closes `missing_required`
  packages/units (`--apply`) by driving the per-repo installers as backends. Freeze +
  per-node actuation-opt-in gated; only ever adds `missing_required`, never removes;
  `--json` is the contract the AI/GUI install wizard wraps. `fleet/installer.py` +
  `fleet/install_backends.py` + the `skfleet install` verb.
- **Fleet install profiles: a node can now say what it is, and be checked against
  it** (epic `3bbf39ea`, waves 1 and 2). The fleet could already schedule work onto
  nodes; nothing said what a node of a given role was *supposed to have installed*.
  - `fleet/profiles.py` defines the `Profile` kind. Service role and state tier are
    **orthogonal** fields, neither derived from the other, because conflating them is
    how a GPU worker ended up holding agent memories and stale checkouts it never
    needed. `stateTier` and `capauthIdentityClass` have no default: a profile that
    will not say how much state it holds is one nobody should converge against.
    Contradictions (a name both allowed and forbidden, or required but not allowed)
    raise rather than resolve, so a drift verdict is never non-deterministic.
  - `fleet/nodeinventory.py` does read-only observation of enabled units and installed
    SK packages, through the injectable runner from `actuation.py`. Degrades to empty
    rather than raising, so an uninventoriable node reports nothing instead of
    reading as "everything is missing".
  - `fleet/profile_doctor.py` is the pure six-category diff. Severity is deliberately
    asymmetric: `forbidden` is the only error grade, `missing_required` is warn, and
    `unexpected` is info, because grading a lagging manifest as an error trains
    everyone to ignore the report.
  - `Node.spec.role` plus `skfleet set-role` and `admit --role`, binding a node to a
    profile. An unbound node is a legitimate state that the doctor skips, not an error.
  - `skfleet node doctor [--json] [--all] [--strict]`. Report only: exits 0 with
    drift unless `--strict`, and performs zero writes.
  - `skfleet get profiles` and `skfleet explain profile`.
  - Four generated manifests under `deploy/fleet-objects/profile/` (control,
    builder-standby, worker-gpu, observer), produced from real read-only node
    inventories rather than hand-typed.
  - `scripts/fleet/dot100-inference-smoke.sh`, the before/after gate for the
    inference node, and `scripts/fleet/gen-node-disposition.py`.
- **Alert surface + execute mux + comms send authority** (P4, card `c6a87139`,
  design doc `docs/specs/2026-08-13-unified-consent-plane-arch.md` sections 5+6).
  Chef's ask: "when the alert comes up, give me the option of next steps so I can
  just say 'do it'." `agent_run.ensure_card` gains an `alert-` branch (a new
  `alert_store.py` file-per-id record store, since none existed: pubsub messages
  are ephemeral/24h-TTL/pruned with no get-by-id, and `operator_seat.decisions`
  belongs to a different subsystem's invariants) that materializes a shadow card
  carrying the alert's own 2-4 options in `meta.origin.options`, surfaced verbatim
  by `suggest_next_steps` (never LLM-regenerated for an alert card: design doc
  section 4.3, untrusted text must never write the option list) and clamped
  draft-only by the existing send-verb check. `mcp_tools/suggest_tools.py`'s
  `_SURFACES`/`_resolve_card_id` registry gains `"alert"` alongside `coord`/`gtd`/
  `itil`, and `agent_run.gate()` gains an `origin == "alert"` row, same shape as
  the existing `gtd` row.
  New `execute_mux.build_execute_mux()` reads the folded card's `meta.origin.surface`
  and a `repo:<name>` label to route a queued execute run to the code bridge or to
  a new `comms_executor.CommsExecutor`; `agent_run._maybe_wire_execute_mux()` wires
  it into the existing `set_execute_dispatcher` seam every job tick, wrapping
  whatever `_maybe_wire_execute_bridge()` left behind (that function's own
  fail-closed contract, and its test suite, are untouched). `CommsExecutor` can
  only ever draft: it imports no send-capable client, and its `send()` method
  exists only to raise, mirroring `skharness.autocode.direct.DirectExecutor._merge`.
  New `send_authority.SendAuthority` is the only object able to invoke a (currently
  unwired, fail-closed) send dispatcher, and only when constructed with an explicit
  `armed=True`, given an `armed_by` identity, and that identity differs from the
  draft's `prepared_by` (no-self-approval, mirroring `skcoord.itil`'s CAB
  `agent != prepared_by` fold guard); any missing condition raises
  `SendAuthorityError` before a transport is ever touched. Wiring a real transport
  behind `send_authority.set_send_dispatcher` is deliberately future work; this
  card ships the structural boundary it will sit behind.

### Added
- **Node roles wave 3** (epic `3bbf39ea`): the profile layer gains its enforcement
  path, its budget guard and its ADRs. Everything remains report-only by default.
  - `skfleet node doctor` now works fleet-wide with no ssh: `sknoded` publishes a
    bounded `status.inventory` block into `node.json`. Collected on a 15 minute
    cadence rather than the 60s heartbeat, sorted, capped, and stripped of its
    timestamp before the write-on-change comparison, so an unchanged node writes
    nothing and cannot flood the control-bus folder.
  - `SKFLEET_PROFILE_GATE` (`off` default, `shadow`, `enforce`) in `converge`,
    modelled on the existing `SKFLEET_SIGNING` rollout. Enforce refuses to HEAL a
    unit the role forbids and never issues a stop verb: stopping a running service
    over a manifest disagreement is the failure this layer exists to avoid.
  - `skfleet taint` / `skfleet untaint`. `NoExecute` is deliberately rejected
    because nothing in this fleet evicts a running workload, so accepting it would
    be policy that reads like eviction and does nothing.
  - `skfleet control-bus audit`, enforcing the 10MB scope contract and naming the
    two growth risks by name. It reports that `events.jsonl` is capped at 2MB per
    node, so five nodes at the cap would spend the entire budget.
  - ADRs: `docs/fleet/adr-node-role-model.md` (the two axes, the four roles, the
    accepted single-management-seat SPOF, norpv1300 left unmanaged) and
    `docs/fleet/adr-edge-device-class.md` (an auth-only device is a capauth device
    class, not a node role).

### Fixed
- **`skfleet node doctor` read an absent inventory as total drift.** `.get("inventory")
  or {}` collapsed ABSENT into EMPTY, so a node that had simply not published yet
  graded as missing every required unit. During a rollout that is every node not
  upgraded. Absent now skips with a note, while a genuinely empty published
  inventory is still graded, since fixing only the first half would have hidden
  real findings.
- **`worker-gpu` declared `stateTier: none` while joining `skfleet-control`.**
  Self-contradictory: `none` means the node holds no SK state, and that folder is
  the fleet store. Corrected to `control-bus`, matching what both design documents
  already said, with a test pinning the general rule that a tier and the folders it
  joins must agree.
- **Build work could be scheduled onto the control box.** The live fleet store
  carried `heavy-build=true` on BOTH `node-41` (20 cores, 739G disk) and
  `node-noroc2027`, the control node, which has 4 cores and under 5G of free disk.
  `scheduler.feasible()` filters on cores and ram only and never looks at disk, so a
  build declaring modest requests but a large real disk footprint could legitimately
  land on the small box and fill its root filesystem. Label removed from the control
  node, with a regression test pinning the consequence using the real measured
  capacity numbers. `node-41` also carried `gpu=true`; `nvidia-smi` fails there, there
  is no `/dev/nvidia*`, and the only display device is Intel Iris Xe integrated
  graphics, so that label was removed too. The GPU box in this fleet is .100.
- **`admission.PRESETS` keys were dead.** They were keyed `node-158`, but
  `paths.self_node_name()` derives the node name from the hostname, so the live
  control node is `node-noroc2027` and `skfleet admit --preset` silently applied
  **nothing** there. Rekeyed to real node names, with `PRESET_ALIASES` keeping the
  old spelling working. The existing test asserted the broken key set, so it was
  pinning the defect rather than catching it.
- **`syncthing_setup._write_stignore()` silently reverted a node's ignore rules.**
  It was an unconditional `write_text()` of the bundled template, so one
  `skcapstone sync setup` reverted that node to whatever the packaged template held.
  The live rules had drifted ~40 lines ahead, and each of those lines is an incident
  someone already paid for (`**/comms/outbox`, the SQLite `-shm`/`-wal` rules,
  `**/memory/chroma`, `(?d)**/*.tmp`). Now non-destructive: union only, back up
  first, no write when already covered. Union is the only safe direction for an
  ignore file, since ignoring less leaks keys and floods the mesh.
- **Docs claimed skcapstone holds no key material. It does.** `SOP.md` §9 and
  `README.md` both declared the maturity tier as
  `T0 / N/A (no key material; delegates identity/crypto to capauth)`. The delegation
  half is true, the "no key material" half is not: `src/skcapstone/tls.py` generates an
  RSA-2048 private key and writes it unencrypted to `~/.skcapstone/tls/daemon.key`
  (0600), `src/skcapstone/sync/vault.py` PGP-encrypts and GPG-detached-signs state
  bundles, and `src/skcapstone/fleet/signing.py` produces and verifies detached capauth
  signatures. The tier stays **T0** (everything is classical), but the false clause is
  gone and §9 now carries the actual per-surface inventory.
- **Docs sent operators to the wrong port.** The SOP asserted `127.0.0.1:7777`
  throughout. On a fleet node the daemon binds a **per-agent** port resolved by
  `_resolve_agent_port` (`lumina` 9383, `opus` 9389, `jarvis` 9391, unknown agents
  9400-9499); the live daemon here answers on **9383** and nothing listens on 7777.
  Two constants named `DEFAULT_PORT` disagree (`daemon.py` 7777 vs `__init__.py` 9383)
  and the CLI imports the latter. §5 now documents the resolution order and how to
  resolve the port instead of assuming it.
- **`SKCAPSTONE_ROOT` documented as the home override.** It is not;
  `SKCAPSTONE_HOME` is. `SKCAPSTONE_ROOT` and `SKCAPSTONE_SHARED_ROOT` are
  backwards-compatible aliases that default to `SKCAPSTONE_HOME`, so setting only
  `SKCAPSTONE_ROOT` silently fails to move anything.
- **Version and release steps were impossible to follow.** §5 said to bump `version`
  in `pyproject.toml` and mirror it in `package.json`. `pyproject.toml` is
  `dynamic = ["version"]` (setuptools-scm, the tag is the version) and there is **no
  `package.json`** in this repo. The quoted `0.13.0` matched nothing (newest tag
  `v0.15.14`). §3/§5/§9 now describe the real flow.
- **`ci.yml` was implied to be a test gate. It runs no tests** (black, ruff, a
  shim-import check, build). The gate is `pytest.yml`. §4 now contrasts the two.
- Console scripts listed as three; there are **five** (`skfleet` and `skoperator` were
  missing). MCP tool count was stated as both "80+" and "125" in the same document.
- Documented that `skcapstone.coordination` / `.card_store` / `.itil` are transparent
  re-export shims over the hard dependency `skcoord`, so patching them here does
  nothing.

### Added
- **`docs-evidence` block + `.github/workflows/docs-check.yml`** (tiers 1,2). Twelve
  executable checks pin the five entry points, both `DEFAULT_PORT` constants, the
  per-agent port map, the loopback-only bind, the `/ping` handler, the TLS key
  inventory, the version source, the skcoord shim, `SKCAPSTONE_HOME`, and the fact
  that `ci.yml` runs no tests. Negative-tested with 25 mutations, all correctly
  non-zero.
- **GFS backup job + staleness monitor** (`gfs_backup.py`). A scheduled backup
  built on the existing `backup.create_backup` primitive with Grandfather-Father-Son
  retention and a health monitor. `select_gfs_retention()` is a pure function that,
  given timestamped artifacts and a `GFSPolicy` (daily/weekly/monthly/yearly counts),
  returns the keep/prune partition using borg/restic union semantics (newest per
  distinct period, per tier); an all-zero policy means "pruning disabled", never
  "delete all". `run_backup_job()` creates an artifact in a configurable dir, prunes
  with hard confinement (only `backup-*.tar.gz` files directly inside the backup dir
  are ever unlinked), and records a `gfs-state.json` sidecar. `check_backup_health()`
  reports `ok`/`stale`/`missing`/`failed` against a freshness threshold. All
  destinations/thresholds are config-driven (`config.yaml` `backup:` block or
  `SKCAPSTONE_BACKUP_*` env vars) with safe defaults (7 daily / 4 weekly / 6 monthly,
  26h threshold). New CLI `skcapstone backup gfs` and `skcapstone backup health`
  (exits non-zero when unhealthy). Zero-arg scheduler entrypoints
  `skcapstone.gfs_backup:run_scheduled_backup` and `:run_backup_monitor` for a
  `type: python` job; `make_backup_monitor_task()` logs and (with
  `SKCAPSTONE_BACKUP_ALERT=1`) fires `sk-alert`. Template systemd units in `systemd/`
  (`skcapstone-gfs-backup{,-monitor}.{service,timer}`) are inert until manually
  installed; install + `jobs.d` wiring documented in `docs/BACKUP.md`. Coexists with
  the operator shell cron (`scripts/skcapstone-gfs-backup.sh`) without touching its
  `skcapstone-state-*` output. Covered by `tests/test_gfs_backup.py` (22 tests).
- **Context-window management in the consciousness loop.** New
  `ContextWindowManager` (`context_window.py`) is wired into `ConsciousnessLoop._process`:
  after each reply it tracks per-sender cumulative token usage and, once a peer's
  history reaches 80% of `ConsciousnessConfig.max_context_tokens` (default `8000`),
  the oldest messages are summarized by the LLM into a single paragraph (keeping the
  4 most recent verbatim) and atomically rewritten. Token counting uses `tiktoken`
  (`cl100k_base`) when installed, else a `len // 4` estimate. The compression summary
  is also persisted as a durable memory (`_store_context_summary_memory`) so nothing
  is lost. Adds `ConversationStore.replace()` for the atomic whole-history rewrite and
  a new `context_stats` MCP tool (per-sender token/message counts, percent of budget,
  last-compressed timestamp), bumping the MCP tool count 124 → 125. The whole check is
  fail-safe: any error is caught and never breaks the loop.
- **Gated desktop notification on consciousness-loop responses.** After generating a
  reply the loop routes through the shared `skcapstone.notifications` path
  (`_notify_response`): title `"Agent response"`, body the first 120 chars. It is
  strictly opt-in via `SKCAPSTONE_DESKTOP_NOTIFY` (default off, checked through
  `desktop_notifications_enabled()`), so background agents never flood the desktop
  tray, and any failure is swallowed. Replaces an ad-hoc raw `notify-send` subprocess.
- **Agent systemd unit hardening.** The per-agent template `skcapstone@.service` (and
  the legacy single-agent unit, the packaged copy under `src/skcapstone/data/systemd/`,
  and the `generate_unit_file()` code path) now ship with `MemoryHigh=3G` /
  `MemoryMax=4G`, exponential restart backoff (`RestartSteps=5` +
  `RestartMaxDelaySec=300`, so restarts ramp 10s → 20s → 40s … capped at 5 min instead
  of a fixed 10s hot-loop), and a crash-loop guard (`StartLimitIntervalSec=1800` +
  `StartLimitBurst=6`) so a persistently failing daemon stops and stays failed inside
  a bounded window. Adds `OnFailure=skcapstone-alert@%i.service`: a new best-effort
  oneshot unit that always writes a visible journal event (tag `skcapstone-alert`,
  priority `err`) and opportunistically pages via `sk-alert`. This encodes the .41
  outage fix (previously hand-applied host state only) into the repo so rebuilt
  machines inherit it.
- **`coord reconcile` command + parity open-count alert.** New `coord reconcile`
  (`--apply`) converges the CardStore fold on the authoritative legacy board via
  append-only, idempotent corrective events (`card_store.reconcile_from_legacy`). The
  `coord parity` soak check now also compares store-served vs legacy open-counts and
  raises a `PARITY ALERT` when the drift exceeds `OPEN_DRIFT_THRESHOLD`, pointing at
  the `coord migrate` → `coord reconcile --apply` repair path.

### Changed
- **Model tier defaults now resolve to backend-verified models.** The default
  `ModelRouterConfig` tier map referenced Ollama names never pulled on the fleet
  (`devstral`, `deepseek-r1:8b`, `qwen3-coder`) which 404'd, and a stale
  `claude-sonnet-4-5` alt. Defaults are re-pointed to models verified live against
  Ollama `/api/tags` (`qwen3.5:4b`, `gemma3:1b`) and the SKGateway `/v1/models`
  catalog (`claude-sonnet-4-6`, `claude-haiku-4-5`, `claude-opus-4-8`).

### Fixed
- **CardStore fold-drift.** The store fold now consumes the two sanctioned legacy
  append-only paths (`coordination/archive/<host>.jsonl` archive index +
  `coordination/card_events/*.jsonl` kanban overlay) as synthesized fold events
  (`load_legacy_mutations`), merged into each card's event stream. Mutations that only
  reached a legacy file (mirror off, or claims/completes recorded pre-cutover) are now
  seen by the fold, so `coord status` no longer overcounts open cards.
- **MCP GTD writes routed through the locked/atomic/deduped skos sink.** `gtd_tools`
  was the last writer using bare `path.write_text` (in-place truncate, no flock, no
  tmp+fsync+os.replace, no whole-store dedupe). `_handle_gtd_capture` now routes
  through `skos.gtd_ingest.capture()` (whole-store `(source, source_ref)` dedupe +
  lock + atomic save) when available; the id-keyed `clarify`/`move`/`done` mutations
  wrap their load-modify-save under the shared `_store_lock()` and persist via the
  atomic saver. Soft-imports skos's exact mechanism (with a local fallback keyed on the
  same lock) so cross-process exclusion with skos holds either way.
- **Repaired pre-existing test failures + wired the memory-promotion truth gate.**
  `fuse_mount.SovereignFS.__init__` no longer crashes with `PosixPath / None` when no
  agent resolves (mirrors `memory_engine._memory_dir` resolution). The memory-promotion
  truth-check gate (`memory_verifier.verify_before_promotion`) is now wired into the
  SHORT_TERM → MID_TERM transition in `memory_engine._promote` / `store()` and
  `PromotionEngine._promote` (fail-open when the backend is unavailable; blocked
  candidates stay in short-term). Test isolation was hardened so the suite reads no
  live `~/.skcapstone` and needs no network, and `daemon._load_components` registers
  the dreaming-job loop reference outside the scheduler-build try-block. Clean
  `pytest -m "not integration and not e2e"` run is green.

## [0.14.0] - 2026-07-03

### Added
- **Per-sender consciousness rate limiting.** The consciousness loop now
  throttles inbound message intake with a thread-safe, per-sender sliding
  window (`_RateLimiter`). Over-limit messages are skipped (logged, never
  crashing the loop); each sender has an isolated window that resets over time,
  and sender identities are normalized before counting. Configurable via new
  `ConsciousnessConfig` keys `rate_limit_enabled` (default `true`),
  `rate_limit_max_messages` (default `20`), and `rate_limit_window_s`
  (default `60.0`); a non-positive `rate_limit_max_messages` disables limiting.
- **Startup pillar-degradation health check + notify.** New
  `skcapstone.health` module (`startup_health_check` / `degraded_pillars`)
  evaluates every pillar's status at startup and emits a single `critical`
  desktop notification (reusing `skcapstone.notifications`) summarizing any
  `DEGRADED` / `ERROR` pillars. Healthy startups (all pillars `ACTIVE` or
  `MISSING`) notify nothing. Wired into `runtime.py`.
- **Message-classification logging + `consciousness classification` CLI.**
  `ConsciousnessMetrics.record_classification()` tracks per-tag counts
  (persisted in daily snapshots and surfaced in `to_dict()` /
  the `/consciousness` endpoint as `classification_usage`). The loop now emits
  an INFO `Classified message` log record (sender, tags, ~tokens, privacy) and
  records the tag distribution — observability only, routing behavior is
  unchanged. New `skcapstone consciousness classification` command shows
  today's tag distribution as a Rich table (with `--json-out`), reading the
  live daemon first and falling back to today's daily metrics file.
- **Recommended GFS backup cron + docs.** New `scripts/skcapstone-gfs-backup.sh`
  writes compressed, checksummed tarballs of the *irreplaceable* `~/.skcapstone`
  state on a Grandfather-Father-Son rotation (14 daily / 8 weekly / 12 monthly /
  2 yearly), excluding the rebuildable vector store + `index.db` and transient
  churn (comms queues, logs, skwhisper cache, media renders) so a ~0.8 GB home
  compresses to ~80 MB and the whole rotation stays a few GB. Includes a 2 GB
  free-space guard (fires `sk-alert` on low disk) and per-file `.sha256`
  sidecars. Optional **off-site 3-2-1 replication**: set `OFFSITE_DEST` in
  `~/.skcapstone/config/backup.env` and each run also `rsync`s the whole
  rotation to another host (best-effort — a failed push alerts but never fails
  the local backup). Documented in [docs/BACKUP.md](docs/BACKUP.md) alongside the
  portable `skcapstone backup` CLI, with a cross-link from
  [docs/HOUSEKEEPING.md](docs/HOUSEKEEPING.md) (backup preserves / housekeeping
  prunes) and a Documentation-table row in the README.
### Changed
- **ITIL → GTD is now a push adapter on the skos `gtd-ingest` port.**
  `itil.py::_gtd_emit()` builds `GtdCapture(source="itil", source_ref=<id>)` and
  routes incidents/problems/changes through `skos.gtd_ingest.capture()` (deduped by
  ID, idempotent), with a legacy fallback if skos isn't importable. Same sev →
  next-action/inbox routing; the store is now unified with all other GTD sources.
  See skos `docs/gtd-ingest-architecture.md` + `docs/gtd-ingest-SOP.md`.

---

## [0.13.0] — 2026-06-16

### Added
- **Legacy & broadcast comms-outbox sweep in housekeeping.** New
  `prune_legacy_comms()` sweeps the v1 outbox layouts that the v2-only
  housekeeping never reached: `~/.skcapstone/comms/outbox/<recipient>/` and
  every `~/.skcapstone/agents/<agent>/comms/outbox/<recipient>/`. Stale
  `*.skc.json` envelopes (>7d) are deleted; a v1 broadcast subdir literally
  named `*` is removed wholesale regardless of age. Wired into
  `run_housekeeping` as the `legacy_comms` target (with dry-run counting via
  `_count_stale_legacy_comms`) and surfaced in the `skcapstone housekeeping`
  CLI table.
- **Weekly housekeeping default job.** A standalone `jobs.d` drop-in
  (`config/jobs.d/housekeeping.yaml`, schedule `0 4 * * 0`) runs
  `skcapstone housekeeping` weekly as a safety net decoupled from the daemon.
  Bundled in package defaults and installed idempotently into
  `~/.skcapstone/config/jobs.d/` on a fresh `init` (never overwrites an
  existing user file).

### Fixed
- Prevents the unbounded profile growth that overheated a Framework 13 laptop
  (462k files in `~/.skcapstone`). Root cause: ~256k stale v1 `recipient="*"`
  presence-broadcast envelopes accumulating in directories literally named
  `*` under the legacy v1 outbox paths, which the existing v2 housekeeping
  never swept.

---

## [0.9.0] — 2026-03-02

### Sprint 15 — Exception Handlers, LLM Retry, Tests, Docs, Systemd, Deps
- Added structured exception handlers across CLI and daemon entrypoints
- Implemented LLM retry logic with exponential back-off in `LLMBridge`
- Expanded test suite: consciousness E2E, cross-package, agent runtime coverage
- Added `systemd` service unit template with watchdog dependency and consciousness flags
- Updated `pyproject.toml` dev dependencies: `pytest-cov>=4.0`, `pytest-asyncio>=0.21`
- Improved inline documentation and docstrings across all pillars

### Sprint 14 — Production Hardening
- ACK (acknowledgement) protocol for reliable SKComm message delivery
- Message deduplication layer prevents duplicate processing under inotify storms
- Input validation hardened on all daemon API endpoints
- Inotify watcher now auto-restarts on `OSError` (inotify limit exceeded)
- Reduced false-positive self-healing triggers via smarter health-check thresholds

### Sprint 13 — CPU Inference Optimization, Daemon E2E, Ollama Fixes
- CPU-only inference path: batching, thread pinning, reduced context window for low-RAM hosts
- End-to-end daemon test: start → send SKComm message → verify LLM response in < 60 s
- Fixed Ollama model-not-found error when model name included `:latest` tag
- `skcapstone daemon start` now waits for Ollama readiness before accepting messages
- `consciousness status` CLI command shows live backends, message counts, and conversation count

### Sprint 12 — Fallback Cascade Fix, llama3.2 FAST Tier, Timeout Scaling
- Fixed `LLMBridge.generate()` fallback cascade — passthrough tier was never reached
- `llama3.2` (2 GB) promoted to primary FAST tier for CPU-only hosts
- Response timeout now scales linearly with model size (configurable via `SKCAPSTONE_TIMEOUT_SCALE`)
- Tailscale transport hostname matching switched to exact match (fixes substring collision)

### Sprint 10–11 — Model Tier Fixes, Context Loader, Exports, Flutter UI
- Three-tier model routing: FAST (`llama3.2`) → STANDARD → CAPABLE (configurable)
- `context_loader.py`: injects agent identity and recent memories into system prompt
- Clean public exports from `skcapstone.__init__` (`ConsciousnessLoop`, `LLMBridge`, etc.)
- Flutter dashboard: agent status card, consciousness badge (online/offline), message feed
- `skcapstone coord` CLI surface: `status`, `claim`, `complete`, `list`

### Sprint 9 — Consciousness Loop, Prompt Adapter, Self-Healing
- `consciousness_loop.py`: autonomous message-processing loop backed by SKComm inotify watcher
- `prompt_adapter.py`: `ModelProfile` + `PromptAdapter` normalise prompts across Ollama model families
- `self_healing.py`: `SelfHealingDoctor` monitors pillars, auto-remediates common faults
- `ConsciousnessConfig` dataclass — YAML-driven configuration for all loop parameters
- `/consciousness` HTTP endpoint exposes live status (backends, counters, conversations)

---

## [0.1.0] — 2025-11-01 (initial release)

### Added
- Core pillar scaffold: identity, memory, trust, security, sync, skills
- `skcapstone status` CLI with Rich table output
- MCP server with `memory_store`, `memory_search`, `coord_status`, `coord_claim` tools
- CapAuth PGP fingerprint identity verification
- Coordination board (YAML-backed): tasks, agents, priorities
- `skcapstone context --format claude-md` for Claude Code integration
