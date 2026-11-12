### Fixed

- Release the exact `<actor>-requal-<card>` claim of a profile requalification job that failed (remote executor or batch advance) and restore the card to READY. Those claims leaked: the card stayed DOING with a live-looking owner, so the legacy selector counted it as claimed and it never entered the POOL_V2 population.
