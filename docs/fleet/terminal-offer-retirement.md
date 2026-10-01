# Exact terminal offer retirement

Card `c1a30117` adds a controller operation for a request whose terminal
generation blocks a new offer despite its card being unclaimed. It does not
approve the old work or alter the source contract, retry bound, cooldown,
quota, node status, claim, outcome or scheduling policy.

Run `skcapstone fleet builder-retire CARD --node NODE --request-sha256 HASH
--status-sha256 HASH --card-sha256 HASH --agent ACTOR --reason TEXT` on the
configured production authority. Request and status hashes address the exact
file bytes. The card hash is the existing native `card_revision` of the folded
card. The production policy and authority environment must be configured.
The same reviewed module must be installed on the named node. The default
performs read-only qualification. Add `--apply` only after independent review
and controlled installation of the exact candidate.

The authority takes the existing production offer exclusion, exact request
lock and native card mutation lock. It requires a backlog, unclaimed card
with unchanged revision, labels and source contract. Only blocked, stale or
retry-exhausted failed attempts qualify. Running, accepted and review-pending
attempts cannot be retired. Node identity comes from the qualified production
node binding, and SSH retains strict host-key verification.

On the actual node, the recorded PID and start token or production unit must
be dead. An unavailable proof fails closed. Existing sandboxed Git inspection
requires the requested source base to be an ancestor of workspace HEAD. The
complete workspace is inventoried and archived, including `.git`, index,
staged/unstaged/untracked files, ignored dependencies and symlink metadata.
Symlink targets are not read. Special files, foreign ownership, redirected
roots, more than 2 GiB of source, changed inventories and conflicting archive
replays fail closed. Original workspace bytes remain in place.

Private node custody lives under
`evidence/work/CARD/terminal-offers/REQUEST/workspace.tar.gz`, with an exact
inventory and copies of the original request/status. The authority retains
the request, status and attributed receipt in the equivalent private path.
Directory mode is `0700`; private files are `0600`. The archive remains on
its originating node. This operation does not establish cross-host source
transfer, and neither the workspace nor its archive may be cleaned up on
the strength of this receipt alone.

Only after source custody and repeated card/hash checks does the controller
unlink the active scheduler request. The old node status remains unchanged.
A durable receipt precedes unlink, so repeating the exact command can finish
an interrupted removal. Conflicting bindings and new active requests refuse;
an exact completed replay reports `already-retired`. Fresh work proceeds only
through the existing admission, claims, route, quota, tests and review gates.

Installation changes only `fleet/cli.py` and adds `fleet/builder_retire.py`.
Require exact CLI preimage comparison, retain a private backup, install on
the authority and affected node, verify installed hashes and CLI help, then
run check-only before apply. Roll back installation by restoring that exact
CLI backup and removing only the added module if its installed hash still
matches. Preserve all retirement receipts and source archives on rollback.
Once retirement has run, installation rollback does not resurrect the old
offer or overwrite a newer generation. No request/status or board JSON edits
are part of the operational rollback.

Validation: `PYTHONPATH=src pytest -q tests/fleet/test_builder_retire.py
tests/fleet/test_builder_retry.py tests/fleet/test_builder_dispatch.py`.
Coverage includes a real native card and sandboxed Git source, full archive
contents, process refusal, stale and changed claims, retry bounds, concurrent
offer exclusion, interrupted retirement and immutable replay.
