# Completion Evidence - Card 14380835

Title: [SKFLEET-TEST-ENV][S][REPAIR] Mount explicitly qualified Python test
dependencies read-only

Owner: pi-fiber-implementation-14380835
Claim revision: 38688667e84849b084dc6810db6953a1
Base revision: e6d4139775682ae596c182168cd2f059aafff5b9
Base ref: fix/fb673cd3-chiwk12-account
Repository: https://github.com/smilinTux/skcapstone.git
Worktree: /work
Date: 2026-09-30

## Outcome

Proposed provisional outcome: PASS_FOR_REVIEW.

This card adds an optional, operator-qualified, read-only Python test
dependency environment for `tests`-stage queue jobs, with fail-closed
refusal for absent, malformed, symlinked, unready or mismatched inputs.
Jobs without a selector keep the existing stdlib-only behavior unchanged.

## Files changed (exact paths)

- ops/fiber-f1be0929/fiber_test_environment.py (new, ~6.0 KB)
  Stdlib-only selector normalization, manifest qualification, uv.lock
  digest verification, and in-sandbox import preflight.
- ops/fiber-f1be0929/fiber_sandbox.py (modified)
  New `environment_mount` and `environment_probe`; `command()` gained an
  optional `test_environment` argument. Read-only `--ro-bind` of the
  operator venv to `/opt/test-env`, prepended private PATH, validated
  `/work`-relative `PYTHONPATH`. No namespaces, limits, bridge or writable
  `/work` behavior changed.
- ops/fiber-f1be0929/fiber_worker.py (modified)
  Selector normalization in `validate`; `environment_plan`,
  `verify_environment_lock`; check/execute preflight; workspace uv.lock
  digest binding before model start; environment passed to `command`.
- ops/fiber-f1be0929/tests/test_fiber_test_environment.py (new, 18 tests)
  Selector, manifest, lock-binding, sandbox mount and in-sandbox probe.
- ops/fiber-f1be0929/tests/test_fiber_worker.py (modified, 6 new tests)
  Selector normalization/stage binding, pre-claim refusal, lock fail-closed,
  environment plan exact-repository binding.
- docs/evidence/agents/14380835/COMPLETION-EVIDENCE.md (this file)

No other file was modified. SHA256SUMS, README, controller modules,
admission/limits/routing/claims code, and all other tasks' files were not
touched.

## Selector contract implemented

- Field: job `test_environment`, optional.
- Allowed keys exactly: `lock_sha256` (64 lowercase hex) and `python`
  (only the string `3.12`). Any extra or malformed field is refused
  (`test-environment-selector-invalid`).
- Absent/None selector returns None and preserves the current behavior.
- Present selector is refused for any stage other than `tests`
  (`test-environment-stage`).

## Trusted artifact contract implemented

- Path: `Path.home()/.local/share/skfleet-fiber/test-environments/<lock_sha256>/`.
- `manifest.json` must bind exactly the six fields `schema`, `repository`,
  `lock_sha256`, `python`, `ready`, `source_dirs`, with `schema=schema1`,
  `python=python3.12`, `ready=true`, the exact job repository string, and
  `lock_sha256` equal to the directory name / selector.
- `source_dirs` is a bounded list (max 32) of safe repository-relative
  directories. Absolute, `~`, backslash, NUL, `.`/`..`, empty or duplicate
  entries are refused (`test-environment-source-dir`).
- The artifact directory, its parent base, `manifest.json` and `venv` must
  not be symlinks; the resolved artifact must live directly under the base
  (`test-environment-path`).
- Missing artifact or missing `venv/bin` refuses
  (`test-environment-unavailable`); malformed JSON refuses
  (`test-environment-manifest-invalid`).

## Fail-closed order (before model execution)

1. `validate` normalizes and refuses a malformed selector.
2. `execute` calls `environment_plan` before `verify_claim`, so an absent /
   unready / wrong-repo environment refuses before any claim read or
   workspace action.
3. After materialization, `verify_environment_lock` requires the workspace
   `uv.lock` SHA256 to equal the selector exactly
   (`test-environment-lock-missing` / `test-environment-lock-mismatch`).
