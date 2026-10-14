# Preserve an unreachable source base

Run `skcapstone fleet preserve-source CARD --agent lumina-nor` from the operator host.
The command searches bounded local workspace roots and the card's source bundles.
Use repeated `--search-root PATH` options to supply exact additional clones or bundles.
It never scans `/mnt` or follows directory symlinks.

The card must be unowned and open, with an exact commit and an unchanged skgit
repository binding. The command publishes `refs/heads/preserve/CARD-SHA8`, proves
fetch and ancestry through the same sanitized strict SSH transport as source-ref
amendments, and writes only `base_ref` through audited `coord link`. It reads the
card again before and after the write and repeats the remote proof.

Publication is compare-and-create: Git's empty expected-ref lease requires the
remote ref to be absent. It cannot update even an existing ancestor. An existing
identical ref is an idempotent success; any differing ref refuses. No ref is
force-updated, deleted or rewritten. Only the exact commit's reachable objects
are copied into a temporary bare publishing repository.

A failed proof leaves the card unchanged. A card change during publication leaves
an immutable preservation ref but refuses the binding write. Retry after inspecting
the reported change. The command does not change repository, revision, criteria,
verdict, ownership, or lifecycle. GitHub-to-skgit repository migrations require a
separate governed binding correction; a skgit ref cannot repair a GitHub binding.
