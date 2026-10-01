# Gateway-owned fleet concurrency

The user explicitly replaces the previous estate, node, provider and spacing
ceilings with SKGateway API provider configuration. This is the approved design.

1. Reproduce old refusal with 100 observed workers, zero legacy slot metadata,
   a qualified provider without a local cap, and several distinct immediate jobs.
2. Remove count and spacing refusals in admission, dispatch and control. Retain
   reservations, exact identities and durable once-only launch recovery.
3. Rank every installed worker host by observed and reserved occupancy. Gateway
   and authority service roles remain distinct from worker installations.
4. Treat host service and gateway health as readiness; memory telemetry remains
   observable. Per-worker resource quota treatment is separately clarified.
5. Preserve source, native ownership, independent review and actual test gates.
   Localize failures so unrelated work is not stopped by one unsuccessful card.
6. Integrate native review route refresh from card f34f26c5 and gateway error
   handling from card 31120409, without overwriting installed usage accounting.
7. Update old policy tests, run relevant fleet and native lifecycle tests,
   obtain independent exact-source review, install with exact backups, qualify
   one real complete work cycle, and then expand useful task supply.

Repository access qualification is card 7481c398. The original work-cycle and
rollout coordination cards remain open until actual end-to-end evidence exists.
