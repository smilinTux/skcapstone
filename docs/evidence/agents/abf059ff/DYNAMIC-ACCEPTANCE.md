# Dynamic production receipt reconciliation

This follow-up implements the user's correction after the prior static-model
candidate d0851cad passed independent review. That prior source and review remain
historical evidence. Production policy now contains enabled families, authority
and resources; gateway metadata and the exact card contract determine models.

`persist_production_snapshot(home, snapshot)` writes one private immutable
content-addressed observation per sealed revision. It refuses stale or unsealed
inputs. The reference contains path, byte hash and capacity revision. Loading
checks the expected path, bounded regular nofollow descriptor, owner, private
mode, content hash and seal. Existing mismatched files are never overwritten.

The native review receipt accepts only the one additional production_snapshot
field. The new native ordinary production receipt records an exact current
owner and claim through the existing CardStore event API. Both refuse invalid
snapshot references before publication. Existing review recommendation and
source-head constraints remain unchanged.

Seat verification replays the shared route resolver on that prelaunch snapshot
against the card's current size, privacy and provider constraints. It matches
the exact model, backend, native event writer, claim and authority. Freshness is
measured against the native launch timestamp, not the later verification time.
Current full gateway capacity therefore does not invalidate a successful
admission. Native claim, source, independent recommendation, live-unit and
failed-release checks remain in their existing verifiers.

Validation: 281 passed, zero failed, zero skipped across thirteen focused suites.
The first broad run exposed a test-only /proc exit race in the existing real
process-group cleanup test; the test now accepts ProcessLookupError when the
child disappears between checking and reading. Both run logs are retained.
New tests cover real private snapshot files, tampering, symlinks/FIFOs, expired
selection, changed card requirements, replayed claims, native receipt refusal
without writes, idempotent native receipt publication and all four route families.
No actual model test or deployment is claimed here.

Parent owns source composition, install, activation and rollback. No runtime
files or active claims were changed by these source tests. Rollback restores
the exact previous module bytes while retaining all immutable observations and
native events for audit.
