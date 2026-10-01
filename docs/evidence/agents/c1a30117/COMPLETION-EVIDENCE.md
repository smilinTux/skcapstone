# c1a30117 candidate evidence

Producer: `codex-terminal-offer-c1a30117`.
Claim: `b15b627ed0764a7eba4157a14b55d612`.
Parent: `9b230773`.
Base: `22ad267d3beea32a9ef7206f80e265f2b85ba4b9`.
Scope: native terminal-offer retirement, local commit and independent review.
Installation and real retirement remain pending root-controlled review.

## Changed files

- `src/skcapstone/fleet/builder_retire.py`: exact controller guards and node
  preservation; immutable audit before scheduler pointer removal.
- `src/skcapstone/fleet/cli.py`: native `builder-retire` command registration.
- `tests/fleet/test_builder_retire.py`: 18 unit and integration tests.
- `tests/fleet/test_builder_retry.py`: existing source-refusal mock accepts
  the current keyword-only `retained_claim` argument and asserts it is true.
- `docs/fleet/terminal-offer-retirement.md`: operation, custody and rollback.
- `changelog.d/c1a30117-terminal-offer.md`: focused changelog fragment.
- This evidence file.

## Verification

`PYTHONPATH=src pytest -q tests/fleet/test_builder_retire.py
tests/fleet/test_builder_retry.py tests/fleet/test_builder_dispatch.py`:
**119 passed in 5.71s**, no skips or failures.

Ruff over the two runtime and two test files: all checks passed.
Black check over the same four files: all unchanged.
`git diff --check`: exit 0. Native CLI help exposes the exact hash guards.

Before the mock correction, this combined suite produced 112 passes and one
failure, `test_prelaunch_refusal_preserves_exact_status_and_claim[source]`.
That identical TypeError, unexpected keyword `retained_claim`, reproduced on
the untouched source base checkout. No runtime retry behavior changed.

Acceptance checks include exact hash refusal, changed or new claim refusal,
live and denied PID proof, source changes, accepted/review-pending/retryable
attempt refusal, native card lock, real sandboxed Git source inspection,
full staged/unstaged/untracked/ignored/index custody, serialization with a
concurrent offer, interrupted receipt recovery and immutable replay. Tests
confirm the native card and original node status are unchanged.

## Concrete production preflight, read-only

Target `a8300e02`, node `node-ziowk01`, old request
`0d75d1fd9678c4facc3e76af17c470afe7061bc02fe765b9bf22a51eaad45a26`.
Request SHA256:
`acf6cff10da0f34f4299e82b1a643829e37eb77971097df25989e07178d3d160`.
Status SHA256:
`58a1fe9a094c3b4fdc8ebe3e319ed2a5b5e1622dd60d34c5403a29e45fb94d71`.
Folded card revision:
`fc1937699a63add48f0d3973d4ef3ea271930c3168c7f66dcf2ee8951b24c9e7`.
Card is backlog and unclaimed. Source base remains
`14123f5e80d5311af3e2d43112e32b81fdb53260`.

Direct strict SSH read on ZIOWK01 found recorded PID `653499` absent and the
old 262 MB workspace present at the original base, with extensive staged,
unstaged and untracked changes. The earlier `7722064f` preservation predates
this request and is not accepted as custody for it. The new command must
perform its own current full preservation before retirement.

## Installation and rollback boundary

The installed fleet CLI, dispatcher, retry and source bundle modules matched
this source base byte-for-byte before edits. Installed fleet CLI preimage:
`1bc5a7cb76c0c313705d0e0e11c0f94d9cf8daefd1843e5512832c6ee4dee149`.
Install only the reviewed CLI delta and new module after exact backup and
preimage checking; do not overwrite separately updated coordination modules.
The node requires the reviewed new module too. Restore the exact CLI backup
and remove only the matching added module to roll back installation. Retain
every source archive and receipt. Installation rollback does not resurrect a
retired offer or overwrite a new generation.

No production request/status, source workspace, cooldown, claim or model
configuration changed while producing this candidate. No push, application
deployment, external legal action or protected corpus access occurred.
Archives remain on their originating node; this is not verified cross-host
source transfer or cleanup authorization. The bounded operation rejects
workspaces above 2 GiB and unsupported special files. Cooldown behavior is
unchanged. Independent exact-source review and controlled installation/use
remain outstanding.
