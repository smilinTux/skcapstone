# Fleet reassessment retention

`skfleet-rotate.service` remains the only execution path. Its existing timer runs the oneshot every five minutes on each fleet host. This change adds no daemon, timer, chat export, or mailbox export.

Every host computes the lifecycle assessment in memory before any existing authorized action. The assessment joins CardStore lifecycle, claims and claim revisions with separate verdict and review evidence. The selector then combines that result with local process and systemd cgroup liveness and readiness checks. Missing or malformed assessment fields stop the cycle before reaping, review transitions, claims, or launches. Ambiguous cards remain excluded and produce only the existing `actions.log` diagnostics.

Only `chiap08` replaces `~/.skcapstone/evidence/fleet-rotation/lifecycle-reassessment.json`. The file is serializer-built, parsed before writing, and capped at 2 MiB. Other hosts write one compact `REASSESSMENT` line to their existing per-cycle `actions.log`; they do not write a full report.

## Retention plan

The authority report is a single replace-in-place derived snapshot, so full-report growth is bounded to 2 MiB without a deletion job. Existing per-cycle `actions.log` directories remain the source for rollback and scheduler audit. A later retention implementation may remove only expired `fleet-rotation/<timestamp>/actions.log` summaries after exporting a hash manifest to protected evidence and proving they are not referenced. It must fail closed on unreadable files, unknown names, missing manifests, or references.

Retention must never delete or rewrite CardStore, claim, verdict, review, rollback, work, or protected evidence. No retention deletion is authorized by this change.
