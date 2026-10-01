# Production builder source acceptance

Card: bd84af1e. Owner: codex-production-builder-bd84af1e.
Claim: 032cc63a9cca4fd58ac944f839ec17e4. Parent: 9b230773.

This is the second additive commit for this leaf. Its source-custody
prerequisite is dfebbd1da0c94f46e68bc0ce0dd0ce3478eec712, documented in
SOURCE-CUSTODY.md. Both commits require independent review and deployment
acceptance before this card may complete. No runtime has been changed.

## Changes

- Production builders share the native queue and the one validated policy.
  Legacy mode retains its four-worker node limit and existing behavior.
- The declared authority is the sole offer writer. Its offer exclusion
  makes CPU/RAM reservation accounting and request publication atomic.
  Ready status, cordons, taints, exact source/card generation and per-card
  native claim admission remain required. Unknown resource reservations
  fail closed for the affected node. No fixed provider or worker-count
  ceiling replaces actual resource admission.
- Native systemd user services enforce the node's configured CPUQuota,
  MemoryMax, TasksMax and RuntimeMaxSec on the actual worker. Exact request
  and attempt unit names plus invocation identity prevent ambiguous
  process state from releasing source custody.
- Model IDs come from the fresh gateway catalog through the shared
  resolve_production_routes helper. Card size, provider-only requirements,
  local/privacy tier, advertised tools/reasoning, enabled family and actual
  gateway request capacity qualify routes. The smallest adequate qualified
  size is selected; no static model table or extra Qwen size ceiling exists.
  Explicit qwen-first requirements remain enforced.
- Every worker uses supported explicit --provider skgateway --model exact-ID
  flags. A unique local Pi catalog binding must target the configured
  gateway, then the exact gateway route must pass the existing bounded
  preflight with matching backend attribution before claiming. Short shared
  process caches bound repeated catalog probes and preflight requests.
- Exact stopped producer proposals use the first commit's bounded source
  custody helper. Runtime state becomes awaiting-review or awaiting-evidence
  while the original source claim remains held. Verified terminal process
  evidence survives collection of its transient systemd unit.
- Worker prompts require the current card, repository AGENTS.md and relevant
  engineering/acceptance documentation, committed completion evidence,
  supported typed native verdict metadata and the verification triple.

## Tests and evidence

The combined source candidate suite passes 325 tests with zero failures or
skips, including 107 unchanged legacy builder tests and 95 artifact/wrapper
boundary tests. Ruff and git diff --check pass. Exact test command, log hash,
commit, tree and changed-file hashes are recorded in operator CANDIDATE.json.

New tests cover dynamic model size/tier/family eligibility, catalog ambiguity,
wrong endpoints, exact service argv, quota and policy drift, resource
reservations above the former count limit, stale node headroom, unit identity,
retained claims, exact native artifact transfer and wrapper exit behavior.
Git export/import tests use real temporary repositories and a real bwrap
namespace; service and SSH effects are mocked. Existing source-access probes
confirm actual read-only bwrap user namespaces on all six execution nodes.

## Remaining gates and rollback

Both source commits still require actual independent review. Root owns
composition, final tests, private file preimages, idle coordinated installation
and a real production trial. c199 tracks native node actuation, queue delivery,
private catalog custody and exact restricted SSH handler targets. All six
workers expose the required typed verdict fields; optional authority-only
CAS flags are not assumed in worker prompts.

Resource reservations deliberately subtract outstanding maximum quotas from
fresh allocatable headroom. This conservatively counts some live memory twice
until terminal proof releases its reservation. Measure that cost after safe
rollout; do not invent a fixed worker count as a substitute.

The tiny route preflight reports its actual served model/backend. It does not
prove every subsequent worker response's attribution. No protected Matter
access or egress authorization follows from provider qualification.

Incremental source bundle imports require the historical base to be available
through existing repository access. Unpublished baselines require separately
verified priming. Oversized or invalid bundles retain custody and refuse
handoff. A failed or incomplete producer requires explicit recovery; its
source bytes and native claim are preserved instead of replayed.

Rollback restores exact installed module, wrapper, policy and forced-handler
preimages under an idle maintenance window. Preserve native events, source
workspaces, immutable bundles, existing keys and unfinished claims.
# Production handoff integration corrections

The final integration corrections reuse the private gateway catalog adapter
without another models fetch, validate the selected route against that same
snapshot, and use the shared source worker brief remotely. BLOCKED instructions
require actual typed evidence and existing Git identities without authorizing a
new commit. Exact native BLOCKED release uses the native board/card lock order
and preserves source bytes and outcome history.

Validation: the 17-file focused boundary suite passed 367 tests with zero skips
in 12.97 seconds. Ruff and git diff --check passed. Tests include real private
catalog metadata projection and real native claim/release events, plus stale
claims, changed source binding/evidence, superseded outcomes, wrong owner,
unproven process death, retry recovery and retry exhaustion.

Local publication is bounded to three attempts, with 5 and 10 second delays.
Exhaustion retains the claim and workspace as awaiting-evidence; this patch does
not claim indefinite recovery of persistent infrastructure failures. Remote
pending publication continues through the existing native consumer reconcile.
No live deployment or restart occurred. Independent review and reviewed rollout
remain necessary. Rollback is the prior source version; already appended native
events and immutable artifacts remain preserved.
