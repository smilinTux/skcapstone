# Dev / CI / prod sync: making the fleet honest about what is actually deployed

**Status:** design
**Date:** 2026-08-08
**Author:** Lumina (with Chef)
**Scope:** every SK\* Python package installed editable into `~/.skenv`

## The problem, stated precisely

Roughly 20 SK\* packages are installed into `~/.skenv` as editable installs
(`pip install -e`), each pointing at a checkout under
`~/clawd/skcapstone-repos/`. The live services import from those checkouts. That
means **production is the working tree**. There is no build, no artifact, and no
moment where "what runs" and "what is committed" are forced to agree.

That is not a hypothetical risk. It has produced three distinct failures already,
each of which cost real debugging time:

1. **CI failed on code that worked perfectly on every dev box.** skchat's suite
   imported `capauth` and `skcomms` at module load. Both are published, neither
   was declared as a dev dependency. Dev machines had them installed, so the
   suite was green everywhere a human looked; CI installed clean and collected
   ~36 modules' worth of `ModuleNotFoundError`. Same story, independently, in
   capauth: `tests/test_integration.py` opens with the sentence *"skcapstone is
   installed in the dev venv"*, and in CI it is not, so 9 tests errored instead
   of testing anything.

2. **A published version number that did not describe the published code.**
   capauth's repo and PyPI both said `0.2.14`, while the repo carried 8 skchat
   PDP rules and the wheel carried 3. Anything that consumed capauth from PyPI
   silently got a different policy than the one under review. A version-string
   comparison reports these as identical, so the drift was invisible until
   something failed at runtime.

3. **An API that was never published at all.** skcomms' `pqdm2` surface existed
   in the working tree, was imported by working code, and did not exist in any
   released artifact. Every dev box and every prod service resolved it via the
   editable path. Nothing outside `~/clawd` could.

The common shape: **a checkout can differ from its origin, and an origin can
differ from its published artifact, and nothing anywhere reports either.** Every
surface a person looks at (the running service, the local test run, `pip show`)
reads green.

`pip show` is actively misleading here. It reports capauth as `0.2.3` while the
repo's `pyproject.toml` says `0.2.14`, because editable install metadata is
frozen at install time and never refreshed.

## Direction chosen

Chef's call: **keep the editable installs, and make everything else stop lying
about them.**

This is the right trade. Editable installs are what make this environment fast to
work in, a change to skcomms is live in skchat on the next process start, with no
build step and no version dance. The problem was never that dev is editable. It
is that CI pretended dev's ambient state did not exist, and that nothing ever
compared a checkout against what the rest of the world can see.

So: do not change how services run. Fix the two lies.

## Approaches considered

### A. Extend `skcapstone doctor` with a source-drift check (chosen)

`doctor` is already the fleet's health surface. It runs at session start, has a
`Check` structure with categories and suggested fixes, already carries a
`packages` category, and already has `_check_packages()` and `_check_versions()`.
Adding drift detection here means it surfaces through machinery that exists and
that people already read.

### B. A per-repo `pre-push` hook

Cheap, but it fires on the wrong event. capauth sat unpublished for months
precisely because nobody pushed a release; a push-triggered check cannot catch a
repo whose problem is that nothing is happening to it. It also has to be
installed in ~20 repos on every node, which is its own drift problem.

### C. A new `skfleet sync-status` command plus a scheduled alert

Fleet-scoped and tidy, and it would work. Rejected because it stands up a second
health surface competing with `doctor`, and the fleet has already decided that
`doctor` is the health surface. Two places to look is how you end up looking at
neither.

## Design

Two halves. They are independent and can land separately.

### Half 1: make CI install what dev has ambiently

**Rule:** if a test imports a sibling SK\* package, CI installs that package
**from git `main`**, with `--no-deps --no-cache-dir --force-reinstall`.

Not from PyPI. The published wheel is exactly the thing we have proven can lag
its repo, so installing from PyPI would make CI test a combination that no
developer is running and that will not be what ships. Installing from git `main`
makes CI test *the code that ships together*, which is the actual contract.

`--no-deps` matters and is not optional: without it, installing skcapstone into
capauth's CI would drag a PyPI `capauth` in over the checkout under test, and the
job would silently stop testing the PR.

Already applied to:

- skchat (`pytest.yml`, `ci.yml`, `qa.yml`): installs `capauth` and `skcomms`
  from git. Took the suite from 27 failures to 0, confirmed green in real CI.
