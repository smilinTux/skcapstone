# Qualification evidence

SKCapstone provides fail-closed building blocks for three recurring release
problems: freezing exact source for independent review, retaining sealed review
artifacts outside temporary storage, and auditing exact Git dependencies without
weakening registry hash enforcement.

## Immutable review checkpoint

Create a checkpoint from an explicit set of regular files:

```bash
skcapstone qualify checkpoint-create \
  --workspace /path/to/project \
  --output /path/to/review-checkpoint \
  --path src/example.py \
  --path tests/test_example.py
```

The result contains a sorted paired SHA256 inventory, a closed manifest, and a
read-only exact source snapshot. Absolute paths, escapes, symlinks, duplicates,
missing files, and nonempty output directories fail closed.

Reviewers verify the current workspace and record independent dispositions:

```bash
skcapstone qualify checkpoint-verify \
  --workspace /path/to/project \
  --checkpoint /path/to/review-checkpoint

skcapstone qualify checkpoint-review \
  --workspace /path/to/project \
  --checkpoint /path/to/review-checkpoint \
  --reviewer reviewer-name \
  --disposition accept
```

Completion requires accepted receipts bound to the exact inventory. Only
explicitly allowlisted evidence files outside the accepted source inventory may
differ from the accepted workspace. An accepted source path can never be
reclassified as evidence. The completion receipt records each evidence-only
difference and binds every review receipt digest. Inventory and receipt files
cannot include themselves.

```bash
skcapstone qualify checkpoint-complete \
  --workspace /path/to/project \
  --checkpoint /path/to/review-checkpoint \
  --evidence docs/evidence/completion.md
```

These commands never commit, push, deploy, change board state, or mark a card
Done.

## Durable review artifacts

`artifact-ingest` accepts a closed `skcapstone-review-artifact/v1` manifest,
verifies every listed path and digest, rejects ambiguous JSON, symlinks, binary
or protected-document formats, raw credentials, and private-key material, then
copies the bundle to a content-addressed directory:

```bash
skcapstone qualify artifact-ingest /tmp/completed-scan \
  --sink ~/.skcapstone/agents/$SKAGENT/evidence/artifacts
```

The durable receipt preserves the producer and version, accepted source digest,
disposition, retention policy, and exact file inventory. Accepted, rejected,
and unsealed states remain distinct. Re-ingestion is idempotent and verifies the
existing stored manifest and every file before returning it. Stored bundles are
read-only. Retention or deletion is an explicit operator workflow outside the
ingest command.

Never put credentials, tokens, signing keys, passphrases, client corpus, or
other protected content in a review bundle.

## Exact Git dependency audit

`audit-vcs` separates two different claims without weakening either:

1. The exported requirement, lock record, installed distribution, canonical
   HTTPS Git URL, full immutable commit, and installed version must all match an
   explicit policy.
2. Registry and transitive requirements remain under `pip-audit --require-hashes`.
   Each approved Git distribution receives a separate release-service query by
   canonical package name and version with `--no-deps --disable-pip`.

Policy JSON is a list of exact records:

```json
[
  {
    "name": "capauth",
    "canonical_url": "https://github.com/smilinTux/capauth.git",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "version": "0.3.1"
  }
]
```

```bash
skcapstone qualify audit-vcs \
  --requirements /tmp/requirements-hashed.txt \
  --lock uv.lock \
  --policy vcs-audit-policy.json \
  --output /tmp/vcs-audit
```

Zero, one, or multiple approved Git dependencies are supported. Any branch,
tag, short commit, alternate URL, embedded credential, duplicate package,
unapproved Git dependency, installed-version drift, lock drift, omitted
dependency, or unhashed registry requirement fails before vulnerability audit.
The prepared split files and plan are digest-bound, rechecked immediately before
execution, and copied to a private execution directory. Mutable plan files,
unsafe output links, missing audit executables, malformed dependency records,
and credential-bearing runner output fail closed or are sanitized before the
receipt is written. Screening includes nested OAuth client and refresh tokens.

Release vulnerability services identify Git-installed projects by published
name and version, not by commit bytes. The separate release query therefore
does not independently attest the commit. The exact requirement, lock, install,
URL, and commit checks provide that identity boundary, and the receipt states
this limitation explicitly.

## Coordination lifecycle projection

`skcoord` owns the lifecycle reconciliation engine. SKCapstone exposes it while
preserving the existing CardStore reconciliation command:

```bash
# Existing CardStore versus legacy-board convergence
skcapstone coord reconcile
skcapstone coord reconcile --apply

# Card lifecycle versus mutable agent projection
skcapstone coord reconcile-agents
skcapstone coord reconcile-agents --repair --agent operator
```

The agent audit is read-only by default. Repair is explicit, serialized, and
uses a durable intent followed by a committed receipt. Projection writes are
rolled back if convergence or receipt completion fails. A later repair resolves
an intent left by process death, and linked receipt files are rejected. Review
keeps an accountable owner claim but clears active execution.
Done clears claims/current work and records historical completion. Reopening
removes stale completion state. Active ownership conflicts fail closed; stale
orphan and non-owner claims can be repaired without forging agent liveness.
