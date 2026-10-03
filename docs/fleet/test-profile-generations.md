# Qualified test profile generations

Card fa8a7b65 adds an operator API, not automatic qualification or deployment.
After actual recipe qualification against the intended runtime, call
`production_test_profile.supersede_profile` with the exact live source claim
owner and claim revision, predecessor byte SHA256 from `read_profile`, new
qualification evidence SHA256, and qualified runtime SHA256. The source card
must remain Doing, unarchived and unconflicted with the same repository and
acceptance criteria. Native card mutation locking covers validation and append.

The original `fleet/test-profiles/<card>.json` remains immutable. Successors
are private exclusive-create records in `fleet/test-profiles/<card>/`, named
for the exact predecessor byte hash. No mutable current pointer is introduced.
At most 128 total generations can be read; further writes refuse. Preflight
and new candidate sealing use the latest generation. Existing sealed plans
resolve their embedded historical profile and retain all runtime, interpreter,
host and policy checks. A historical plan is not executable on a new runtime.

Malformed, disconnected, ambiguous, redirected and over-bound chains refuse.
This filesystem evidence boundary cannot detect removal of the entire final
suffix when no retained caller pin refers to it. Removing an interior record
with surviving descendants refuses. Exact current runtime validation makes an
old profile unusable after runtime replacement even if a suffix disappears.
A deleted historical profile required by a sealed plan also refuses. Private
owned evidence storage and external retention remain necessary.

The API trusts the operator-supplied hash of actual new qualification evidence,
as the existing qualification API does. It does not run recipes, authorize
claims, release reservations, repin plans or restore automated admission.
Installation requires the separate rollout barrier and native qualification.
