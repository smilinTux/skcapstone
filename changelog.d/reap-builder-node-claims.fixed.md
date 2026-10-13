- Recognize exact production builder-node owners in the dead-worker reaper,
  preserving all-host liveness, claim generation, grace, and source custody
  checks. Manual owner names remain excluded. Protect current producer PASS
  outcomes before reaper evidence and under the locks used by direct builder
  recovery releases, reusing the finished-work guard from PR #1169.
