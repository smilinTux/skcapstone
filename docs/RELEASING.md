# Releasing: how a version actually gets cut

Written 2026-08-15 after nearly cutting a duplicate release by hand. The two
repos in this family do NOT release the same way, and the difference is the
kind of thing that only bites once you are already halfway through.

## Cross-package order: release skcoord first

skcoord owns the coordination lifecycle and authoritative card fold consumed
by skcapstone. When skcapstone imports a new skcoord symbol or relies on new
fold behavior, release order is strict:

1. Merge and release the required skcoord change first.
2. In a fresh environment, install the published skcoord artifact from the
   registry, with no sibling checkout, editable install, VCS dependency, or
   `PYTHONPATH` overlay.
3. Verify every new import and behavior against that artifact. For the 0.1.39
   floor, this includes the existing lifecycle and scheduled reconciliation
   contracts plus current acceptance-criteria folding through every CardStore
   rollback selector.
4. Raise the skcapstone dependency floor to the verified skcoord version.
5. Only then release skcapstone.

The skcapstone test workflow installs skcoord from its moving Git default
branch. That is source-compatibility evidence, but it does not prove that the
registry artifact selected by a fresh install contains the required API. A
local source overlay has the same limitation. Do not publish skcapstone while
the minimum required skcoord artifact is unavailable or unverified.

## Changelog fragments, and why there is no "changelog build" step

Entries land as `changelog.d/<slug>.md` fragments, one per PR, so concurrent PRs
never conflict on a shared `CHANGELOG.md`. `python scripts/changelog_fragments.py`
folds pending fragments into the `## Unreleased` section and deletes them.

**There is no required cadence for running it, and it is not wired into CI.** That
is deliberate and it is why this repo uses a local script instead of towncrier or
scriv: both are built around a discrete release moment at which `build` runs, and
this repo has none. As the next section describes, `publish.yml` cuts the next
patch tag on *every* merge to `main`, so "assemble at release time" would mean
"assemble on every merge", which is just the shared-file conflict again wearing a
build step. Fold the fragments when the directory gets noisy, or when you are
hand-cutting a minor or major version and want the notes collected under it.

The script refuses to run if `CHANGELOG.md` contains more than one
`## Unreleased` heading. It carried two identical ones for months, which made a
naive "insert after the heading" ambiguous and broke at least one insert.

---

## skcapstone: the tag is cut FOR you

`.github/workflows/publish.yml` has a `tag` job gated on
`github.ref == 'refs/heads/main'` that cuts **the next patch tag** whenever
HEAD on main is not already tagged, then builds and publishes to PyPI.

So a normal merge to main produces a patch release with no human action.
`v0.15.14` was cut that way, by `github-actions[bot]`.

The version itself comes from the tag via setuptools-scm (`pyproject.toml`
`[tool.setuptools_scm]`, "The git tag IS the version"). Nothing is hardcoded.

## Reproducible candidate artifacts

Evidence and independent review builds use the task-owned deterministic entry
point, not ordinary `python -m build`:

```bash
python -m pip install build setuptools-scm
python scripts/build_reproducible.py --outdir /tmp/skcapstone-dist
```

Run it from a clean tracked checkout and an empty output directory. It derives
`SOURCE_DATE_EPOCH` from the exact source commit, derives the exact version from
setuptools-scm, exports the distribution-specific pretend-version value to the
isolated build, and normalizes the source archive's gzip, tar, timestamp, and
owner metadata. It adds no runtime dependency. Two clean checkouts at the same
commit must produce byte-identical wheel and source archives.

**Cut a tag by hand only when you want a version the bot would not choose**,
which in practice means a minor or major bump. `v0.15.15` was hand-cut that way.

## skgateway: release by PR

skgateway uses a `release/vX.Y.Z` branch merged through a PR. `v0.6.0` came in
as "Merge pull request #27 from smilinTux/release/v0.6.0". Its `publish.yml`
fires on `tags: ['v*']` and publishes to PyPI.

## The hazard, concretely

On 2026-08-15 two sessions worked the same epic. One hand-cut `v0.15.15` on
commit `28016d7`. Twenty minutes later the other prepared `v0.16.0` on **the
same commit**, having checked the tag list before the first tag existed.

Two tags on one commit means:

- setuptools-scm has an ambiguous version for that commit
- identical code publishes twice to PyPI under two version numbers
- **PyPI has no delete API**, so neither can be withdrawn

It was caught before pushing, but only because the tag was compared against
`v0.15.15` rather than trusted. So:

## Before cutting any tag

1. `git fetch origin --tags` first. A tag list from five minutes ago is stale
   when other sessions are active.
2. `git tag --points-at HEAD`. If anything comes back, the commit is already
   released. Stop.
3. Ask whether the bot will do it for you. On skcapstone a patch bump needs no
   human at all.
4. Check `git log origin/main..HEAD` is empty. Tagging a commit that is not on
   origin publishes something nobody else can see.

## What is genuinely irreversible

Pushing a `v*` tag publishes to PyPI, and PyPI has no delete API (the manage UI
is the only recourse, and it will not free the version number). Everything else
here is recoverable. Treat the tag push as the point of no return, not the
merge.

## A cut release is not a deployed one

Everything above ends at PyPI. It says nothing about which fleet host is
actually running the tag once it is up: three different `skmail` binaries
sat across five hosts, and `pip show skcapstone` agreed with `__version__`
on every one of them throughout. Once a host has installed a release,
`skcapstone fleet node drift` compares its actual content and live state
against a fresh manifest built from a checkout, rather than trusting a
version string. `skcapstone fleet rollout` and `skcapstone fleet rollback`
then get a release onto (or back off of) the fleet's hosts, one node at a
time, gated by that same check, dry run by default. See
[`docs/fleet/rollout-drift.md`](fleet/rollout-drift.md). All three are
human-invoked: nothing here is scheduled, and cutting a release still does
not put it on any host by itself.
