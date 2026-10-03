# Qualified profile generation implementation plan

Goal: append source-claim-bound qualifications after runtime changes without rewriting historical evidence.

Architecture: keep the original card profile immutable. Store each successor under the exact predecessor byte hash using the existing exclusive writer. One bounded reader resolves the full chain for current admission and embedded historical profiles for sealed plans. Runtime validation remains mandatory.

1. Add shared chain resolution and operator supersession in production_test_profile.py. Check current source custody under the native card mutation lock and explicit expected runtime before appending.
2. Route preflight, candidate sealing and sealed-plan reads through the shared resolver.
3. Test immutable predecessors, competing successors, wrong claims/hash/runtime, corrupt chains and preserved sealed plans. Run focused profile and test-boundary suites.
4. Preserve a source-only commit and bundle for independent review. No installed or live profile modifications.
