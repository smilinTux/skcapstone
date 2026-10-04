# Failed remote review retirement

Use `skcapstone fleet review-retire` on the production authority after a remote
review worker failed before producing retained review custody. This is an
operator recovery action for one exact generation, not a review verdict.

Run with the same policy context as the dispatcher:

```sh
export SKFLEET_PRODUCTION_POLICY="$HOME/.skcapstone/fleet/production.json"
export SKFLEET_AUTHORITY_HOST=chiap08
skcapstone fleet review-retire CARD --node NODE \
  --request-sha256 REQUEST_FILE_SHA256 --status-sha256 STATUS_FILE_SHA256 \
  --card-sha256 REVIEW_STATE_SHA256 --agent jarvis --reason 'Observed failure'
```

The default only checks. Pin the hashes of the current request and status file
bytes and `skcapstone.seat_runtime.review_state_revision` of the current folded
card. Add `--apply` only to apply those same checked inputs.

Required evidence is the native authority offer and exact launch, a released
claim, one attributable nonzero worker exit, and managed unit death on the
destination. The card must still be unclaimed review work. Changed generations,
live workers, successful exits and retained review packets are refused.

Apply archives the exact request, status, worker exit and retirement receipt
under `evidence/work/CARD/retired-reviews/REQUEST_ID`, records the operator in
the native event history, then removes the stale status and request pointers.
The workspace and earlier event history are retained. An interrupted cleanup
can be retried with the same hashes. Every historical offer must have an exact
retirement receipt before the normal scheduler may create a fresh generation.

Install this command only through a merged-main rollout.
