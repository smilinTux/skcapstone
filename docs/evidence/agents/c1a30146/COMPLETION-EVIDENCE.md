# c1a30146: one fresh store per acceptance inspection

Owner: `codex-store-reuse-c1a30146`. Claim:
`9be98398890f48fba3fdb3ca48e3266b`. Dependencies c1a30141 and c1a30143
were read back DONE. No matching task TDD exists in SKLegal's task document;
the exact native card supplies this bounded implementation contract.

## Change and custody

Base is `12d61d56870c5081252033f54ba9d1344846bffc`. Before editing the
gateway, its exact reviewed installed bytes were recovered from
`3b36c0333161e9e03c6d38695bf5ed388c9db6c1:src/skcapstone/seraph_review_cardstore.py`
and verified as SHA-256
`20a7e63b33c93d35987b5a05054bb5403db01abb86f89a407cf31c06c90140fc`.
This restores the already qualified evidence alias and replacement predecessor
handling absent from the base, as pinned by the c126 authority manifest.

The new delta adds a private optional `_store` argument to gateway `read_card`
and passes the store already constructed inside `native_state`'s mutation
lock. Default gateway calls still construct a fresh store. No store is saved
on the gateway or shared across calls, mutations, acceptance steps or cycles.
Sibling enumeration, predecessor authorization, outcome, source, claim and
revision checks remain unchanged.

Changed files are the gateway, acceptance finish module, their two existing
test files, the c146 changelog fragment and this evidence document. No
installed modules, profiles, timers, scheduler controls or matter data changed.

## Verification

Private evidence: `~/.skcapstone/evidence/work/c1a30146/`.

- Authority composition: **37 passed, 1 deselected in 44.49s**. It loads only
  the two candidate modules over the qualified installed authority dependency
  set. The runner verifies the c126 manifest and installed c143 finish hash
  first; module paths and hashes are retained in its composition inventory.
- Four new tests prove one legacy load instead of two, equal complete native
  snapshots, real lock scope, native and legacy mutations visible on the next
  inspection, and unchanged sibling conflict/predecessor helper decisions.
  The predecessor helper is explicitly doubled in the unit test; actual
  predecessor validation is exercised by the retained authority replay below.
- chiap02 source-only historical Python/Node and Node-profile checks:
  **119 passed, zero failures/skips, one environment warning in 27.05s**.
  The warning is pytest's missing asyncio plugin configuration support; these
  tests are synchronous. Exact source commit
  `ac52861219c0b790297575c496437784a6758bd1` and tree
  `f436c517a8cdc22df4bd4d577590347192b5240d` were transferred in a verified
  Git bundle, checked out separately and verified clean after tests. A fresh
  read measured 24 CPUs, 11.6 GiB available RAM and load 1.50, with no active
  builder units. Test quotas were CPU 200%, RAM 2 GiB, tasks 128, runtime 180s.
  No packages or modules were installed. Remote imported-module hashes are
  retained. This is source-only coverage, not native authority qualification.
- Ruff on all four changed Python files, whitespace and changelog checks pass.

The first local test attempt stopped on four new fixture setup failures:
missing synthetic home directories and governance rejection of incomplete
replacement fixtures. A second attempt exposed the fixture's absent parent.
These fixture errors were corrected using ordinary synthetic cards, native
label events and an explicit predecessor double; the final tests pass.
Initial logs are retained. No production guard was relaxed.

The historical full-completion fixture remains explicitly deselected:
`test_real_guarded_native_completion_review_before_source_and_replay_without_writes`.
Its existing minimal test binding lacks exact historical source binding,
as documented and measured in c143. No fabricated proof or bypass was added.

## Bounded authority measurements

`PROFILE.json` compares the installed c143 finish plus reviewed gateway with
the candidate over the same two actual cards. All native snapshot fields
match exactly, including current and predicted completion revisions.

| Card | Before seconds | After seconds | Legacy loads | Sibling folds |
| --- | ---: | ---: | --- | --- |
| a8300e02 | 2.1309 | 1.3554 | 2 to 1 | unchanged |
| ac8b9cbd | 7.4494 | 3.3424 | 2 to 1 | unchanged, 8074 total folds |

These are one bounded cProfile comparison per card, with warm-read and live
host contention effects; they establish the removed load, not a throughput
guarantee. The full-board sibling scan is deliberately preserved.

Candidate replay of retained ac8 acceptance succeeds with all 481 tests and
zero failures, errors or skips, without commands, mutations or test reruns.
The separate d570 replay still refuses `replacement predecessor history
changed`. The audit guard recorded zero prohibited attempts. Retained plan,
raw test outputs, reports, decisions and acceptance artifact hashes, plus
installed and candidate source hashes, are unchanged.

Only in this diagnostic process, lockfile creation was replaced with
`nullcontext`; this avoids coordinator interference but makes no atomic
snapshot or lock timing claim. Real lock scope is exercised in unit and
guarded native integration tests.

## Review and rollback handoff

Root owns independent review acceptance, any installation and runtime
qualification. The private descriptor supplies final commit/tree/ref, file
hashes, verified bundle, exact review context and evidence hashes. The claim
is retained; no completion or release is asserted by this source worker.

The only prospective installed targets and required preimages are:

- `seraph_review_cardstore.py`:
  `20a7e63b33c93d35987b5a05054bb5403db01abb86f89a407cf31c06c90140fc`.
- `fleet/production_review_finish.py`:
  `a3a01a1b2a55a0f96b454e9f8d7a91eb0ad4a7b862caae0c1f45d3e6a6340724`.

Root must back up those exact bytes and modes before installation, recheck
the independently reviewed postimages, and preserve current custody. Rollback
restores both recorded preimages and modes, then verifies runtime fingerprint
`7bd350f243b8a3917271681bc442456d71474899b849b97774086de9fd49dcf8` and the
retained read-only replay. No data migration or card rewrite is needed.
