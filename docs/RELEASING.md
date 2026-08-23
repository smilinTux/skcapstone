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
3. Verify every new import and behavior against that artifact. For the 0.1.28
   floor, this includes the existing lifecycle and authoritative criteria-fold
   contracts plus `skcoord.cmdb_scheduler`, its disabled default policy, lease,
   incident routing, and retention helpers.
4. Raise the skcapstone dependency floor to the verified skcoord version.
5. Only then release skcapstone.

The skcapstone test workflow installs skcoord from its moving Git default
branch. That is source-compatibility evidence, but it does not prove that the
registry artifact selected by a fresh install contains the required API. A
local source overlay has the same limitation. Do not publish skcapstone while
the minimum required skcoord artifact is unavailable or unverified.

## skcapstone: the tag is cut FOR you

`.github/workflows/publish.yml` has a `tag` job gated on
`github.ref == 'refs/heads/main'` that cuts **the next patch tag** whenever
HEAD on main is not already tagged, then builds and publishes to PyPI.

So a normal merge to main produces a patch release with no human action.
`v0.15.14` was cut that way, by `github-actions[bot]`.

The version itself comes from the tag via setuptools-scm (`pyproject.toml`
`[tool.setuptools_scm]`, "The git tag IS the version"). Nothing is hardcoded.

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
