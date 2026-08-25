# a0a89b24 — fail-closed profile registry evidence

Agent: `pi-skpm-prof-01`
Scope: source and tests only; no deployment, commit, push, production activation, or external action.

## Acceptance evidence

1. `src/skcapstone/profile_registry.py` defines strict Pydantic v1 envelopes for the registry and human/service profiles. Registry entries bind profile kind, selectable, fallback eligibility, explicit memory principal, schema/profile revisions, and SHA-256 profile hash; the registry has its own revision and SHA-256 hash.
2. Resolution returns zero tools, no memory principal, nonselectable, and fallback-ineligible for missing, corrupt, stale, hash-mismatched, conflicting, and unknown-version metadata. A valid service retains only its explicit service memory principal and remains nonselectable, fallback-ineligible, and zero-tool. It cannot borrow human identity or tools.
3. `src/skcapstone/__init__.py` filters active-agent discovery through eligible human profiles. `src/skcapstone/data/sk-agent-picker.sh` applies the same guard to menus, explicit switches, print-mode first-directory fallback, environment selection, and `--agent` selection.
4. Public synthetic fixture pack: `src/skcapstone/data/profile-conformance-v1.json`; canonical content hash excluding `pack_hash`: `sha256:52f74fca0abbb0ad8fe54fc550b83827175eff1f14b5fa3aad0140ad9a8a56e1`; 15 cases. Whole-file SHA-256: `0d0c18d21b24e05f9cb18289c9a9e0d50b1e21c21a1046d4b0e5fc84bf29d1a6`.
5. `tests/test_profile_registry.py` executes every golden vector, mutates every fixture condition to prove sensitivity, asserts no state equals `Unknown`, rehashes adversarial service privilege attempts, and checks discovery/picker/fallback behavior. Existing human profile and discovery tests were updated to construct explicit registered identities rather than rely on implicit broad defaults.

## Checks

- `pytest -q tests/test_profile_registry.py tests/test_agent_profile.py tests/test_multi_agent.py` — 54 passed.
- `python -m pytest -q` — 6513 passed, 41 skipped, 553 pre-existing warnings in 362.89s.
- `python -m ruff check ...` for all touched Python source/tests — passed.
- `python -m black ...` for all touched Python source/tests — passed.
- `bash -n src/skcapstone/data/sk-agent-picker.sh` — passed.
- `git diff --check` — passed.

## Limitations

- This card intentionally does not migrate live agent directories or generate live registry records. Until operators install exact valid records, selection fails closed.
- SKMemory and Jarvis/Mission Control consumer changes are separately card-gated (`b1ce03c4`, `248aa691`) and are not implemented here.
- A wheel smoke build was attempted but the isolated environment lacks the declared build dependency `setuptools_scm>=8`; package-data inclusion is nevertheless declared in both `pyproject.toml` and `MANIFEST.in` and repository tests pass.

## Rollback

Revert only the files listed in this evidence/card report. Removing the new registry module/fixture and restoring the guarded discovery, bridge tool resolution, picker, package-data declarations, and adjusted tests returns the worktree to its starting revision. No runtime state or deployment needs rollback.
