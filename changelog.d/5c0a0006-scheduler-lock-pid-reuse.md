- The scheduler's per-job overlap lock now records `{v, pid, btime, token}` (btime is
  `/proc/<pid>/stat` field 22) instead of a bare PID. A lock is stale only when its owner
  is provably gone: no such PID, or the PID is alive with a different start time (PID reuse
  used to block a job behind an unrelated process indefinitely). A provably live owner is
  never stolen. Locks that cannot be validated (legacy PID-only for a live PID, garbage,
  unverifiable start time) block for `SKCAPSTONE_LOCK_STALE_SECONDS` (default 600) and are
  then recovered. Release unlinks only a lock that still holds our own record.
  Port of #521 onto current main.
