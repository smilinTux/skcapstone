### Fixed

- Acceptance requalifies a test profile that differs only in fingerprints (base-revision source, runtime, toolchain): its trusted test run at the candidate head is the qualification evidence, and the successor profile is published under the source custody claim (or unclaimed if released) before the pair finishes. Contract changes still refuse.
