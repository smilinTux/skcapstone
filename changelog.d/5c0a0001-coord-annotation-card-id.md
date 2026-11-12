- `coord label` and `coord link` (CLI and MCP) now refuse a task id that is not exactly
  eight lowercase hex characters, and `coord label` refuses a card with no foldable
  core, instead of appending an orphan overlay event for an id that names no card
  (for example a pasted `POOL_IDS|...` string). Port of #325 (card 68df7567).
