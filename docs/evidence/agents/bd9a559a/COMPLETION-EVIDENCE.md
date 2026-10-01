# Fiber infrastructure rollout evidence

This report records operator work under card bd9a559a. Original card 09cf4fec,
its claim and its worktree remain preserved. All commits are local; this work
does not deploy SKLegal or authorize protected Matter processing.

## Installed behavior

- Six execution nodes are qualified: chiap02, chiap03, chiap04, chiwk12,
  chiwk13 and ziowk01. Chiap08 remains the controller and chiap01 the gateway.
- Scheduler worker-count and fixed-spacing gates outside SKGateway are removed.
  Gateway provider pools own request concurrency and backpressure. Per-worker
  node-qualified CPU, RAM, swap, task and runtime limits remain enforced.
- Exact source and claim checks, private namespaces, duplicate execution fences,
  independent cross-family review and required external tests remain mandatory.
  Card failures do not impose a global automatic HOLD.
- Candidate bundles carry unpublished commits to another machine for review.
  The controller waits for the authoritative execution-record link before
  exporting a candidate, preventing delayed synchronization from invalidating
  an already sealed review snapshot.
- Dependency admission requires DONE and exact PASS. Controller pagination uses
  persistent fair cursors, and the timer checks again five seconds after a cycle.
- Worker requests preserve gateway backpressure and explicit generation budgets.
  Default generation policy comes from SKGateway model metadata. The initial
  configured value is 8192 on the four qualified routes, an operator default,
  not a measured provider maximum. SDK context handling and the Codex adapter's
  omission of token-budget fields remain existing behavior.
- Completion guidance reaches implementation and review workers. Reports avoid
  self-referential final hashes while reviewer source-head and report-hash
  requirements remain intact. Reviewers commit their own evidence before exit.
- Kimi is disabled because no subscription is active. DeepSeek, Zai, Codex and
  local Qwen each passed an attributed completion after the gateway restart.
- SKLegal repository read access was verified on all eight nodes. The two
  previously missing nodes use their own read-only deploy keys. Credential
  containment is recorded separately under completed card 7481c398.

## Exact deployment and validation

The installed Fiber composite is the historical source snapshot
`b7c0bea695729a7ea8a455d7eafada929fff4c5a`, tree
`36a5a001f547b932dd83d77a74e27451c8976955`. It combines reviewed leaves 6e47a2bd,
9e26a7d1 and 93fbd7c1 above the installed execution-link repair 7bee43ff.
Rejected source 8abcfd10 is excluded. This report is a later evidence artifact;
the external descriptor binds the commit containing this report.

Gateway source snapshot `08d9afebd3c1e4e096935cfd41190369df98057f` changes only
`src/index.mjs` and `config/model-cards.overrides.yaml`. Both candidates received
actual independent Zai PASS reviews, with exact head and tree attribution.

Evidence root: `~/.skcapstone/evidence/work/bd9a559a/`.

- `FINAL-GENERATION-COMPOSITE.json` binds exact source, hashes and test logs.
- `FINAL-GENERATION-ALL-TESTS.log`: 483 passing test cases and 412 passing
  subtests. Two initially conditional private-sandbox cases were explicitly
  rerun and passed. One remote-resource-only skip remains; node qualification
  is separately recorded. No failing tests.
- `review-zai-1790833050340941785.json`: actual exact composite PASS.
- `GENERATION-HARNESS-1790832960226614699.json`: 99 scratch installation-layout
  checks passed, zero skips, installed runtime unchanged during rehearsal.
- `install-generation-1790833296014899875.json`: 15 files installed across
  seven hosts. Each of six execution nodes passed 50 installed-module checks;
  the authority passed 99. All seven read the generation policy successfully.
  Other runtime module hashes, modes and owners were preserved.
- Gateway card f54adfd1 `GATEWAY-INSTALL-1790833259561150503.json`: two files
  installed, 80 other source files and private configuration preserved,
  service healthy, four model defaults verified and four completions accepted.
  Its source suite passed 97 tests without skips.

## Real automatic cycle and quality findings

`UNASSISTED-9E-ACCEPTED-CHECKPOINT-1790832685133187755.json` proves the full
automatic cycle for source 9e26a7d1 and review dc350cfe:

1. DeepSeek implemented on chiap03 and committed an unpublished candidate.
2. Zai independently reviewed that exact candidate on chiap02.
3. A trusted external runner passed all 13 required tests.
4. Native guarded completion marked both cards DONE/PASS and released claims.
5. Eighteen later cycles showed no replay. Twenty observations showed that the
   other card's synchronization wait or refusal did not stop this card.

The source used 31 model requests, 718859 prompt tokens including 692736 cached,
and 7900 completion tokens. Review used 15 requests, 174628 prompt tokens
including 159232 cached, and 2990 completion tokens. Neither run recorded a
bridge failure or local budget refusal. These are actual usage observations,
not a throughput benchmark or a monetary savings estimate.

Earlier a31d4e54 and 41a6039b cycles needed operator recovery. Their checkpoints
explicitly label assistance and remain preserved. The Qwen 83b5ae19 trial was a
valid negative review. On 8abcfd10, the independent reviewer missed a concrete
scope error; operator inspection blocked acceptance before tests/completion.
Its original model PASS remains historical evidence, while source and review
are BLOCKED with correction 93fbd7c1 linked. The corrected prompt received a new
independent PASS and is the version installed. Review is useful, not infallible.

## Rollback and remaining boundaries

`GENERATION-INSTALL-MANIFEST.json` binds per-host preimages and private backup
locations. `deploy_generation_composite.py rollback` restores them under a short
manual maintenance HOLD with zero active runs and reservations. The gateway
installer retains both file preimages under its receipt's private backup path
and supports restoration with a managed restart and health/catalog checks.
Installation scripts retain failures and attempt exact rollback; unexpected
drift or host loss requires operator inspection, never a false success.

Fresh authority claim reads add roughly 2.6 seconds per request in the measured
three-sample diagnostic. Native completion still requires 17 guarded writes,
and synchronization can delay candidate handoff. These remain visible latency
costs. They were not bypassed with stale claim caches or weaker acceptance.
Private-environment qualification is required before further authentication
changes. Unqualified provider families remain excluded.

Historical Jarvis ownership is not blanket reassigned. New dispatched cards use
their workflow identities; completed and blocked crews release their own exact
claims. Dirty worktrees, active work and original 09cf4fec remain preserved.
Backlog admission requires an exact source, eligible dependencies, authorized
task scope and qualified test contract. Architecture approval alone is not a
deployment or corpus-ingestion approval.

Final native closures, fresh ownership census and maintenance release are linked
from the parent card's final external rollout checkpoint. This immutable report
does not claim those events before their receipts exist.
