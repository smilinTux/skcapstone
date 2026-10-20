- `UsageTracker.get_daily` on the shared home now also sums the per-agent usage
  files under `agents/*/usage/`, so `skcapstone usage` stops under-reporting agents
  that write into their own home. The write path is unchanged. Malformed agent
  files (bad JSON, non-object roots, non-numeric counters) are skipped instead of
  failing the report. Port of #341 onto current main.
