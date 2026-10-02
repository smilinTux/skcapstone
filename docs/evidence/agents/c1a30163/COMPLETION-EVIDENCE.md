# c1a30163 implementation evidence

Exact retained owner: `codex-review-brief-c1a30163`.
Claim: `465dba3262f944c8ba5b59c7aed96565`.
Authorized source base: `abe88b8cfe6ef9c864f2202bc62119ae40d08509`.
The native card description is this task's contract. This SKCapstone checkout
does not contain the SKLegal-specific `docs/tasks/SUBAGENT-TASK-TTDS.md` path.

## Change and acceptance evidence

The existing post-claim renderer selection now chooses a dedicated hosted
review brief for governed production reviewers without the source-only label.
The producer and source-only renderer implementations are unchanged.

The hosted brief selects the native repository CI contract, permits terminal
PASS or precise BLOCKED only, and invokes a compact handoff command in the
same reviewed module. The command preserves Git HEAD/tree and a clean worktree,
checks current ownership, claim and source bindings, verifies private owned
regular evidence, computes the summary digest, and writes matching evidence
aliases through native revision/claim guarded links with deterministic
transition IDs. It neither commits source/evidence nor completes/releases cards.
Source trees absent from typed hosted metadata are derived from the exact pinned
Git commit and checked again during handoff; explicit metadata remains binding.

Real rendered-brief tests execute against Git and a CLI stand-in backed by native
CardEventLog and LiveCardStoreGateway. Hosted SKLegal, GitHub SKCapstone, mirror
repository semantics, and a c162-shaped card without typed candidate_tree all
pass native read_card and validate_review_completion after handoff. Assertions
verify plain paths, identical digest aliases, repository-specific CI, last
terminal verdict and unchanged source. Negative cases cover missing, partial,
stale or unrelated CI, invalid decisions, dirty/committed source changes,
claim/source drift, unsafe evidence and concurrent native revision changes.
Existing producer/source-only execution and refusal tests continue to pass.

## Exact validation results

- `python -m pytest tests/fleet/test_production_source_review_brief.py tests/fleet/test_production_brief.py tests/test_review_verdict.py -q --no-header --disable-warnings`: **185 passed in 33.10s**, no skips.
- `python -m ruff check src/skcapstone/fleet/production_hosted_review_brief.py tests/fleet/test_production_source_review_brief.py`: **All checks passed**.
- `python -m black --check src/skcapstone/fleet/production_hosted_review_brief.py tests/fleet/test_production_source_review_brief.py`: **2 files unchanged**.
- `python -m compileall -q src/skcapstone/fleet/production_hosted_review_brief.py scripts/fleet/skfleet-rotate.py`: **exit 0**.
- `git diff --check`: **exit 0**.

The first three regression cases failed with the new renderer absent. The
compact entrypoint subsequently passed 184 tests; adding absent-tree coverage
produced the final 185-test result above. Formatting-only cleanup followed.

## Prompt size and read-only source check

The original generated c158 brief is 8,313 UTF-8 bytes. Retaining its generic
instructions while substituting the current c162 title, description and criteria
produces 7,698 bytes. The new brief using those same c162 fields is 6,193 bytes,
a 19.6 percent reduction. Executable validation code stays in the reviewed module.
Measurement used synthetic owner/claim values in memory and read-only exact Git
source `/home/skuser01/.skcapstone/fleet/workspaces/pi-codex-review-chiap08-c1a30158`.
The current c162 native card was neither claimed nor modified. The rendered
measurement is private `c162-rendered-size-check.txt` beneath this card's evidence
directory. A first read-only probe of the original shared SKLegal checkout
correctly refused because it lacked the exact f62f532 object; no source changed.

## Limitations and rollback

No installed script, service, remote repository, old evidence, source card or
held review card changed. No push or external action occurred. The new module
and rotation import must be installed together after exact independent review.
The CLI stand-in exercises native fold/readback and completion validation but
does not qualify the installed authority service or a real hosted worker run.
CI and provider truth still require independent controller verification; a prompt
and worker-supplied values cannot prove them. No schema/data migration applies.
Rollback restores the prior rotation script and removes the newly installed
module only through the controller's reviewed installation backup process.

Local candidate identity, bundle and private hashes are recorded externally after
the authorized commit to avoid self-referential committed evidence. Root retains
independent review, installation, acceptance and completion authority. This fresh
session has used zero compactions and preserved the prior helper handoff.

## Installed-probe correction: native elastic reviewer identity

Root's installed probe of the first candidate found that the native owner
`pi-codex-review-chiap08-c1a30162` was refused because the renderer omitted the
declared qualified seat used by native admission. Root reported exact rollback
under the generation lock, with c162 held and no worker affected. The prior
candidate and its independent PASS remain preserved as history, not acceptance
of this correction.

The correction applies the exact native elastic identity expression from
`assert_governed_review_claim`, then uses `qualified_reviewer_seats`,
`governed_review_seat` and `reviewer_candidate_reasons` without bypassing producer
independence or accepting an arbitrary declared seat. Named Seraph behavior is
unchanged. The two positive regression cases failed before the fix; afterward
10 identity cases passed, including malformed/wrong-card names, missing or
unqualified/multiple seats, exact producer and same-seat producer refusals.
Positive cases execute the rendered handoff and native readback against the
exact c162 owner with no typed candidate_tree.

Final command adds `tests/fleet/test_production_hosted_review_identity.py` to the
pytest command above: **195 passed in 41.87s**, no skips. Ruff and Black passed
for all three changed Python module/test files; compileall and diff checks passed.
A read-only render using the actual held c162 JSON and preserved source, with
the real elastic owner and a synthetic claim, succeeded and produced 6,199 bytes.
It derived the exact `07191d8da72209cf946507bfebfc97d8ff9c8827` tree without native
card mutations. This new candidate still requires root-controlled independent
review and installed qualification. No installed files were changed by this worker.
