# `changelog.d/` — one changelog fragment per PR

**Adding a changelog entry? Put it in a NEW file here. Do not edit `CHANGELOG.md`.**

```bash
# pick any slug that will not collide; the branch name or card id works well
cat > changelog.d/fix-lane-admission-timeout.md <<'ENTRY'
- Card `7ad5f0c1`: lane admission now uses an explicit bounded endpoint timeout
  above measured gateway health latency, without weakening fail-closed checks.
ENTRY
git add changelog.d/fix-lane-admission-timeout.md
```

That is the whole workflow.

## Why this exists

The `docs / docs-check` tier-2 gate fails any PR that touches `src/**` or
`pyproject.toml` without recording a changelog entry. **That gate is good and it
stays.** The entries it forces are detailed and evidence-rich, and they are why
this project's changelog is worth reading.

The problem was never the requirement. It was that the requirement pointed every
PR at the *same lines of the same file*. With a dozen agents opening PRs
concurrently, every one of them appends to the top of `CHANGELOG.md`, so a rebase
conflict there is not bad luck, it is the default outcome. Three were resolved by
hand on 2026-09-18 alone.

A fragment per PR means two PRs never touch the same lines. The conflict is not
made rarer, it is made **structurally impossible**. The only way to collide is to
choose the same filename as another open PR, which a branch- or card-derived slug
already prevents, and which git reports honestly as an add/add conflict rather
than silently interleaving prose.

## Rules the gate actually enforces

- The file must be a `.md` sitting **directly** in `changelog.d/`. A nested
  subdirectory does not count.
- `README.md`, `.gitkeep`, and `.gitignore` do not count as entries. Otherwise the
  mere existence of this directory would satisfy tier 2 for free.
- Editing `CHANGELOG.md` directly **still works**. It is not deprecated, and it
  remains the right move for a release-assembly commit. Expect the conflict.
- The escape hatches are unchanged: the `docs-exempt` label, or `[skip-changelog]`
  in the PR title, for a genuinely trivial change.

## Format

A fragment is a fragment of the changelog, so write exactly what you would have
written under `## Unreleased` — normally one top-level `-` bullet, wrapped at about
80 columns. It is pasted in verbatim. No front matter, no heading, no category
taxonomy to memorise.

Match the house style: say what changed, and say what was observably wrong before.
"Fixed a bug" is not an entry.

## Folding fragments into `CHANGELOG.md`

```bash
python scripts/changelog_fragments.py --check     # list what is pending
python scripts/changelog_fragments.py             # fold them in and delete them
```

The script inserts every fragment under the first `## Unreleased` heading, oldest
first, then removes the fragment files. Run it whenever the directory gets noisy;
there is no required cadence, because `publish.yml` cuts a patch tag on **every**
merge to `main`, so this repo has no single release moment to hang it on.

It refuses to run if `CHANGELOG.md` contains more than one `## Unreleased`
heading. That is not hypothetical: the file carried two identical `## Unreleased`
headings for months, which made "insert after `## Unreleased`" ambiguous and broke
a naive insert. Failing loudly beats appending to whichever one came first.
