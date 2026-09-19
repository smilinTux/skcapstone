# Safe Source Rollback: skfleet-rotate.py

Card: `ff68bade`

Repair card: `3c2ea0cd`

## Verified Baseline

- Governed repository: `smilinTux/skcapstone`
- Governed source: `scripts/fleet/skfleet-rotate.py`
- Known-good commit: `06dfa7eb76a9de3f321c884a31b862697b5493cd`
- SHA-256: `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e`
- Byte count: `74594`
- Repository tree mode: `100755`
- Installed mode observed on chiap08 on 2026-08-28: `0775`

The governed repository source predates the provenance repair card and is the
rollback authority. The installed copy is a deployment target, not a source of
truth. SHA-256 covers file bytes, not mode, ownership, or deployment state.

## Scope and Gates

This plan covers a source-only repair branch and pull request. It does not
authorize any installed-file change, launcher execution, live rotation,
service or timer action, deployment, merge, or direct update to `main`.

If the installed launcher differs from the verified source in bytes or mode,
stop and open a separately approved deployment repair. Preserve the observed
hash, mode, owner, and path as evidence. Do not overwrite the installed file.

## Read-Only Verification

These commands inspect the governed blob and installed file without executing
the launcher:

```bash
REPOSITORY="$HOME/work/skcapstone"
GOOD_COMMIT="06dfa7eb76a9de3f321c884a31b862697b5493cd"
LAUNCHER_PATH="scripts/fleet/skfleet-rotate.py"

git -C "$REPOSITORY" cat-file blob "$GOOD_COMMIT:$LAUNCHER_PATH" | sha256sum
git -C "$REPOSITORY" cat-file -s "$GOOD_COMMIT:$LAUNCHER_PATH"
sha256sum "$HOME/.local/bin/skfleet-rotate.py"
stat --format='%a %s %F' "$HOME/.local/bin/skfleet-rotate.py"
cmp -s \
  <(git -C "$REPOSITORY" cat-file blob "$GOOD_COMMIT:$LAUNCHER_PATH") \
  "$HOME/.local/bin/skfleet-rotate.py"
```

Expected byte hash: `36492d0530494fa7629fef26fa1b1394cec730a549632e774c15d2d979d4779e`.

Expected byte count: `74594`.

The installed mode is recorded separately because it is not part of the hash.

## Safe Source-Branch Rollback

Use a new branch from current `origin/main`. Restore only the launcher path from
the known-good commit, review the diff, and publish a normal pull request:

```bash
REPOSITORY="$HOME/work/skcapstone"
GOOD_COMMIT="06dfa7eb76a9de3f321c884a31b862697b5493cd"
ROLLBACK_BRANCH="fix/skfleet-rotate-source-rollback"

git -C "$REPOSITORY" fetch origin main
git -C "$REPOSITORY" switch -c "$ROLLBACK_BRANCH" origin/main
git -C "$REPOSITORY" restore \
  --source="$GOOD_COMMIT" \
  -- scripts/fleet/skfleet-rotate.py

git -C "$REPOSITORY" diff --check
git -C "$REPOSITORY" diff -- scripts/fleet/skfleet-rotate.py
pytest -q tests/test_rotation_launcher.py

git -C "$REPOSITORY" add scripts/fleet/skfleet-rotate.py
git -C "$REPOSITORY" commit -m "fix(fleet): restore governed rotation source"
git -C "$REPOSITORY" push -u origin "$ROLLBACK_BRANCH"
```

Open a pull request for independent review after the normal push. A reviewer
must bind the verdict to the exact branch head and confirm the launcher hash.
Merge and deployment remain separate human decisions.

## Immutable Evidence Contract

Publish evidence under a card-specific directory. Build the manifest only after
all artifacts are final. List every artifact in `SHA256SUMS`, set artifacts and
the manifest to mode `0444`, set the directory to mode `0555`, and attach the
manifest SHA-256 to the card's append-only evidence links.

Example artifact set:

- `repair-report.md`
- `test-results.txt`
- `source-hashes.txt`
- `diff.patch`
- `file-modes.txt`

Example finalization:

```bash
EVIDENCE_DIR="$HOME/.skcapstone/evidence/work/<card-id>"

cd "$EVIDENCE_DIR"
sha256sum \
  repair-report.md \
  test-results.txt \
  source-hashes.txt \
  diff.patch \
  file-modes.txt > SHA256SUMS
chmod 0444 \
  repair-report.md \
  test-results.txt \
  source-hashes.txt \
  diff.patch \
  file-modes.txt \
  SHA256SUMS
chmod 0555 "$EVIDENCE_DIR"
sha256sum SHA256SUMS
```

The final command supplies the manifest hash to bind to the card. The manifest
cannot contain its own hash, so the append-only card link closes that integrity
chain. Verification is read-only:

```bash
cd "$EVIDENCE_DIR"
sha256sum -c SHA256SUMS
stat --format='%a %n' "$EVIDENCE_DIR" "$EVIDENCE_DIR"/*
```

## Rollback of the Source-Branch Proposal

If the source-only proposal is wrong, close the pull request or add a normal
revert commit on its branch. The proposal has no installed or service effect,
so no live rollback action is required.
