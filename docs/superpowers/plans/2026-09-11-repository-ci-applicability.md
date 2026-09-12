# Repository CI Applicability Implementation Plan

> For agentic workers: execute this single task with the available executing-plans workflow or equivalent inline TDD. No additional delegation or approval round is required by this plan.

**Goal:** Allow repository-bound CI completion without weakening legacy review gates.

**Architecture:** Verify a checked-in manifest against immutable base/candidate Git objects at card creation, then persist its bytes and pins in `CardCore.meta`. Validate one pinned result receipt at the existing shared completion gate.

**Tech Stack:** Existing Python/Pydantic/pytest stack; standard-library JSON, hashlib, pathlib, subprocess, and datetime. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-11-repository-ci-applicability-design.md`.

## Global constraints

- Source base: `46914a4502541491052f782a0416b0b5b85a8abd`; use an isolated worktree and claim the implementation card before edits.
- Scope: source and tests only; no installation, workflow edits, board migration, or repository enrollment.
- Preserve canonical `PASS`, six-check legacy fallback, existing authorization, and all four shared completion entrypoints.
- Manifest maximum 64 KiB; Git calls have a 10-second timeout, no shell, and no network.
- One source card, one reviewed commit; do not create a card per test or helper.

## Task 1: Implement and verify the complete profile boundary

**Files:**

- Create `src/skcapstone/ci_applicability.py`: strict manifest/capsule/receipt validation and read-only Git binding.
- Modify `src/skcapstone/review_verdict.py`: select immutable profile validation or unchanged legacy checks; correct the stale negative-verdict comment.
- Modify `src/skcapstone/cli/coord.py` and `src/skcapstone/mcp_tools/coord_tools.py`: optional creation input and shared validated metadata.
- Create `tests/test_ci_applicability.py`; extend `tests/test_review_verdict.py` and `tests/test_coord_completion_parity.py`.
- Read `src/skcapstone/coord_completion.py`, `src/skcapstone/source_binding.py`, and `skcoord.card_store.CardCore`; their interfaces need no changes.

**Interfaces:**

```python
def bind_ci_profile(meta: dict, request: dict) -> dict:
    """Return the validated immutable capsule; raise ValueError on any failure."""

def validate_profile_completion(card_id: str, home: Path, core: dict) -> None:
    """Validate the stored capsule and latest pinned receipt, or raise ValueError."""
