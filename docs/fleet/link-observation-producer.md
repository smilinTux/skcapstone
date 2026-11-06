# Mediated Link observation producer

The producer is separate from the Link seat. It uses read-only GitHub
and private SKGit connectors, but Link never receives either connector, its token,
or its process environment.

The producer requires a lineage manifest with schema
`skfleet.link-lineage/v1`. Each open PR must map to an exact source card,
card-generation, review-card ID, review-card revision, and terminal review
verdict. Reviewer candidates
must carry their exact identity, host, session, and workspace. Missing lineage
is incomplete evidence, not an orphan that can become healthy by inference.
The dry-run reconciler reads the explicit reviewer authority at
`~/.skcapstone/identity/reviewer-candidates.json`; that source must contain
the independent reviewer seat and its lowercase SHA-256 public-key fingerprint.
Jarvis is never a lifecycle reviewer candidate.

On success, the producer writes the canonical
`skfleet.link-observation-feed/v1` feed with an atomic same-directory replace.
It includes the connector source revision and evidence SHA-256. Connector
failure, stale snapshots, malformed PR data, duplicate PR keys, or incomplete
lineage never replace the last valid feed. Incomplete runs write only a
`.blocked.json` diagnostic beside the target.

The producer has no merge, deployment, release, card-claim, or fleet-mutation
path. `skfleet-link-producer.timer` refreshes lineage and then attempts feed
publication every five minutes, starting four minutes before Link's initial
cycle. It runs as the separate `link-producer` identity. Incomplete lineage is
a bounded diagnostic outcome: it preserves the last valid feed and does not
stop Link's independent review-work fallback. FAIL and BLOCKED outcomes remain
visible to Link but cannot satisfy merge eligibility, which requires exact
independent PASS.

The service supplies the exact lifecycle repository scope through
`SKFLEET_LINK_REPOSITORIES`, and the producer requires that environment to
equal the estate's declared scope exactly, optionally plus the private route
`https://skgit.skstack01.douno.it/smilinTux/sklegal`. The environment is what
the producer actually fetches, so an environment narrower than the declaration
would publish a feed that looks complete while ignoring whole repositories.
The producer fails closed before connector access when this setting is empty,
malformed, duplicated, missing a product, or contains any other repository.
Missing or malformed checks remain unknown.

An estate declares its own scope in `<home>/coordination/link-scope.json`, a
`skfleet.link-scope/v1` document:

```json
{
  "schema": "skfleet.link-scope/v1",
  "repositories": [
    "smilinTux/skcapstone",
    "smilinTux/skdashboard",
    "smilinTux/skworld",
    "smilinTux/sk-standards"
  ]
}
```

With no such file the packaged scope applies, which is those same four GitHub
repositories, so a node that has declared nothing behaves as it always has.
A file that is present but malformed fails closed with
`repository_scope_unreadable` rather than falling back: quietly substituting
the packaged default for a scope the operator believes is in force is worse
than a hard failure. The scope used to live only in the producer script, which
meant any estate watching a different set could not be configured at all.

Bounded exclusion records live in
`<home>/coordination/link-lineage-exclusions.json`. The producer passes that
file to `link-lineage.py --exclude` when it exists and omits the flag when it
does not, so an estate that excludes nothing is unaffected. The file's contents
are an operator decision: `docs/fleet/link-lineage-reconciliation.md` forbids
excluding a PR by inference from title, age, author or repository history, and
each record is bound to the PR's current head and base SHA, so an exclusion
lapses as soon as the PR moves.

The service template includes the private route and reads the optional producer-only
`%h/api-keys/link-skgit.env` environment file. Its
`SKFLEET_SKGIT_READ_TOKEN` grants read access only to the exact private route;
it must never be configured on the Link seat. The environment file must be owned
by the operator and readable only by that account. Without usable credentials,
private observation fails closed and preserves the previous feed. Updating this
template does not activate or qualify the live service.

Review lineage binds the forge-qualified repository, PR number, and exact candidate
head. Review cards may pin the head in `links.commit`, `links.head_commit`,
`links.head`, or `meta.link_head_revision`; conflicting pins fail closed. A bare
PR number cannot bind a terminal review. Private source discovery requires explicit
repository metadata or an exact card reference in the PR body. Legacy unscoped
GitHub title discovery is accepted only when that PR number occurs once across the
observation set. Exclusions use `repository#number` keys for private or colliding
PR numbers. Card PASS evidence and a published feed do not constitute an actual
forge approval or authorize a merge.

The Seraph private-forge approval publisher was removed in the SKFLEET cut;
there is no automated independent approval publication any more.

Install the reviewed package revision into `~/.skenv` first so the scripts and
imported modules agree. Then install and activate the producer independently:

```bash
install -m 0755 scripts/fleet/link-lineage.py ~/.skenv/bin/link-lineage.py
install -m 0755 scripts/fleet/skfleet-link-producer.py \
  ~/.skenv/bin/skfleet-link-producer.py
install -m 0644 systemd/skfleet-link-producer.{service,timer} \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skfleet-link-producer.timer
```

Dry run:

```bash
python -m skcapstone.link_observation_producer \
  --repo smilinTux/skcapstone \
  --lineage ~/.skcapstone/coordination/link-lineage.json \
  --output ~/.skcapstone/coordination/link-observations.json \
  --dry-run
```