4. `fiber_sandbox.environment_probe` runs `/opt/test-env/bin/python3.12 -c`
   inside the real sandbox and requires `sys.prefix == /opt/test-env`,
   Python 3.12, and successful `import pytest, pydantic`. Failure refuses
   with `test-environment-imports-failed` before the Pi process starts.
5. `check` mode performs the same `environment_plan` and in-sandbox import
   probe on a disposable workspace before reporting ready.

## Exact tests and results

Environment: `/usr/bin/python3` = Python 3.12.3, stdlib-only `unittest`.
Commands run from `/work/ops/fiber-f1be0929`:

```
PYTHONPATH="$PWD" python3 -m unittest tests.test_fiber_test_environment
Ran 18 tests in 0.005s - OK
```

```
PYTHONPATH="$PWD" python3 -m unittest tests.test_fiber_worker
Ran 26 tests in 0.043s - FAILED (errors=1)
```

The single error is pre-existing and unrelated:
`test_source_metadata_and_links_must_agree` imports the unavailable host
`skcapstone` package (`ModuleNotFoundError: No module named 'skcapstone'`).
It fails identically at the untouced base revision.

Focused suite:

```
PYTHONPATH="$PWD" python3 -m unittest discover -s tests -p 'test_fiber*.py'
Ran 124 tests in 0.669s - FAILED (failures=1, errors=3)
```

All four failures/errors pre-exist at base revision
e6d4139775682ae596c182168cd2f059aafff5b9 and were reproduced with the
working tree stashed, so this change adds zero new failures:
- test_fiber_legacy: missing `skcapstone` host package.
- test_fiber_control.test_owned_or_missing_dependency_never_admitted:
  missing `skcapstone` host package.
- test_fiber_worker.test_source_metadata_and_links_must_agree: missing
  `skcapstone` host package.
- test_fiber_sandbox.test_root_has_no_host_network_home_or_runtime_socket:
  in this worker the private HOME equals `/work/.fiber-runtime/home`, so
  the test's `str(Path.home())` substring assertion is a test-environment
  artifact, not a regression. (Same assertion failed at base.)

The new environment tests all pass; full-suite SKCapstone dependencies are
unavailable here and were not installed, per the card.

## Safety properties preserved

- Namespaces unchanged: `--unshare-all`, `--unshare-user`,
  `--disable-userns`, `--cap-drop ALL`, `--clearenv`, `--die-with-parent`.
- No host network, no host credentials, no Docker socket, no `/run/user`.
- Exactly one writable mount remains: the task `/work`.
- The test environment is mounted `--ro-bind` only, at the fixed private
  prefix `/opt/test-env`; its `bin` is prepended to the private PATH only.
- Only validated repository-relative source directories are added to
  PYTHONPATH as `/work/<dir>`; no arbitrary host path is reachable.
- No task environment is placed in the host process environment; PATH and
  PYTHONPATH are set only via bubblewrap `--setenv`.
- Gateway bridge, pinned route, and cgroup limits are unchanged.
- No controller admission, capacity, routing, or claim logic changed.

## Limitations

- The operator-built environment under
  `~/.local/share/skfleet-fiber/test-environments/<lock_sha256>/` is not
  present in this worker; no real read-only mount or real pytest/pydantic
  import could be exercised here. The mount/probe paths are covered by
  mocked-argv and mocked-subprocess regression tests only.
- `fiber_test_environment.preflight()` is provided but the wired path uses
  `fiber_sandbox.environment_probe`; both implement the same check.
- The full SKCapstone pytest suite requires unavailable dependencies and
  was not run; exact unavailable checks are the four items above.
- Independent GLM review and real operator-built environment qualification
  are still required before installation. This is not a self-accepted PASS.

## Verification of artifacts

- Working tree: /work, branch work/fiber-14380835-67a8aca6.
- No push, deployment, host edit, board write, or external action performed.
- One local-only source/test/evidence commit is created (card explicitly
  requests it) on branch work/fiber-14380835-67a8aca6, parent base revision
  e6d4139775682ae596c182168cd2f059aafff5b9. A Git commit cannot contain its
  own hash, so the authoritative hash of the final commit is captured by
  `git rev-parse HEAD` immediately after the commit/amend and reported in
  the run transcript; it is not embedded in this file. No push, no second
  content commit.
