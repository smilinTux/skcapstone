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

Rollback is a normal reviewed revert. It restores only the qualifier and test
wrapper, but it also restores the reproduced fixed-port collision and hidden
diagnostics, so rollback is not a safe operational workaround.
