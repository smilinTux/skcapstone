# c1a30171: reject unbound beats before authority reads

Owner `codex-liveness-fast-c1a30171`, claim `c112a612c8e84767ba2dfc42645803b1`.
Base `0c8486eb9ffcabcc4c12a9fa67bb3382d9b75742`.

The c169 collector replayed CardStore authority before rejecting process
identities that could never qualify. Move that structural rejection ahead of
all board and systemd reads. Require a positive integer PID (not bool, float or
string) and the existing lowercase 32-hex invocation identity. Valid launch
payloads already use those types. Exact valid claim, wrapper and publication
checks remain unchanged.

Regression was observed failing against c169 before implementation: the spy
failed at the first `_claim_revision` call for a missing PID. Eleven malformed
identity cases now require zero board/systemd calls. Existing active, terminal,
stale process, concurrent claim and actual native handoff tests still pass.

Focused tests: 78 passed in 1.69 seconds before formatting; final results are in
the private candidate packet. No live card, service or installed code changed.

Bounded benchmark on actual retained legacy beats: 464 files, 381 card IDs.
Their filename/content-hash map has SHA-256
`eafc8627a168d0caf1da312de218326945f2971654ba265132f797078fb59ff0`.
Old collector made 464 authority calls; candidate made zero. Both returned zero
observations and made zero systemd calls. Calls were counted with a no-current-
claim stub, deliberately avoiding 464 expensive real replays. Structural scan
times were 0.012590 and 0.012032 seconds respectively; these are not full old/new
production cycle timings. One real fresh fold of c171 took 0.802271 seconds.

Root owns independent review and installation. Only installed
`fleet/worker_liveness_runtime.py` needs replacement after exact-byte backup;
rollback restores those bytes. Historical beats and active workers are preserved.
