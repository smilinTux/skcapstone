### Fixed
- **A repaired memory had its title stripped back off within the hour, every
  hour, and the sweep logged `"promoted": 0` while doing it.** Two defects that
  only did damage together. `memory_engine._load_entry` skipped a unified
  SKMemory record only when `id` was present AND `memory_id` was absent, so a
  repaired file (which carries both keys by construction) walked past the guard
  into `MemoryEntry`, a model with no `title` field; pydantic's
  `extra="ignore"` dropped `id` and `title` silently and the next
  `_save_entry` wrote the loss to disk. Separately,
  `PromotionEngine.compress_memories` could never converge: it truncates to
  `max_chars` and appends `"..."`, yielding `max_chars + 3`, which is always
  greater than `max_chars`, so the already-short-enough check could not retire
  a memory it had itself compressed. One observed file sat at exactly 503 chars
  and was re-truncated to a byte-identical string on every sweep for 38 days,
  and each of those rewrites was a chance to lose fields the legacy model does
  not carry. The guard now tests only for `id`, and compression compares
  against the compressed form so a fixed point is recognised as one. A
  genuinely legacy record (`memory_id`, no `id`) still loads, and content
  genuinely longer than the fixed point still compresses.
