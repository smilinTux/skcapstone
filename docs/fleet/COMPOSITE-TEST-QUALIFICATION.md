# Combined frontend and API test qualification

Some source-only cards require both the complete frontend unit suite and Python
API regressions. A frontend-only profile does not qualify that contract.

The governed operator APIs `qualify_profile` and `supersede_profile` select
`skfleet.qualified-composite-test-profile/v1` when the approved recipe includes
both `vitest` and the complete existing Python recipe fields, and the operator
supplies the qualified `node_environment`. Existing Python-only and Node-only
profiles retain their schemas and execution contracts.

The recipe combines the existing `vitest` per-file coverage inventory with
`pytest`, `compile`, `lint`, and `changelog`. Python `file_cap` and exact
`deselect` baseline evidence remain available. Every target goes through the
existing literal-path parser; shell text and arbitrary command arguments are
not supported. Use the complete actual frontend file inventory, not a sample
subset: unexpected Vitest files fail acceptance.

The fixed order is Vitest, TypeScript, ESLint, then Python pytest and the
selected Python checks. The Python Ruff check is named `python-lint` so it
cannot overwrite the independent ESLint log. Each command executes in the
existing network-isolated sandbox, with frontend cwd `/work/apps/web` and
Python cwd `/work`. Node phases use the approved immutable dependency artifact;
Python phases use the pinned runtime and qualified collection plugin. Neither
phase gets writable candidate source or inherited host credentials.

The worker preserves `vitest.xml` and `pytest.xml` separately. Its JUnit digest
binds both raw-file digests; counts contain independent `node` and `python`
phase results and their total. Native acceptance rehashes both files and
recomputes both strict coverage results, checks every command/log/exit code,
and requires the exact native invocation and unchanged source. Missing,
tampered, failed, errored or skipped phase evidence refuses acceptance.
Historical acceptance rechecks these same preserved files without rerunning
candidate code. Directory/baseline Python recipes also retain exact selection
evidence.

Qualification remains an operator action after an actual native run. Do not
edit a profile, substitute a previous source's result, publish a profile from
a failed run, or hide API checks in a Vitest wrapper. Host-local admission,
source/claim custody and trusted remote artifact transport remain required.
This capability covers frontend and Python test phases; a card's real browser
journey, fresh synthetic authority, protected-data limits and independent
product review remain separate acceptance requirements.

This executor change deliberately updates the scoped runtime fingerprint.
After normal rollout from merged main, run governed qualification against the
installed harness for any needed profiles. Preserve all old receipts and
append-only profile generations. Running retained workers are not restarted
by this change. Rollback uses a reviewed main revert and ordinary rollout,
followed by qualification for the restored harness.
