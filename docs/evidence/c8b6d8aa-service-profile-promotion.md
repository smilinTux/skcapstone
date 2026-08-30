# c8b6d8aa service-profile promotion evidence

Card: `c8b6d8aa`
Repair scope: AC1 of preserved FAIL `90ebc6a7` only.
Agent: `codex-skpm-prof-04r`
Repository: `skcapstone`
Candidate worktree: `/home/skuser01/worktrees/skpm-prof-04r-c8b6d8aa`
Dependency candidate read-only worktree: `/home/skuser01/worktrees/skpm-prof-01-a0a89b24`

## Provenance

- Dependency candidate revision read: `45a00b3df1595c30bcbe285e673e2277ca35b2ed`.
- Clean candidate base revision: `e9b81135cfe9a7e0b8b4bcfd2e3320aba70f4426`.
- Clean candidate base tree: `89db31ecb42a3a408246805b847e3659abdf077c`.
- Base is `refs/remotes/origin/main` from the dependency checkout.
- The dependency worktree and the user SKLegal worktree were not modified.
- No deployment, restart, credential access, protected data access, Portfolio mutation, external action, merge, push, or unrelated cleanup occurred.

## Task-owned payload

The candidate contains only the reviewed SKCapstone profile payload copied from the dependency candidate:

- `MANIFEST.in`
- `pyproject.toml`
- `src/skcapstone/__init__.py`
- `src/skcapstone/cli/agent_profile_cmd.py`
- `src/skcapstone/data/sk-agent-picker.sh`
- `src/skcapstone/data/profile-conformance-v1.json`
- `src/skcapstone/profile_registry.py`
- `tests/test_agent_profile.py`
- `tests/test_multi_agent.py`
- `tests/test_profile_registry.py`
- `docs/evidence/a0a89b24-profile-registry.md`
- `docs/evidence/c8b6d8aa-service-profile-promotion.md`

The shared fixture contains exactly 15 public synthetic cases. Its canonical pack hash is `sha256:52f74fca0abbb0ad8fe54fc550b83827175eff1f14b5fa3aad0140ad9a8a56e1`, and its whole-file SHA-256 is `0d0c18d21b24e05f9cb18289c9a9e0d50b1e21c21a1046d4b0e5fc84bf29d1a6`.

## File hashes

SHA-256 values were computed in the clean candidate worktree before the local candidate commit:

```text
MANIFEST.in a8a46381e70ee2d5916621b9bc5d6301a87e77d300710552b2f51612e21500a4
pyproject.toml efa982b9ea12ec26b3316bb4c61ad74563244a6dece113801c9c8792ec815f19
src/skcapstone/__init__.py 63f73a6d0d6ee103182011b220e573bc3cedd95e3ef917b8d52b27366b73f7c4
src/skcapstone/cli/agent_profile_cmd.py f36c4be975cbe43515b6dc580c52f4c9d64b1e3dd652cdf493042255a41015e9
src/skcapstone/data/sk-agent-picker.sh 0f9340a661a8508bf4b77ccbd48f3daa645fc4572eb8ce837a90ce82156185f0
src/skcapstone/data/profile-conformance-v1.json 0d0c18d21b24e05f9cb18289c9a9e0d50b1e21c21a1046d4b0e5fc84bf29d1a6
src/skcapstone/profile_registry.py 7511d561dedc3201256c63bc8b0b901641efc08d20b60b641ec08289306dc983
tests/test_agent_profile.py b2dc854ebc92f3b58dd97c62fff072f3984c362b0a55561be2465f96c9fe0a03
tests/test_multi_agent.py 1c54038b292d3d1a42ab259d504175f7775ecaf959cb537795325b20dfdba9fd
tests/test_profile_registry.py 4988c22a35be3c455fe03fbbc25b384a3bd6ae8cdc4cc6e4491e39a2db121d5a
docs/evidence/a0a89b24-profile-registry.md ca8faf7187d0f14882c60b0ca69e733dafd9f02bd6708f33773bbf8a6bdce07a
```

The final candidate commit, tree, task-payload diff, full diff, rollback diff, task manifest, focused test receipt, and evidence file SHA-256 values are published as immutable links on card `c8b6d8aa`. The commit is local and detached from any remote branch. The evidence file is part of the candidate commit.

## Verification

Focused tests:

```text
python -m pytest -q tests/test_profile_registry.py tests/test_agent_profile.py tests/test_multi_agent.py
54 passed in 1.21s
```

Focused receipt SHA-256: `118d874d9642a40d4c621f12ade7ad61a1fde96e2e757c5b87732290a5d86623`.

Focused static and diff checks passed:

```text
python -m ruff check src/skcapstone/profile_registry.py src/skcapstone/__init__.py src/skcapstone/cli/agent_profile_cmd.py tests/test_profile_registry.py tests/test_agent_profile.py tests/test_multi_agent.py
python -m black --check src/skcapstone/profile_registry.py src/skcapstone/__init__.py src/skcapstone/cli/agent_profile_cmd.py tests/test_profile_registry.py tests/test_agent_profile.py tests/test_multi_agent.py
bash -n src/skcapstone/data/sk-agent-picker.sh
git diff --check
```

Relevant full-suite check:

```text
python -m pytest -q
6563 passed, 41 skipped, 2 pre-existing failures
```

The two failures are unrelated inherited failures in `tests/test_coord_amend.py::test_skcoord_dependency_requires_criteria_fold_release` and `tests/test_dashboard.py::TestSidebarNav::test_all_nav_destinations_present`. The focused profile tests and all task-owned checks pass.

## Rollback

Rollback is local and source-only. Recreate the clean candidate at base revision `e9b81135cfe9a7e0b8b4bcfd2e3320aba70f4426` with base tree `89db31ecb42a3a408246805b847e3659abdf077c`, or remove only the candidate worktree after review. No runtime rollback is required because no runtime was changed. The exact rollback revision and tree are published on card `c8b6d8aa`.

## Limitations

This card promotes the SKCapstone implementation, shared fixture, focused tests, and evidence only. SKMemory and Jarvis consumers remain separately gated by `b1ce03c4` and `248aa691`. Cross-repository qualification remains the preserved FAIL follow-up path and is not claimed or closed here. The preserved FAIL card `90ebc6a7` remains open and unchanged.
