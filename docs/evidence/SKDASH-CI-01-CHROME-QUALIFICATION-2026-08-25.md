# SKDASH-CI-01 Chrome qualification evidence

Card: `054710f7`

## Reproduction

Main CI run `32850965876` failed once when the real-Chrome AI qualification
subprocess returned exit status 1. The wrapper hid its captured stderr, so the
failed stage was unavailable. The unchanged exact-SHA rerun passed.

The harness also bound fixed port `17884`. Holding that port with a local
public-synthetic server reproduced exit status 1 and `Dashboard did not start`.
The server subprocess error was discarded and temporary dashboard and Chrome
directories survived every run.

## Repair

The embedded Uvicorn server now binds port zero and publishes the OS-assigned
loopback port through its private temporary directory. The browser uses that
exact port. A failure writes one bounded JSON diagnostic containing only the
qualification stage, assertion message, and child exit codes. The pytest
wrapper exposes at most the final 4000 stderr characters.

Both child processes are stopped before the two private temporary directories
are removed. The success result is printed only after cleanup. Authorization,
purge, stale-response, accessibility, contrast, responsive, no-write, and
no-external assertions are unchanged.

## Qualification and rollback

Qualification must prove that the harness passes while port `17884` remains
occupied, then pass repeated focused runs and the complete suite. CI remains
authoritative for Python 3.10 and 3.12.

Local results:

- fixed-port sensitivity before repair: exit 1 with `Dashboard did not start`
- repaired qualifier while port `17884` stayed occupied: 5 of 5 passed
- forced Chrome failure: expected test failure named stage `Chrome startup`
- focused workspace tests: 4 passed
- complete suite: 546 passed with 8 inherited deprecation warnings
- full Ruff, Python format, Node syntax, and diff checks: passed
- recent scratch created by this qualifier after success or failure: none

## Post-merge timing finding

Main CI run `32852897263` exercised the new diagnostic and failed at stage
`Chrome startup` with both child exit codes unset. Chrome was alive but had not
published `DevToolsActivePort` within the existing 10-second readiness bound.
The release workflow skipped, `v0.1.84` remained absent, and PyPI returned 404.

The follow-up keeps all assertions and polling cadence unchanged. Only Chrome
startup receives a bounded 30-second readiness window, with a 60-second outer
subprocess limit. No test retry or skip is added.

Rollback is a normal reviewed revert. It restores only the qualifier and test
wrapper, but it also restores the reproduced fixed-port collision and hidden
diagnostics, so rollback is not a safe operational workaround.
