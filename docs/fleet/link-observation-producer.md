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
`SKFLEET_LINK_REPOSITORIES`: `smilinTux/skcapstone`,
`smilinTux/skdashboard`, `smilinTux/skworld`, and
`smilinTux/sk-standards`, with the optional exact private route
`https://skgit.skstack01.douno.it/smilinTux/sklegal`. These four GitHub
repositories remain mandatory. Both lineage discovery and feed observation use
the same forge selector. The producer fails closed before connector access
when this setting is empty, malformed, duplicated, missing a product, or
contains any other repository. Missing or malformed checks remain unknown.

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
