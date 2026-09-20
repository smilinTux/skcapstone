### Added

- `skcapstone coord show <id>` prints one folded card (status, column, labels,
  links, assignment), with `--json`. Previously the only way to read a single
  card was to render the entire board and filter it, which on a multi-thousand
  card store meant megabytes of JSON to answer a question about one id.

### Changed

- `coord describe` is renamed to `coord edit`. Every other CLI spells
  "describe" as a read (kubectl, aws, docker) while here it writes a title or
  description, which reliably misleads. `describe` stays as a deprecated alias
  that warns and still performs the edit, so existing scripts keep working.
