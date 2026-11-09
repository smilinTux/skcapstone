A rollout to a host whose kernel has AppArmor disabled (WSL2: built in, off at
boot) no longer halts at `install_sandbox_profile`. The profile file is still
installed, the `apparmor_parser` load is skipped with a note, and the sandbox
readiness gate still requires both profiles loaded in enforce mode, so such a
host fails closed for sealed qualification rather than running a sandbox
without the capability deny. chiwk12-wsl, 2026-11-09.
