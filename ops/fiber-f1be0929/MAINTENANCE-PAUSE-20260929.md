# Human-authorized maintenance pause

Parent f1be0929. Operator jarvis. Observed 2026-09-29 23:09 through 23:20 UTC.
The human explicitly authorized pausing other work before proceeding with the
fiber rollout. This supersedes the earlier instruction to wait for natural
drain only. It does not authorize discarding work or bypassing qualification.

## Work actually paused

Thirteen active named Pi crew sessions received a steering instruction to
finish the current tool safely, save .handoff.md, preserve work and claims,
and remain idle. All thirteen handoff files were independently found and
hashed. Final Herdr state for each was done, meaning idle awaiting input,
NOT that its card is complete:

```text
dsw-e6bf25f9     94182f4df9895a9804bb3a0e9bd82685a47c3e8b45821ead7af5df24ce46a4bc
glm2-a8200001    8207be9a93f3c22cf9a4f527fa614299bb7e669329f835f1600ab998e0af0bee
glm2-f7f6daeb    85b043b517dfa73be5dd9dc04e4f75fff74b4734b96f84bd10df6b0e79ec04eb
glm2-daa1cd5f    b3400870b396cee27716548ae4d5cfec9b35771612858fae3c870547aceceb04
glm2-13580287    d4335a997664663e720fd91f8dc06c364a4a2c66a7535d18ed9d135070204a8a
glm2-060e165c    0851efd2b8205a08bb50b9f27714767d8794ea17031d2b70c83e5ddf64d1718a
ds2-02963e5f     7a5269d270789c0f7f594ce91b6942e5fef03966134e44ddea52369ff0abb8fe
ds2-04e788cb     5e053bbf458db9f79705899f5f2bca9dbd157712278f5b2b087d54796a11a197
ds2-1a5cd985     94ec71da8d8f7a76d188edf64029bcc30f3540e6fcf658aaf026726d0957c257
ds2-236377aa    489d28992fd3c2878389f751d722731b497f81130b18fdbc5627e661dcf4a943
ds2-256586ab    271e026b3892a3993fbe80480814413f55793f789851f83d73a58ba4611414ac
ds2-1960b104    3b2c29fb77bc85758ac264f7c6f52983cdb43a0b91b7c17e395b5d3ba4f53ae2
ds2-2509445c    ca26fee350bf519210a58431bbfed37dc138a594a51384be340880ca6e0340bf
```

Handoffs are in each existing worktree. Most ds2/glm2 worktrees are under
/srv/sklegal-fast/work/deepseek-m-crew-20260926/<card>.
Exceptions: e6bf25f9 is under glm-crew-20260926; f7f6daeb and daa1cd5f are
/srv/sklegal-fast/work/skcapstone-<card>.
No root-agent claim release, commit, push, merge, worktree deletion or reset.

## Previously unaccounted launch path

/srv/sklegal-fast/work/deepseek-m-crew-20260926/babysitter.sh, PID 997985,
was continuously reviving non-working GLM/DeepSeek panes. It does not inspect
the fleet HOLD or estate capacity. It actually resumed f7f6daeb after the
worker had acknowledged PAUSED. A subsequent nudge reached daa1cd5f too.
Both were paused again. f7f6daeb acquired its own claim during this unwanted
continuation; that claim was retained, not rolled back or stolen.

The root agent suspended the exact babysitter PID. The existing orchestration
Pi session then acted on the maintenance instruction and stopped the known
loops. PID 997985 was subsequently absent. Do not describe it as still frozen
or use SIGCONT against a potentially reused PID. Script bytes remain intact:
SHA256 8fa1d7ecb4b1f11757fd47ddc45b278964819eb5195269f8435d681207796797.
It must not be restarted alongside the new dispatcher unchanged.

The separate Codex-backed Pi session in pane w7D:p1, PID 272237, repeatedly
auto-compacted at 124-130 percent of its declared 128k context and retried
after cancellation. Native escape cancellation and temporary process suspend
were used; its shell/TUI transition left encoded control-key text at the
prompt, which was cleared. After restoring the foreground job, literal native
Ctrl-C twice exited Pi normally. Final PID absent, pane shell retained, no
remaining chiap08 TCP connections to port 18790. No process tree was killed.
Its saved session is retained at:
/home/skuser01/.pi/agent/sessions/--mnt-cloud-onedrive-projects-DAVE-AI-sklegal--/2026-09-26T08-24-14-975Z_01a0dcd0-9f7f-7f40-bfe2-161394ddd867.jsonl
Do not revive that overfull session blindly; use a reviewed compact handoff
for a fresh session after maintenance. There was no completed root-worktree
.handoff.md from it. Saved history is not a claim of completed handoff.

