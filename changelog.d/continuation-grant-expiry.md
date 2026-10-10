### Fixed

- An expired, unused builder continuation grant (no node capacity within its hour) is kept as `expired-<id>.json` and replaced by a fresh one instead of blocking the generation forever.