```

Both functions live in `ci_applicability.py`. `meta` contains the already validated source binding and optional existing review head. The binder accepts exactly the three request fields and returns exactly the six capsule fields defined in the spec. The completion function receives parsed immutable core, never a folded mutable Card projection.

- [ ] Add parameterized synthetic Git repository tests for Node, Python, and mixed profiles. Each fixture initializes a local repository, sets a credential-free synthetic HTTPS origin, commits the manifest, and creates a descendant candidate with unchanged policy. Build expected metadata from actual commit IDs and SHA-256, not hard-coded fake object IDs. A minimal passing binder assertion is:

```python
capsule = bind_ci_profile(meta, request)
assert capsule["candidate_revision"] == request["candidate_revision"]
assert capsule["profile_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
assert capsule["manifest_text"].encode("utf-8") == manifest_bytes
assert "repository_path" not in capsule
```

- [ ] Add failing binder cases by changing one fixture input at a time: missing manifest; invalid JSON/UTF-8; duplicate JSON key; unknown field/version; wrong origin or manifest repository; multiple origin URLs or a subdirectory path; shortened or wrong candidate/base SHA; non-ancestor base; bad digest; missing source-binding field; mismatch with review head; symlink or oversized manifest; changed manifest between base and candidate; empty check set; all-inapplicable set; omitted legacy key; missing NA reason; unknown expected state. Each must raise `ValueError` before card creation. Simulate timeout and Git read failure with subprocess mocks.
- [ ] Run `PYTHONPATH=src python -m pytest -q tests/test_ci_applicability.py`; expect collection/import failure for the missing helper, then assertion failures as its interface is introduced.
- [ ] Implement the binder with exact schema/key checks and rejection of duplicate keys using `json.loads(..., object_pairs_hook=...)`. Inspect tree mode and blob size before bounded blob reads. Use literal argument arrays with exact validated hex object IDs and fixed path `.skcapstone/ci-profile.json`. Compare raw bytes at base/candidate, decode strict UTF-8, recompute SHA-256, and return the specified capsule. Wrap filesystem, decoding, timeout, and Git command failures as actionable `ValueError` with no credentials or raw evidence in errors.
- [ ] Add profile completion tests by extending the existing `_home` fixture with immutable `meta` and a `ci_applicability` JSON receipt. Keep all existing legacy tests. Parameterize each profile result against these rejection states:

```python
@pytest.mark.parametrize("state", [
    "SKIPPED", "PENDING", "FAILURE", "CANCELLED", "UNKNOWN",
    "success", "SUCCESS extra", "", "NOT_APPLICABLE",
])
def test_required_profile_check_rejects_other_states(profile_home, state):
    home, core, append_receipt = profile_home
    append_receipt(check="ci_check_docs", state=state)
    with pytest.raises(ValueError):
        validate_profile_completion("bbbbbbbb", home, core)
```

`profile_home` is a new local test fixture returning synthetic home/core and an append helper. The helper emits actual `action/link_key/link_value/ts` rows under the existing temporary evidence layout and preserves all other valid receipt fields. It must never write to the live board.
- [ ] Cover exact NA reason/reference, omitted/extra checks, missing evidence, malformed receipt, wrong repository/head/profile hash, newer invalid receipt, duplicate-identical rows, equal-time conflicting rows, missing/naive timestamps, unreadable evidence, malformed/null/tampered capsule, and absent/malformed core. Assert old `SUCCESS` links and mutable `ci_profile`/required-check links cannot override a profile or reduce the legacy six checks.
- [ ] Implement receipt selection and complete validation in the helper. Parse timestamp instants with explicit timezone, require the latest whole receipt, and reject ambiguity without mixing rows. Retain the existing evidence field aliases. In `review_verdict.py`, keep early non-review return and verdict checks, then branch only on membership of `ci_profile` in immutable metadata:

```python
if "ci_profile" in core.get("meta", {}):
    validate_profile_completion(card_id, home, core)
    return
checks = unsuccessful_checks(card_id, home)
```

Load core with explicit read/JSON/object failures before this branch; keep the current legacy error if `checks` is nonempty. Reject malformed `meta` rather than treating it as absent.
- [ ] Expose CLI `--ci-profile` as one JSON string and MCP `ci_profile` as an object with the three exact properties. After existing source/review metadata construction and before any board write, use the same binder:

```python
if ci_profile is not None:
    meta["ci_profile"] = bind_ci_profile(meta, ci_profile)
```

CLI decodes JSON and reports `click.ClickException`; MCP reports its existing error response. Preserve authorization checks. Extend the parity module with creation tests for equivalent immutable capsules and zero created cards after bad inputs. Do not add a general-purpose mutable metadata flag.
- [ ] Extend `_review_home` to construct a profile fixture in temporary storage. Parameterize the existing `_invoke` over `cli_complete`, `cli_move`, `mcp_complete`, and `mcp_move` for valid Node/Python/mixed receipts, missing receipt, NA masquerading as success, stale candidate, malformed capsule, and a newer failed receipt. For each rejection assert unchanged `Board(home).load_agent("reviewer").current_task` and unchanged folded lifecycle column. Keep legacy parity assertions intact.
- [ ] Run `PYTHONPATH=src python -m pytest -q tests/test_ci_applicability.py tests/test_review_verdict.py tests/test_coord_completion_parity.py tests/test_card.py`. Expected: all pass. Run the repository's existing formatter/linter on changed Python files only. If the local environment cannot collect the pinned source, record the exact dependency mismatch; do not install or fall back to testing a different checkout.
- [ ] Review the diff against every spec acceptance case, check no runtime or live data changed, then commit the seven named source/test files together with message `feat(coord): bind review CI applicability to repository policy`. Link the exact commit, focused test results, and acceptance evidence through `skcapstone coord`. Record `PASS` and complete the implementation card only after its own criteria are satisfied; independent review remains the existing downstream gate.

Rollback before deployment: revert that one implementation commit. Leave historical cards and evidence untouched. Runtime activation and rollback compatibility for profile cards require a separate authorized rollout.