- capauth (`ci.yml`): installs `skcapstone` from git. Note the direction is
  deliberately *not* a declared dependency, capauth must never depend on
  skcapstone (skcapstone depends on capauth); skcapstone is the optional
  integration backbone and the test only needs it present to exercise
  "integrated" mode.

**Corollary rule, learned the hard way:** a gate that skips itself must not
report green. capauth's `test_factory_returns_sequoia_backend` bypassed its own
file's "requires `sq`, skipped otherwise" fixture and hard-failed on every
runner. The pqdm2 interop gate had the mirror-image bug: it enforced nothing and
still reported success. Both are the same defect class. When a test guards a
capability, either the guard runs or the suite says out loud that it did not.

**Pin the linters.** An unpinned `ruff` re-reds a lint job every time upstream
adds a rule, with no commit from us in between, which trains everyone to ignore a
red job. skchat and capauth now pin an exact ruff version. Bump it deliberately,
in the same commit as the reformat it implies.

### Half 2: teach `doctor` to compare a checkout against the world

A new `_check_source_drift()` contributing to `run_diagnostics()`, emitting
`Check` objects under a new `source` category. For each editable-installed SK\*
package, resolve its checkout path from the `__editable__.*.pth` entry and report
three independent facts:

| Check | Question | Why it matters |
|-------|----------|----------------|
| `source:<pkg>:uncommitted` | Does the working tree differ from `HEAD`? | Prod is running code that exists in exactly one place on earth. |
| `source:<pkg>:unpushed` | Does `HEAD` differ from `origin/<branch>`? | CI has never seen it. No teammate and no other node can get it. |
| `source:<pkg>:unpublished` | Does the built repo tree differ from the released wheel? | Anyone consuming this from PyPI gets different code. |

Each failure carries a `fix` string (`git -C <path> push`, `git -C <path> tag
v<next>`), consistent with every other `doctor` check.

**The unpublished check compares content, not version strings.** This is the
central design decision, and it is what makes the check worth building. capauth's
repo and its wheel both said `0.2.14` and differed in eight PDP rules; a version
comparison would have reported "in sync" and been wrong in the most damaging
possible way, by confirming the thing we were trying to disprove. The check
builds an sdist/wheel from the checkout, hashes the package tree (normalizing for
non-deterministic build metadata), and compares against the same hash of the
downloaded released artifact.

That is the expensive part, so it is gated: `doctor` runs a cheap version-and-
git-state pass by default, and the content comparison runs under
`doctor --deep` and on a schedule, not on every session start.

**Degrade, never block.** Every one of these can fail for boring reasons: no
network, a detached HEAD, a repo with no remote, a package that is not on PyPI at
all (which is a legitimate state, not an error). Any check that cannot answer its
question reports "unknown" with the reason, and never a false pass. A drift
detector that cries wolf gets muted, and a muted detector is worse than none,
because it converts a known unknown into an assumed green.

`_check_versions()` stays as-is but is explicitly **not** the drift check. It
compares installed-vs-PyPI, and for an editable install its "installed" number is
frozen at install time (it reports capauth as `0.2.3` today while the repo says
`0.2.14`). It answers a different and much weaker question.

## What this does not do

It does not make the fleet reproducible. Editable installs mean a given node's
behaviour still depends on ~20 checkouts being at the right commit, and this
design only *reports* on that, it does not enforce it. Enforcement (pinned
commits, a lockfile, built artifacts in prod) is a much larger change that Chef
explicitly did not choose, and it would cost the fast edit-to-live loop that
makes this environment work.

The honest framing: this converts silent drift into loud drift. That is a real
and sufficient improvement over the current state, and it is not the same thing
as eliminating drift.

## Success criteria

1. A checkout with unpushed commits shows a failing `doctor` check naming the
   package and the commit count.
2. A repo whose built tree differs from its released wheel is reported even when
   the version strings match. Regression-test this against the exact capauth
   `0.2.14` case, which is the scenario that motivated the whole design.
3. No SK\* CI job depends on a package being ambiently present in the runner.
4. Every check degrades to a named "unknown" offline, never to a false pass.

## Related

- Memory: `skworld-fleet-control-plane`, `pypi-publishing-fleet`,
  `pqdm2-interop-gate-hardening` (the self-skipping-gate lesson).
- `skcapstone/src/skcapstone/doctor.py`: `run_diagnostics()`, `_check_packages()`,
  `_check_versions()`, the `Check` dataclass.
