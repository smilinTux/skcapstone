The CMDB operator adapter now has a module-level `CONDITIONS` table like the other
adapters. Its condition schema in the operator loop was empty, so the loop dropped
every cmdb observation and never saw `CmdbReconcileFresh`, `CmdbLastScanComplete`
or `CmdbAuditClean`. `ACTION_CONDITIONS` declares which condition makes each cmdb
action eligible (the shadow for the first two, nothing for the irreversible apply).
