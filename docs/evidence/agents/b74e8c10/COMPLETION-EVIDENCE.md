# b74e8c10 review evidence commit contract

Owner: codex-review-contract-b74e8c10.
Exact base:0921e811c24b1f6f84251bc60a1c797423a2b886.

The real review24b6d4b8 returned a Zai PASS proposal but left its review report and decision uncommitted. Its native card only permitted a commit, while the generic worker contract requires an explicit request. The candidate collector correctly refused the dirty workspace. This repair makes both the sandbox review prompt and the generated native guarded review card explicitly require a new local evidence commit before handoff, for any verdict. It requires a clean git status, restricts the commit to that review card's evidence directory, prohibits source/test modifications and pushes, and preserves the reviewed producer source_head separately from the reviewer evidence commit.

Changed implementation files:ops/fiber-f1be0929/fiber_sandbox.py and src/skcapstone/link_review_work.py. Regressions cover both actual generated contracts and the real sandbox collector's refusal of uncommitted review evidence. Existing review creation, claim/source identity, concurrency, idempotency, review independence and candidate verification behavior remain intact. The current review24b6d4b8 and worker were not modified.

Validation:

- Before implementation, the two strengthened contract regressions failed on the missing mandatory-commit instruction:2 failed.
- PYTHONPATH=src SKFLEET_LIVE_SANDBOX_TESTS=1 /home/skuser01/.skenv/bin/python -m pytest -q ops/fiber-f1be0929/tests/test_fiber_sandbox.py ops/fiber-f1be0929/tests/test_fiber_candidate.py tests/test_guarded_review_work.py tests/test_link_review_work.py:77 passed, zero skips,2.67seconds. Live sandbox collector checks reject dirty review evidence and producer-source changes, then accept the clean evidence-only commit.
- Ruff check and Ruff format --check passed for the changed native source and native test module. git diff --check passed. A final formatting-only adjacent-string split preserves the exact prompt bytes.

Acceptance limits:independent exact-candidate Zai review and parent composition/deployment are still required. Recovering the existing interrupted trial does not establish automatic success. A fresh Qwen documentation trial must prove the repaired handoff without operator intervention. No protected Matter, credential, gateway configuration, remote push or merge occurred.

Rollback:restore the two implementation files from the exact base through the parent's backed-up deployment process. Keep candidate/review/claim evidence and interrupted-trial artifacts. No data migration or ownership transfer is part of this change.
