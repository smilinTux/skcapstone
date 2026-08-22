# CMDB operator views

SKDashboard projects the canonical `skcoord` CMDB fold and verified reconcile
run artifacts. It does not maintain another inventory or write path.

## Fleet view

`GET /api/cmdb/overview` reports total CIs, status counts, evidence freshness,
the latest collector coverage, per-node completeness, unreachable target count,
the last successful reconciliation, and up to ten recent verified runs. An
artifact is ignored if its checksum does not match.

## CI detail and impact

`GET /api/cmdb/ci/{ci_id}` reports folded attributes, ownership, endpoints,
last seen, discovery provenance, event writers, status history, and grouped
`runs_on`, `hosts`, `depends_on`, and `connects_to` relationships. Reverse
dependents and linked open ITIL incidents make the operational impact visible.

## Search and filters

`GET /api/cmdb/search` accepts bounded `q`, `limit`, `type`, `node`, `status`,
`owner`, `tag`, `staleness`, and `source` parameters. Filters are combined and
read only from the canonical fold.

## Safe reconciliation controls

`GET /api/cmdb/plan` executes local discovery in preview mode and reports the
authorization state that would govern apply. Its `execution_state` is always
`not_executed`. `POST /api/cmdb/apply` passes through the existing capability
gate, applies the canonical reconciler only when authorized, and reports
`applied` or `refused`. Repeated scans converge without duplicate CIs.
