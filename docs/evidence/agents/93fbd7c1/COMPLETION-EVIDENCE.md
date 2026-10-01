# Shared worker evidence guidance

Card `93fbd7c1`, producer `codex-evidence-prompt-93fbd7c1`.
Pinned source base: `623fb325ea0b73371c337d978747709ef07ef104`.

Only `task_prompt` in `ops/fiber-f1be0929/fiber_sandbox.py` changes.
Both implementation and review prompts now explain that the report must not
embed its own final commit, tree or report hash. The external controller
binds those after the local commit; final Git output belongs in the handoff.
Optional historical references require actual Git output and accurate
labeling, without presuming a historical hash equals the workspace source.
Needed identifiers come from trusted card/controller values, and the current
task claim is distinguished from any separate source producer claim.

`review_prompt` is byte-identical to the pinned base. Its exact source-head,
report-digest, evidence-only scope and local review-commit requirements remain
intact. The rejected `8abcfd10` candidate was not merged. This correction
retains its parent linkage for attribution without importing that code.

Verification command:

```text
PYTHONPATH=src:ops/fiber-f1be0929 ~/.skenv/bin/python -B -m pytest -q -ra ops/fiber-f1be0929/tests/test_fiber_sandbox.py::SandboxTest::test_review_prompt_preserves_boundaries_and_requires_typed_proposal ops/fiber-f1be0929/tests/test_fiber_candidate.py::CandidateTest
```

Result: **16 passed, 0 skipped, 0 failed, 0.08 seconds, exit 0**.
These existing tests cover the review contract and external candidate
binding; no test that merely matches the new prose was added.
Generated implementation and review prompts were inspected, and an AST-based
source extraction confirmed the existing `review_prompt` bytes are unchanged.
`git diff --check` passed.

External artifacts are retained under
`~/.skcapstone/evidence/work/93fbd7c1/`: `boundary-tests.log` and
`generated-prompts.json`. Test-log SHA-256:
`10a3c3900c14644a7f93086c8ae8ddc17fda032f69e2ff28ee25f490da24aa1b`.

No model calls, runtime installation, original-card mutation, push or merge
occurred. Prompt clarification cannot guarantee model compliance; external
evidence verification remains mandatory. Independent exact-version review
and root composition/install remain pending. No data migration or deployed
rollback was needed. The external candidate descriptor binds the final
commit, tree and evidence bytes after commit; the native claim is retained.
