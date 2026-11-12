# c0e135f9: worker monitor reporting

## Scope and reproduction

The chiap01 monitor recognized only `skfleet-worker-*.service`. A live Pi
process in a `skfleet-builder-<card>-<request>-<attempt>.service` cgroup was
therefore reported as an orphan. Eight old terminal Pi processes were counted
alongside current native workers. A stale projection was described as having
no exact claim even when its canonical owner still matched.

## Changes

- Recognize both native unit shapes from exact process cgroups.
- Compare process claim revision and owner with the folded card. Missing
  revision is unverified, not exact. Unit-only rows cannot prove a claim.
- Count current claimed unit processes separately from all observed Pi
  processes, legacy sessions and projection/unit diagnostics.
- Native builder units without Pi remain visible as unit activity, since
  preparation and tests run without Pi. Historical inactive native units
  are excluded from current jobs.
- Default output prioritizes current processes. `--details` retains legacy
  rows, full worker identities, unit names and claim revisions.
- Process liveness does not claim model request activity. Historical terminal
  sessions are not automatically labelled orphaned or terminated.
- An unavailable systemd observation fails the host collection explicitly.

## Verification

- `python3 -m pytest -q tests/test_skfleet_working.py`: 65 passed.
- New regressions initially failed on native cgroups and summary semantics.
- Ruff, Black check and `git diff --check`: PASS.
- Read-only live collection observed three exact current unit processes on
  three hosts and eight legacy Pi sessions. The previously false orphan on
  chiap04 was correctly joined to its active native builder unit.
- No worker, policy, toolchain, service or unrelated card was changed.

## Acceptance and limitations

Source candidate only until independent review, green CI and merge. The
chiap01 installed readback will be attached separately. Counts are a rolling
observation across hosts, not an atomic fleet snapshot or a throughput metric.
No inference activity is established from empty or buffered logs. No legacy
session is declared idle or dead without session-specific evidence.

## Rollback

Revert this candidate on main. Preserve the previous chiap01 monitor bytes
and hash before the governed installation; restore that exact file if the
installed readback fails. Monitor cache is observational only. No coordination
or worker state migration is involved.
