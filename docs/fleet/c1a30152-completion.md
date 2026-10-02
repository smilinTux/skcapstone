# c1a30152 source qualification

Scope: existing Link exclusions input, tests, and documentation only. The
claimed source starts at `8abe125000f9338966d60e3c0bc76f574c523ad4`.

Installed baseline SHA256
`40c09ec3a3e2a3a68d6c62c64ea63d9e51653b07d9559ba08b1cffbd326376d6`
matches the script at Git commit
`9b6fbc726fa3632529444e71b6b8d6404516f8d0` byte for byte after replacing
only the installed interpreter shebang with the repository shebang. The
repository SHA256 is
`f10fc95737b4c8d99215723d3d10754bf2e711bfdb29597ff334c58d3a36ec3c`.
No newer installed behavior was discarded.

The bounded implementation extends the existing operator JSON input, validates
all dispositions before mapping any PR, and retains each applied or rejected
record. It preserves the observation producer's existing mapping hash contract.
The default CLI discovers the operator file; no additional worker or orchestration
framework is introduced. The explicit file allowlist is retained despite the
generic 500-line guidance: the existing tests already exceeded that guidance,
and splitting this change would add an unauthorized helper file.

Validation: the initial red phase had 19 expected new failures and 26 existing
passes. The final adjacent run passed 94 tests covering Link lineage, observation
production, and review work. Subsequent compatibility changes are rechecked
with the affected lineage tests and static checks, with exact results in private
completion evidence.

A read-only live PR28 replay at head
`f62f5327e0dc1d3b00e72d7426790e078ec7aa15` used hypothetical explicit
dispositions for operational cards c1a30129, c1a30134, c1a30149, and c1a30150.
It reduced the candidate sources to c1a30138, retained real review c1a30151,
and remained unresolved because that review was pending. No live operator
file, CardStore state, service, feed, approval, or runtime installation changed.
Private evidence is under `~/.skcapstone/evidence/work/c1a30152/`.

Root owns installation and actual operator dispositions after independent
review. Rollback is removal of the operator input and restoration of the
preserved installed baseline. Source qualification is not a publication grant.
