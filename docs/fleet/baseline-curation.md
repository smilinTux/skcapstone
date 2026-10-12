# Baseline curation refusals

A failed qualification job does not necessarily mean pytest failed. Check the
job's exact sealed plan and execution host before changing a recipe. A new job
for the same card may name a different run.

The curator skips missing run artifacts so partial sync or a pre-execution
failure does not become a product refusal. It also skips checks that all exited
zero: stale plans, claim changes and custody failures require their own recovery.
Neither case authorizes deselection or a qualification PASS.

For an exit-2 collection failure, inspect pytest.log and receipt.json on the
execution host. Fix missing declared dependencies in the qualification extra
and regenerate the hashed runtime lock. Do not automatically exclude a module
just because a dependency is absent. For example, CapAuth's passkey tests need
both webauthn and soft-webauthn; excluding those tests would lose useful coverage.

Check the pinned repository's declared test scope too. CapAuth declares tests/.
Explicit pytest . used to import live Nextcloud scripts outside that scope;
the full-suite recipe now honors repository testpaths (PR #1139).
An operator can record a validated tests/ recipe through coord, with evidence
for any baseline exclusions, and read it back. The subsequent native
qualification must still run and validate the recipe on the pinned base.
