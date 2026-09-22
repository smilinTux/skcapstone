# September 21 Seraph activation checkpoint

Card: 99bc5a12. Owner: jarvis. Current mediated claim revision: af1325f9be984c8a9eb004b47b31a1a9. Dependencies were complete and live gates returned eligible. User confirmed chiap01 returned from maintenance and expressly authorized use of its API key.

## Completed source repair

Base: 5c50b01ca31b1954d1e53e753af62dd55ded12fd. Isolated clone: /home/skuser01/work/skcapstone-99bc5a12-attestation-0921. No commit or push.

The actual credential attestor requests the exact SKLegal repository metadata endpoint. The production HTTP allowlist previously admitted its descendants but rejected that endpoint. Added the exact root to the path allowlist; method and payload checks continue to reject root writes. No other repository was admitted.

Files: src/skcapstone/forgejo.py, tests/test_forgejo.py, tests/test_seraph_forgejo.py, changelog.d/99bc5a12-repository-attestation.md, and this evidence file.

The new real-client/fake-HTTP-opener attestation regression failed before the change with forge_path_not_authorized. After the one-line production repair, the existing eight-suite verification command passed 233 tests in 17.37 seconds. Black check passed for all three changed Python files; git diff --check passed.

Independent session reviewer approve_pr2 inspected the exact four-file source candidate without editing it: 92 focused tests passed in 0.64 seconds. An in-memory negative control removing only the production addition caused the new regression to fail at the expected call. Sibling repository roots and root POST remained blocked before transport. This is independent source PASS, not a live deployment or formal forge approval.

| Source file | SHA-256 |
| --- | --- |
| src/skcapstone/forgejo.py | 6d97e5c2459c4971c425c48a5fd393580dfedc87191bc4592380ab1b79787fca |
| tests/test_forgejo.py | 0f3b903de949d0023600f084785f8e552cedc50e3337f9f7020925c29d7ee454 |
| tests/test_seraph_forgejo.py | 7bc9bcd18d24153c3756f87a4d3ab56fc7873bc67d3e35de1cf1c30e987b7218 |
| changelog.d/99bc5a12-repository-attestation.md | f4c9599477adad9b550ebc74fab3164d8a5a8027dc43899c538ddca6075ec958 |

Tracked source diff SHA-256 before this additive evidence file: 898372f914a564412d7e66f8480b5679a27dabb65f03323793f9bb5b4f7c0872. No repaired runtime was installed or invoked against the live approval endpoint.

## Credential ceremony and actual changes

Used existing chiap01 ~/api-keys/chef-skgit.env in place over authenticated SSH. Mode 0600; live /user identified chefboyrdave2.1 with administrator true. Its key was not printed or copied. The casey-jarvis credential lacked the self-user read permission and was not used for provisioning.

Verified existing service user seraph-review-bot, ID 8, restricted true, administrator false, active true. Team 3 is sklegal-seraph-reviewers, has only smilinTux/sklegal, code read and pulls write, no broader units or repository-creation permission. Existing token list was empty.

Forgejo rejects PAT-authenticated token creation with HTTP 401, auth method not allowed. Under the previously authorized dedicated-account setup, the existing bot login password was replaced with a generated credential and stored on chiap01 in ~/api-keys/seraph-skgit-account.env, mode 0600. That is the only live account mutation in this checkpoint. Existing restrictions and membership were retained. The credential's value was never printed or copied. Basic authentication then correctly identified seraph-review-bot.

The requested token creation with scopes read:organization, read:user, write:repository and the exact SKLegal repository returned HTTP 400. The reverse proxy replaced API details with a generic SKStacks HTML error. Bounded diagnosis established the primary-source constraint below. No reviewer PAT was created, no CapAuth enrollment/class/grant was changed, no publisher unit was installed/enabled, no review was posted and no composition hold was removed.

## Version-specific blocker

Live /api/v1/version reports 15.0.9+gitea-1.22.0. Its Swagger confirms the repository-scoped token request shape.

Primary source: https://codeberg.org/forgejo/forgejo/src/tag/v15.0.9/services/authz/access_token.go

validateRepositoryResource permits repository-specific tokens to contain only read:issue, write:issue, read:repository or write:repository. It rejects read:user and read:organization. The documented single-token publisher contract requires exactly the two rejected read scopes plus write:repository. It therefore cannot satisfy the deployed server's validation rules. Removing repository scoping would broaden the credential and was not attempted.

Separate outstanding CapAuth prerequisites: Seraph's exact existing fingerprint and signing key were found, but the current root registry has no Seraph device enrollment or identity-class assignment and decide denies publication. No grant was fabricated to make this pass.

## Proposed bounded continuation

Replace the impossible single-token contract with separate read-only identity/team attestation and SKLegal-only write credentials. Both must be bound by trusted provisioning metadata to the same existing non-admin reviewer account. The writer never receives an unrestricted repository grant. Tests must reject swapped account/token bindings, broad scopes, missing live attestation, wrong repository, changed hashes, stale evidence, stale head and changed protection.

Freeze that scope in the existing activation task before implementation and obtain independent review of the changed authorization contract. Then finish VERIFIED enrollment and the approved signed bounded grant, validate one exact PR2 canary, verify remote review and immutable receipt, and only then extend to PR3/PR4. Protected-source acceptance and persistent installation remain separate gates.

## PR state and rollback

Independent current PR2 readback: exact 216bd59b3562d078e38a45cb3cf6c41024ac5dc9, required CI success, open/unmerged, no reviews. Main protection requires one approval, rejects stale/outdated/rejected reviews, disables direct push and applies to administrators. Existing PR852 and PR854 are merged with 11 successful checks and verified native independent source PASS records; those reviews do not cover a new credential-contract change.

Source rollback removes this isolated uncommitted repair, retaining evidence. No installed source rollback is required. The bot password's previous unknown value cannot be restored; preserve the new owner-only credential, or rotate it again through authorized account management. Do not delete the reviewer account or its historical records. Token revocation is unnecessary because no replacement token exists. Recheck remote token list before any future issuance to avoid ambiguity or duplicates.
