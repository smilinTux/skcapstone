# Reproducible package build repair TDD

Date: 2026-08-24

Producer card: `e1d24370` (`SKCAP-CRIT-02F`)

Independent review card: `a6c7fd2f` (`SKCAP-CRIT-02R`)

## Purpose

The criteria-fold uptake source candidate is accepted at commit
`627c8193c9b76f1a409642d52b4981d314622cbf`, tree
`df017ce637ae65957226f43ba5e64e8fcf4e8d1c`, but its ordinary wheel and source
distribution builds are not byte-reproducible. This repair defines one small,
fail-closed build entry point. It changes packaging procedure only and preserves
the released SKCoord `0.1.39` requirement and all runtime behavior.

## Producer contract

Before implementation, build the pinned source twice at different wall-clock
times with the ordinary `python -m build` procedure. At least one archive hash
must differ. Compare archive member timestamps, package metadata, member counts,
and normalized payload hashes so the test proves real timestamp drift rather
than comparing unrelated source.

Also prove setuptools-scm sensitivity. The same source tree under different Git
commit identity, or without its exact version override, must not silently claim
the canonical candidate version and artifact hash.

The repair uses only existing build tools and the standard library:

1. Refuse a missing Git checkout or tracked working-tree changes.
2. Derive `SOURCE_DATE_EPOCH` from the exact source commit timestamp.
3. Derive the exact setuptools-scm version from that clean source checkout.
4. Export `SOURCE_DATE_EPOCH` and the distribution-specific
   `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE` value to `python -m build`.
5. Write artifacts only to the caller-selected output directory.

No build tool is vendored and no runtime dependency is added. The procedure may
require the already-declared build requirements `build` and `setuptools-scm` in
the invoking build environment.

Unit tests are written before the implementation. They prove exact environment
construction, source-derived values, dirty-tree denial, missing metadata denial,
and subprocess failure propagation.

After implementation, make two separate clean noneditable source directories at
the same accepted producer commit. Start their builds at least two seconds apart.
Both wheel files must be byte-identical and both source distributions must be
byte-identical. Record each SHA-256, archive member count, normalized member
payload hash, metadata version, dependency metadata, source commit, source tree,
source epoch, and exact version override.

Sensitivity removes the deterministic inputs and repeats the differently timed
ordinary builds. The same comparison must detect hash drift. A false-clean
sensitivity result blocks completion.

Install each independently built wheel with the exact released
`skcoord-0.1.39-py3-none-any.whl` in a fresh environment. Neither installation
may resolve through an editable path, sibling checkout, `.egg-link`, editable
`direct_url.json`, or `PYTHONPATH`. The installed CLI must return the same nine
current acceptance criteria for unset, `1`, `dual`, `0`, `off`, `false`, and
`no`, and malformed criteria state must make reads and claims fail closed without
rewriting task, core, event, or claim state.

Run focused tests, the complete repository test gate, Black, Ruff, documentation
tiers 1 through 3 with changed-file context, build, Twine, Gitleaks, and exact
rollback. Seal source, tree, artifact, metadata, test, sensitivity, rollback,
secret-scan, and fresh-index evidence with the required co-author trailer.

## Independent review contract

The existing reviewer for `a6c7fd2f` remains distinct from the producer and does
not repair the source. After `e1d24370` is DONE, the reviewer fetches current
state and uses a new isolated worktree. It reads this complete TDD before review
action.

The reviewer checks out the exact producer source commit and tree, repeats two
differently timed clean builds through the documented entry point, and requires
byte-identical wheel and source distribution hashes. It independently checks
member counts, normalized payload hashes, metadata version, dependency floor,
source epoch, exact version override, and Twine results.

The reviewer repeats the omitted-input sensitivity and requires detected drift.
It installs both exact producer wheels with SKCoord `0.1.39` into fresh
noneditable environments and repeats selector parity, malformed-state read and
claim denial, and no-mutation proof.

Review also proves no runtime dependency, editable sibling, task or core rewrite,
baseline change, service mutation, manual tag, check bypass, force push, remote
write, or shared-checkout edit. It records immutable PASS or BLOCKED evidence on
`a6c7fd2f` without changing producer source.

## Rollback

Rollback removes only the task-owned build entry point, its tests, changelog and
release documentation updates, TDD, and evidence. A disposable reverse-apply and
revert must reproduce tree
`df017ce637ae65957226f43ba5e64e8fcf4e8d1c` exactly. Shared checkout, live
CardStore outside `e1d24370`, service, runtime, operator, ATLAS, baseline, and
remote state remain untouched.
