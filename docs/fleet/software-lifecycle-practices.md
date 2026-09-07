# Software lifecycle practices applied to the SK fleet

This is the SKCapstone implementation note for the software lifecycle seats.
It applies only to SKCapstone, SKDashboard, and SKWorld. No other product or
workflow is in scope.

## Practices adopted

1. Small, frequent changes: Link keeps integration work small and exact-head
   fenced. This follows DORA's trunk-based development and small-batch
   guidance.
2. Required automated checks: protected repository settings must require the
   relevant test and security checks on the latest commit. Merge-queue runs
   must receive the same checks as pull requests.
3. One deployment at a time: release and deployment jobs use an environment
   concurrency key. Tank installs only the exact approved artifact and Seraph
   verifies the resulting version, health, rollback, and idempotent rerun.
4. Least privilege: Link and Mero produce recommendations and observations;
   Niobe is the only normal fleet mutation seat after activation; Tank and
   Seraph are card-scoped; ATLAS is frozen; Jarvis is Casey-directed only.
5. Supply-chain evidence: release artifacts carry a content hash, source
   revision, provenance, and SBOM where the product's release pipeline
   supports it. A free or stronger model does not change data or action
   authority.
6. Observable workers: every active worker sends a startup hello, polls
   SKMail and `all` traffic at least every five minutes, emits bounded beats,
   and records a claim revision and progress token. Mail is collaboration, not
   workflow authority.
7. Startup drift detection: before a lifecycle worker is enabled, run the
   read-only seat manifest audit. It checks identity, estate backing, mailbox,
   model, card-contract, and beat configuration without reading private key
   material or changing runtime state.

## Mapping to seats

| Lifecycle need | Seat | Durable output |
| --- | --- | --- |
| Integration quality | Link | exact-head recommendation and merge evidence |
| Convergence and blockers | Mero | typed read-only observation and recommendation |
| Fleet coordination | Niobe | fenced claim, launch, release, or reassignment event after activation |
| Artifact release and install | Tank | exact artifact and deployment receipt |
| Independent verification | Seraph | PASS, FAIL, or BLOCKED evidence |
| Operations | ATLAS | frozen observation until its separate action contract is authorized |
| Casey assistance | Jarvis | Casey-directed assistance only |

## Human interruption policy

Routine observation, documentation, mailbox handling, evidence collection,
shadow work, non-production canaries, rollback rehearsals, and independent
verification are notify-only. Interrupt Casey only for external authority,
production authorization, protected-data egress, legal or financial
commitment, an irreversible material effect, or a role-contract exception.

## Current runtime state

The Link, Mero, and Niobe unit templates are present under `systemd/` and are
designed for chiap08 as the active host. Mero's ten-minute bounded census timer
and Niobe's five-minute shadow timer are enabled on chiap08. Link remains
installed but disabled because it consumes the mediated PR observation feed and
must record `observation_feed_missing` or another bounded rejection until a
fresh valid feed is present; it must not read GitHub credentials directly. The
producer candidate has passed independent review, but the live lineage audit
still contains unresolved records, so the feed remains incomplete and the Link
timer stays disabled. The control-plane record is
`scripts/fleet/seat-control-plane.json`.

The seat preflight is:

```bash
python3 scripts/fleet/seat-manifest-audit.py --home "$HOME/.skcapstone"
systemctl --user is-active skfleet-mero.timer skfleet-niobe-shadow.timer
systemctl --user is-enabled skfleet-link.timer
```

It must report `healthy: true` before enabling a lifecycle unit. A failed
preflight is a notification and bounded stop, not an automatic repair or a
human interruption.

## External-control readback

The read-only GitHub API audit on 2026-09-06 found active CI workflows for
`smilinTux/skcapstone`, `smilinTux/skdashboard`, and `smilinTux/sk-standards`.
The public ruleset query returned no rulesets for the four named repositories:
`smilinTux/skcapstone`, `smilinTux/skdashboard`, `smilinTux/skworld`, and
`smilinTux/sk-standards`.
The branch-protection query returned HTTP 404 for each `main` branch. That
response is not treated as proof that protection is absent because GitHub can
also hide branch settings from a token without repository-administration
access. The required repository-admin readback remains an explicit external
authority gate on card `05dc9297`.

No lifecycle seat is activated for production authority, and no agent changes
repository settings. Until the exact admin readback exists, Link can produce
bounded eligibility observations only, while Tank and Seraph remain
card-scoped and non-production by default.

The generic fleet scheduler contains legacy estate-wide lanes and escalation
profiles. Those lanes are outside this lifecycle seat contract. Lifecycle seats
use `sk-codex-mid` with `gpt-5.6-luna` by default; a stronger route is a
card-scoped notify-only boost and does not change seat authority or scope.

## Sources

- GitHub protected branches and required checks:
  https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
- GitHub deployments, environments, protection rules, and concurrency:
  https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments
- GitHub required-check behavior on merge queues:
  https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks
- DORA trunk-based development:
  https://dora.dev/capabilities/trunk-based-development/
- OpenSSF Scorecard supply-chain checks:
  https://github.com/ossf/scorecard/blob/main/docs/checks.md
