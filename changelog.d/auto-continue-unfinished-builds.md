### Fixed

- A build whose worker committed or staged real work and stopped before its typed handoff now gets one preserved continuation automatically (governed `authorize()`, byte-preserving workspace archive, proven death, one-use grant) instead of waiting in `awaiting-evidence` forever.
