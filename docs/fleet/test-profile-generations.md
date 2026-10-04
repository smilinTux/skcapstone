# Qualified test profile generations

Card fa8a7b65 adds an operator API, not automatic qualification or deployment.
After actual recipe qualification against the intended runtime, call
`production_test_profile.supersede_profile` with the exact live source claim
owner and claim revision, predecessor byte SHA256 from `read_profile`, new
qualification evidence SHA256, and qualified runtime SHA256. The source card
must remain Doing, unarchived and unconflicted with the same repository and
acceptance criteria. Native card mutation locking covers validation and append.

For an unclaimed Backlog or Ready source card, an operator may instead pass
`unclaimed=True` with no `source_claim`. The same native lock verifies the card
is unowned, unarchived and unconflicted with unchanged repository and criteria.
Its full folded card SHA256 is stored in a v2 successor envelope. This permits
requalification before preclaim, which otherwise cannot occur when the old
profile blocks dispatch. It does not claim the source card or change its work.

The original `fleet/test-profiles/<card>.json` remains immutable. Successors
are private exclusive-create records in `fleet/test-profiles/<card>/`, named
for the exact predecessor byte hash. No mutable current pointer is introduced.
At most 128 total generations can be read; further writes refuse. Preflight
and new candidate sealing use the latest generation. Existing sealed plans
resolve their embedded historical profile and retain all runtime, interpreter,
host and policy checks. A historical plan is not executable on a new runtime.

When a sealed candidate has not launched its native test unit, a freshly
qualified profile can seal an append-only successor plan for the same source
head. The original plan remains immutable. Successor files sit beside it,
named by the predecessor plan SHA256, and bind that predecessor in the new
record. The old run lock fences publication against a concurrent launch; a
prior `launch.json` refuses succession. Readers validate the whole chain and
execute only its current generation. A runtime or test policy change without
fresh qualification still refuses.

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

The runtime hash covers the installed Python test toolchain, interpreter
startup `.pth` contents, and the trusted SKCapstone test executor modules.
Distribution `RECORD` files and unrelated package source are not test
qualification inputs. Candidate source, the approved recipe, card criteria,
policy, interpreter, and actual test results have their own checks. A main
rollout that changes only unrelated package code or wheel provenance therefore
keeps existing profiles usable. A change to the toolchain, startup path, or
trusted executor still requires governed qualification before new producers
launch. Profiles made with the previous whole-package hash need one final
qualification after this change lands; their immutable history is preserved.
