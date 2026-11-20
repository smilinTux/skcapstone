`service_health` no longer probes Qdrant (skvector) by default. skmemory keeps vectors
in the per-node skmem-pg and the Qdrant backend is retired, so the probe stops reading
`skvector.yaml` and stops falling back to `http://localhost:6333`; either one filed a false
"down" incident per sweep. It runs only when `SKMEMORY_SKVECTOR_URL` is set explicitly
(opt-in, and the rollback path). SKGraph, Syncthing and daemon probes are unchanged.
