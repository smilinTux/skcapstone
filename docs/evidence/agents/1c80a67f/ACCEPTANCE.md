# Fresh gateway production route resolution

Card: 1c80a67f. Owner: codex-production-routes-1c80a67f.
Claim: 8a486439f661470f8752bebd12f5e7c9.
Base: 53514f6e3cf5709e2835c1048111720f66a3c2c7.

The production policy contains authority, gateway origin, enabled provider
families, cycle budgets and per-worker resource limits. Each non-Kimi family
has only boolean `enabled` and `provider: skgateway`. Kimi remains disabled.
Static `model` fields fail validation, preventing a second model catalog.

`resolve_production_routes(routes, *, policy, required_size, labels, lane=None,
model_pin=None)` consumes rows from the caller's current acquired and sealed
gateway snapshot. It never fetches, caches or invents freshness. Existing native
gateway eligibility checks size, local-only privacy, health and active request
capacity. The resolver also requires an enabled family with consistent typed
provider/backend attribution, honors current family-only labels, and returns
exact route rows plus semantic `family` and canonical policy `lane`.

Logical card buckets and exact gateway IDs may differ. Both are preserved.
Nested gateway metadata is copied without changing capability limits. New
qualified gateway IDs become usable without editing production policy.
The optional exact model pin is an API constraint only: this leaf introduces
no new card metadata field. An absent pinned model never creates a route.

Independent review reuses the same enabled-family qualification and excludes
the actual producer family. Unknown or contradictory producer evidence still
refuses admission. Qwen replicas remain one family. Claim custody, activation,
source versions, and per-worker cgroup limits remain separate existing gates.

Validation command:

```text
PYTHONPATH=src python -m pytest tests/test_production_policy.py tests/fleet/test_production_dispatch.py tests/fleet/test_production_review.py tests/fleet/test_review_capacity.py -q
```

Result: 117 passed, zero failed or skipped. Tests exercise changing catalog IDs,
disabled families, backend mismatch, inadequate sizes, private cards, exhausted
request capacity, conflicting provider pins, exact missing pins, independent
review, immutable metadata and actual wrapper refusal before dispatch.

This is an isolated source candidate. Parent integration must pass the current
snapshot and exact card requirements to every launch and receipt check. Existing
dispatcher callsites are updated separately; no runtime or configuration was
installed by this leaf. Reverting this commit removes only these source changes.
