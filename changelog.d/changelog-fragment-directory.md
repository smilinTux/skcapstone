- **`changelog.d/` fragments: the rebase conflict on `CHANGELOG.md` is now
  structurally impossible, not merely rarer.** The `docs / docs-check` tier-2
  gate fails any PR touching `src/**` or `pyproject.toml` without a changelog
  entry, and that gate is kept: the entries it forces are detailed and
  evidence-rich, and they are why this changelog is worth reading. The defect was
  that it pointed every PR at the same lines of the same file, so with a dozen
  agents opening PRs concurrently a rebase conflict was the default outcome
  rather than bad luck (three resolved by hand on 2026-09-18 alone: #787, #790,
  and a third agent independently). A PR now adds a new `changelog.d/<slug>.md`
  instead, so two PRs never touch the same lines. `scripts/changelog_fragments.py`
  folds pending fragments into `CHANGELOG.md` and deletes them. The gate widening
  lives in sk-standards (`docs_check.py` tier 2) and accepts only a `.md` directly
  in `changelog.d/`; a `src/**` change carrying neither a fragment nor a
  `CHANGELOG.md` edit still fails, and the `docs-exempt` label and
  `[skip-changelog]` title hatches are untouched.

- **`CHANGELOG.md` carried two identical `## Unreleased` headings; now one.** The
  file is three changelogs concatenated by successive prepends, and the duplicate
  made "insert after `## Unreleased`" ambiguous, which had already broken a naive
  insert. Only the duplicate heading line was removed, so every entry under it
  flows into the single section above it with nothing reordered and no entry text
  changed. `scripts/changelog_fragments.py` refuses to run if more than one
  `## Unreleased` heading ever reappears, rather than guessing which was meant.
  A third heading, the bracketed `## [Unreleased]` at what is now line 1639, sits
  below an orphaned second preamble from the original file and is left alone:
  merging it would relocate ~890 lines and is a separate change.
