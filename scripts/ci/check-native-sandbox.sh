#!/usr/bin/env -S -u BASH_ENV bash
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "${RUNNER_ENVIRONMENT:-}" == github-hosted ]] || {
    echo 'Native sandbox provisioning is restricted to disposable GitHub-hosted runners.' >&2
    exit 1
}
unset BASH_ENV ENV
command=(systemd-run --user --quiet --wait --pipe --collect
    --property=NoNewPrivileges=yes --property=RuntimeMaxSec=15 --
    /usr/bin/bwrap --unshare-all --die-with-parent --new-session
    --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64
    --proc /proc --dev /dev --tmpfs /tmp /usr/bin/true)
if "${command[@]}" > sandbox-ci.log 2>&1; then
    echo 'Native sandbox preflight passed.' >> sandbox-ci.log
else
    cat sandbox-ci.log
    # Ubuntu runner AppArmor can prohibit the user namespaces required by bwrap.
    # Change only this disposable host, and only after observing that refusal.
    grep -qiE '(namespace|loopback).*(not permitted|permission denied)' sandbox-ci.log
    sysctl kernel.apparmor_restrict_unprivileged_userns >> sandbox-ci.log 2>&1
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 >> sandbox-ci.log 2>&1
    "${command[@]}" >> sandbox-ci.log 2>&1
    echo 'Native sandbox passed after enabling runner user namespaces.' >> sandbox-ci.log
fi
