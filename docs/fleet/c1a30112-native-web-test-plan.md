# Native web test qualification plan

Card c1a30112, owner codex-native-web-tests-c1a30112, claim
ff7ecd19724e43ed9e804df9883229c8. Source baseline
e9ee44cd1ca43aebd0b947a0c6d7afb26d2ef143 matches all four installed test
modules byte for byte. Preserve the existing Python profile and native
sandbox; add only the Node test contract needed by frontend cards.

1. Add a strict Node/Vitest profile and fixed test, typecheck and lint
   commands in production_test_profile.py. Bind exact card criteria, runtime,
   package configuration, lock and qualified dependency artifact identities.
2. Extend production_test_plan.py with exact dependency validation and
   per-file Vitest JUnit checks. Reject absent, duplicate, unexpected,
   skipped, failing or undercounted test evidence.
3. Reuse production_test_worker.py and production_tests.py for read-only
   source/runtime/dependency mounts, private scratch/output, quotas and
   existing native service custody. Retain truthful structured failures.
4. Run focused Python regressions, then actual bounded synthetic Node
   positive and negative qualification. Test drift, write/network denial,
   failed tests and timeout or missing results. Use existing pinned tools;
   do not fetch dependencies during candidate execution.
5. Freeze a local commit with exact hashes, tests and operating instructions
   for independent review. Parent installation requires backups and a
   verified rollback. Qualifying this runner does not satisfy a8300e02's
   separate scanner behavioral and mutation criteria.

No new executor, generic model-selected commands, candidate network, host
credentials, Docker socket, protected matter content, push or merge.
Do not change the frozen SKLegal c322 candidate or preserved worker sources.
