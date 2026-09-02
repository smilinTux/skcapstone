# Mero blocker census

**Epic:** `2516480b`. **Card:** `8fa7d8eb`. **Status:** implemented.

`skcapstone.mero_census` is Mero's read-only recurring blocker census for the
ITIL coordination board. It scans the `CardStore` under `~/.skcapstone` and
produces a bounded `CensusReport` of typed findings. It never mutates: no
claims, releases, moves, merges, deploys, credentials or selector re-runs —
only `append_event` with `RECOMMENDATION_EVENT` as its sole write, and even
that only when a finding is fresh.

## Finding classes

`CensusFindingType` (all emitted through the bounded `report.findings` list,
capped at `max_findings`, cards capped at `max_cards`):

| value | meaning |
|---|---|
| `dead_claim` | a claim whose owner process is gone AND whose identity is gone |
| `stale_claim` | a claim past `stale_claim_sla` (default 24 h), tagged `at_risk` or `missed` |
| `completed_dependency` | a card whose dependency is already `done` |
| `contradictory_verdicts` | two or more conflicting verdicts on one card |
| `malformed_blocker_referent` | a `blocked_on` referent that does not resolve to a real card |
| `void_dependency_edge` | a live card that depends on a voided card |
| `superseded_live_card` | a live card superseded by a completed/voided successor |
| `review_identity_gap` | a card missing the review identity it claims |

## Recommendation envelope

Each finding optionally carries a typed `recommendation` dict:
`{type: <CensusRecommendationType>, payload: {...}, dedup_key: str,
evidence_link: str, artifact_sha256: str}`. Types: `release_claim`,
`complete_dependency`, `unblock`, `void`, `reopen`, `assign`, `note`.

Dedup: the census keeps a generation counter per `dedup_key` in
`~/.skcapstone/coordination/itil/mero_census_state.json`. A recommendation is
re-emitted only when its generation increments, so re-running the census does
not spam the board with the same event.

## Running it

```python
from skcapstone.mero_census import MeroBlockerCensus

census = MeroBlockerCensus(Path.home() / ".skcapstone")
report = census.run()
for finding in report.findings:
    print(finding["finding_type"], finding.get("details"))
```

## Tests

`tests/test_mero_census.py` pins all eight finding classes (positive), the
recommendation envelope, dedup generation, and the negative boundary: the
census module exposes exactly the read + single-write surface and nothing else
(no `claim`, `release`, `launch`, `stop`, `create`, `mutate`, `merge`,
`deploy`, `credential` access). 48 tests, all green.
