# Native review gateway capacity candidate

Card: `f34f26c5`. Producer: `codex-native-review-f34f26c5`.
Base: `5f621b47bfe98efe4bdee59282cb06ba5c20bf7b`.
Branch: `work/f34f26c5-native-review`.

## Defect and change

Native review claims and gate diagnostics preferred an old host-specific route
snapshot with a physical limit of three, even when a newer gateway observation
existed at the fallback path. The installed legacy publisher wrote the fallback;
the newer source publisher wrote the host-specific path. Both could therefore
coexist indefinitely. Native evaluation also counted local worker occupancy as
provider request occupancy and accepted arbitrarily old sealed observations.

The reader now selects the newest bounded observation from the two existing
paths. A newer failed observation still wins over an older healthy one. The
existing seal must match, and its observation must be finite, not future dated,
and at most 120 seconds old. Malformed evidence fails closed.

New seals omit retired physical limits; the source publisher no longer supplies
one. Native admission uses gateway active requests and gateway configured limits.
Legacy physical arguments remain callable for rolling installation, but cannot
restrict native admission. Local occupancy stays in the sealed audit data and
diagnostic output. Exact source, claim, dependency, policy and independent-review
checks remain in place.

## Validation

The first targeted reproduction produced six failures and one pass before the
fix. The same defects now pass along with malformed-input and new-seal checks.

Command, with `PYTHONPATH=src` and the existing `.skenv` Python:

```text
python -m pytest -q tests/fleet/test_review_gateway_only.py tests/fleet/test_review_capacity.py tests/test_coord_gate_diagnostic.py tests/test_review_pool.py tests/test_skfleet_capacity_fill.py tests/test_skfleet_seraph_selector_e2e.py
```

Result: **80 passed, zero skips, 13.68 seconds**. Includes native CLI gate and
atomic claim tests, independent reviewer/source checks, gateway exhaustion,
tampered seals, stale/future/nonfinite timestamps, malformed evidence, and newer
gateway-failure selection. `git diff --check` passed.

A read-only live probe through the changed acquisition code queried the actual
gateway, producing 15 qualified route entries, no probe error, and an eligible
native result. The snapshot is retained separately under the operator evidence
directory with revision
`8353d3e9b6ab2a187f03309110bddab84e8168e4da25cf76bce3475611b7a03b`.
This is historical source qualification, not installed gate or worker proof.

## Installation and rollback requirements

This candidate has not been deployed or independently reviewed by its producer.
The root operator must preserve exact preimages of the installed
`skcapstone/review_admission.py` and `skcapstone/fleet/review_capacity.py`, install
the reviewed bytes together, then acquire and atomically publish an actual fresh
gateway snapshot with `acquire_review_route_snapshot` before native review gates.
The existing source API accepts the gateway URL, host-specific evidence path,
and unique cycle ID. Refresh before the 120 second freshness window expires.

Do not deploy the entire drifted legacy rotate script just for its one removed
publisher argument. The acquisition function already omits the obsolete field
for old callers. Rollback restores the two exact module preimages and preserves
all old/new route observations for audit. No card data migration is required.

## Remaining scope

Installed native gate proof and independent exact-candidate review remain with
the parent rollout. The legacy rotate dispatcher still has its own lane targets
and physical launch-selection helpers; Fiber does not use those helpers for
native claims. This change does not activate that dormant dispatcher. The root
card owns Fiber scheduling, node placement, service quotas and lifecycle work.
