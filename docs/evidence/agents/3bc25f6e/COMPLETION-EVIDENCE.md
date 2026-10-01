# Private launch request creation

Card: `3bc25f6e`. Producer: `codex-private-request-3bc25f6e`.
Base: `0921e811c24b1f6f84251bc60a1c797423a2b886`.

The SSH process creates the launch request before systemd's service UMask
applies. The former text exclusive open requested mode 0666, yielding 0664
under SSH umask 002. The candidate collector correctly refused that evidence.

`fiber_worker.start` now uses exclusive, no-follow descriptor creation with
mode 0600, then retains its existing JSON serialization, flush and fsync before
starting the service. Claims, resource limits and collector checks are unchanged.
The same small stdlib pattern already exists in the test harness. Reusing the
refill publisher would introduce temporary-file/hardlink publication semantics
unnecessary for this specific correction.

Two new regression methods cover umask 000 and 002 and inspect actual mode,
complete JSON and real collector acceptance from the mocked service-launch
boundary. Existing regular files, symlinks and dangling symlinks remain intact
and never launch a service. Before the fix the umask cases failed with 0666
and 0664 respectively; after the fix all 32 worker methods pass without skips.

Command: `python3 -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_worker.py`.
Worker result: 32 passed, zero skipped, 0.276 seconds.
Baseline and final logs are retained in the card's private evidence directory.
Collector suite is checked separately with the installed qualified interpreter.

Independent model review and installed-file verification must name the final
candidate hash, with their receipts retained outside the immutable commit.
Deployment replaces only `fiber_worker.py` after verifying its expected prior
hash and preserving exact prior bytes and mode. Rollback restores that prior
file without stopping already running services or changing retained requests.
This fix affects new requests; root owns the separately authorized correction
of the already completed trial's request mode.