## Scheduled mutations paused, with rollback

The exact three-entry user crontab was backed up and compared byte-for-byte
immediately before installing a commented maintenance version. Entries paused:
nightly report publishing, reboot-triggered report publishing, and the
five-minute cross-repository auto-merge sweeper.

- Backup: backup/crontab-before-maintenance-20260929
  SHA256 1e63d4787a3b59c220c6a5602c2634652c79beed1d16e74cef85bad02bd7c83c
- Installed: crontab-maintenance-20260929
  SHA256 d8ddbedcc5ab8d17d85c71a081fbad9a0a4905d226266d9f55b8994aff79cb7d

Restore only after explicit resume: compare current crontab against the exact
maintenance file, then use native crontab to install the backup. If it differs,
preserve concurrent edits and reconcile. Do not restart the old babysitter.
Other heartbeat, auth sync, application, gateway and storage services were
not stopped by the root agent. Native Codex terminals outside Herdr were not
blindly signalled. Their live processes still consume conservative capacity;
this report does not claim every interactive terminal in the estate is paused.

## Gateway evidence

Serving unit on chiap01 is skgateway-codex.service, PID 999637, working directory
/home/skuser01/work/skgateway-86237bb9. Its open SQLite FD confirms the metrics
DB actually is /home/skuser01/work/skgateway-ab1608f9/data/metrics.db. A different
working directory is not evidence that the reported metrics DB is stale.

Read-only trailing-hour queries showed:
- Codex: eight HTTP 499/cancelled records, near 300 seconds, first bytes within
  about 1-5 seconds; two HTTP 200 successes. This establishes downstream
  cancellation, not provider saturation. The compaction loop is a plausible
  contributing caller; the DB lacks agent identity, so exact attribution is
  not proven.
- DeepSeek: thousands of HTTP 200 successes, five HTTP 502 failures from curl
  clients at ten-minute intervals, plus five pre-backend HTTP 404 Pi requests.
  Caller classification and cadence distinguish likely probe traffic from
  worker traffic; exact failing probe source remains unverified.
- skgw-lanes labels any nonzero count of status>=400 as BROKEN TRAFFIC. That
  merges cancellation, client errors and upstream failures into one label.
  No dashboard code was changed during this pause.

Fresh synthetic completions through the installed gateway-only qualification
helper returned nonempty DeepSeek and Codex responses:
- deepseek-flash -> deepseek-flash, epoch 1790723657.4531796.
- gpt-5.6-sol -> gpt-5.6-sol, epoch 1790723659.0375674.
- Gateway revision 3abf87ce34e064a6b75182304679e60760020c1e.
Proofs are in ~/.local/state/skfleet-fiber/observations/completion-*.json.

GLM initially returned 503 bucket_no_eligible_member. Live capacity store
proved provider:zai throttled/backend_cooldown, pending its native recovery
probe. Catalog model lifecycle rows were active. Therefore the error's
generic not-routable wording was not proof of lifecycle retirement. No
capacity-store, registry, credential or qualification record was rewritten.
Native recovery changed provider:zai to available/probe succeeded at epoch
1790723936.602. Subsequent sk-zai-m completion returned glm-5.3-flash at
1790723956.621494.

IMPORTANT: transport recovery is not M-class qualification. The live model
card lists glm-5.3-flash as S. The current helper accepts any nonempty glm*
served model and returned ready=true, which is insufficient evidence of the
logical bucket's capability floor. Keep GLM implementation admission held
until requested/resolved/served attribution and qualification are reconciled.
Do not route directly to a provider or lower the floor to make this pass.
Kimi remains fenced; no Kimi probe was made.

## Remaining rollout gate

Fresh eight-host observation at 1790723832.5741687 was complete, no errors,
all hosts runtime-ready. chiap08 MemAvailable rose from 7147756 KiB during
pause to 17589212 KiB; this is a temporal observation, not a causal benchmark.
Collector still conservatively counted idle/unknown native terminals.
Gateway queue reached zero active/queued at 23:15:39 UTC. Later snapshots
included one in-flight request during recovery/cancellation; do not claim
permanent zero traffic. No chiap08 client sockets remained after native Pi exit.

New dispatcher remains disabled and held. No canary worker was launched.
Next: reconcile remaining logical occupancy, review infrastructure changes
independently, enforce served-model qualification, then run the source-bound
implementation/review/test canary. Preserve the maintenance pause on other
work throughout. The original full deployment objective is not complete.
