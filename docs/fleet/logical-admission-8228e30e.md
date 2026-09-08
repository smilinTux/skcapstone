# Logical fleet admission repair

Card: `8228e30e`. Source-only repair of PR 515, reviewed under `3458ce15`.
Parent: `281853e23add7aad3b43c2c9e1f93d96b5bed36e`.

## Contract

Ordinary cards request `sk-s`, `sk-m`, `sk-l`, or `sk-xl` in folded labels or
the corresponding `[S]`, `[M]`, `[L]`, or `[XL]` title marker. No size defaults
to `sk-m`. Conflicting size declarations deny admission. The same size selects
the model during health acquisition, dry-run reporting, and immediate preclaim.

Existing provider-only and escalation contracts remain explicit exceptions.
`qwen-first` remains exclusive until the existing hash-bound semantic completion
rule releases it. `qwen-only`, `sovereign`, and `qwen-sovereign` explicitly pin
Qwen. `qwen-suitable` and a suitable title alone never authorize Qwen. No Qwen
model, endpoint, capacity-domain binding, target, or completion rule changes.

## Deployment binding

`SKFLEET_BUCKET_PROFILE` is a version 1 JSON object. With no configured profile,
each ordinary bucket considers `glm`, `kimi`, and `codex`, in that order. Kimi
retains its existing zero-slot default. A supplied profile must declare all four
buckets, each as a list of existing worker transport names. Empty lists disable
a bucket. Unknown names, duplicates, malformed JSON, unsupported versions, and
Qwen mappings fail closed. Invalid configuration never falls back to defaults.

Example deployment profile, inert until separately approved for installation:

```json
{
  "version": 1,
  "buckets": {
    "sk-s": ["glm", "kimi"],
    "sk-m": ["kimi", "glm", "codex"],
    "sk-l": ["codex"],
    "sk-xl": ["escalate"]
  }
}
```

Transport names describe existing worker plumbing, not card compatibility.
Deployments bind GLM and Codex through their existing per-size model settings,
and Kimi through its existing coding/K3 ladder. Astra can use the existing
`escalate` transport by explicitly mapping a bucket to it and configuring
`SKFLEET_ESC_MODEL` and `SKFLEET_ESC_CAPACITY_DOMAINS` to the qualified Astra
model and domain. There is no implicit Astra qualification or new worker type.
Such model and domain configuration is outside this source-only repair.

Binding does not grant admission. The selected lane must exist, have a free
slot, and pass the same-cycle exact-model health predicate. Missing health
defaults to denied. Profile preference orders only qualified free lanes.
Selection and immediate preclaim share the binding function and frozen profile;
preclaim folds current card labels and recomputes the model and health again.

The Kimi domain remains `kimi-coding,kimi-k3`. Snapshot expiry, endpoint,
revision, cycle, model, capacity-domain, observation, queue, and quarantine
checks remain in the unchanged `fleet_lane_health.py` implementation.

## Verification and rollback

`tests/test_fleet_logical_routing.py` covers every bucket against GLM, Kimi,
Astra, and Codex deployment fixtures for healthy, unknown, stale, malformed,
unreachable, unauthorized, unhealthy, and quarantined evidence. Existing lane
affinity and health tests retain explicit restrictions and completion behavior.
The former fixed `/tmp` snapshot test now uses pytest `tmp_path`.

Run the focused and broad fleet/scheduler tests, pinned CI Black and Ruff
scopes, new-module typing and inherited mypy parity, compilation, Gitleaks,
binary patch reconstruction, and reverse-patch tree verification. Seal exact
candidate and evidence hashes and create a separate `sk-m` independent review.
Do not self-review or install this candidate.

Reverse the repair patch in an isolated checkout to return to the PR 515 parent
above, preserving that parent's Kimi correction. Reversing the cumulative patch
to `7e60e31683630526642d5983aae5d18600205035` also restores its known Kimi
availability defect. No runtime or data rollback is needed: no installation,
service action, worker launch, provider call, credentials, protected Matter,
database, or Inbox access is authorized or performed.
