# Mediated Link observation producer

The producer is separate from the Link seat. It may use a read-only GitHub
connector such as `gh api`, but Link never receives that connector, its token,
or its process environment.

The producer requires a lineage manifest with schema
`skfleet.link-lineage/v1`. Each open PR must map to an exact source card,
card-generation, review-card ID, review-card revision, and terminal review
verdict. Reviewer candidates
must carry their exact identity, host, session, and workspace. Missing lineage
is incomplete evidence, not an orphan that can become healthy by inference.
The dry-run reconciler reads the explicit reviewer authority at
`~/.skcapstone/identity/reviewer-candidates.json`; that source must contain
the independent Seraph seat and its lowercase SHA-256 public-key fingerprint.
Jarvis is never a lifecycle reviewer candidate.

On success, the producer writes the canonical
`skfleet.link-observation-feed/v1` feed with an atomic same-directory replace.
It includes the connector source revision and evidence SHA-256. Connector
failure, stale snapshots, malformed PR data, duplicate PR keys, or incomplete
lineage never replace the last valid feed. Incomplete runs write only a
`.blocked.json` diagnostic beside the target.

The producer has no merge, deployment, release, card-claim, or fleet-mutation
path. A producer run is not permission to enable the Link timer. The producer
timer is intentionally not installed yet; every PR must first have a terminal
review outcome, and the Link timer remains disabled until a fresh producer
output is observed. FAIL and BLOCKED outcomes remain visible to Link but
cannot satisfy merge eligibility, which requires exact independent PASS.

Dry run:

```bash
python -m skcapstone.link_observation_producer \
  --repo smilinTux/skcapstone \
  --lineage ~/.skcapstone/coordination/link-lineage.json \
  --output ~/.skcapstone/coordination/link-observations.json \
  --dry-run
```
